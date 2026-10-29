#!/usr/bin/env python3
"""Quick read-only check of proxy clips for the duplicate-PTS timestamp defect.

Reads the stored DTS/PTS of the first video track straight from the MP4 index
(stts/ctts), so clips are not read completely. ffprobe is only a fallback.
Run without arguments (e.g. "Run Python File" in VS Code) after setting `folder`.
"""

# ---------------------------------------------------------------- IMPORTS
import argparse
import csv
import itertools
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------- CONFIG
app_name = "pruefe_zeitstempel"
app_version = "1.0.0"

folder = Path(r"\\10.0.77.102\FiMu 1.5 Mbit Proxy")  # <- hier den Proxy-Ordner eintragen
recursive = False
report_dir = Path(__file__).resolve().parent / "reports"
ffprobe_path = "ffprobe"
ffprobe_timeout_s = 3600
max_moov_bytes = 512 * 2**20

# ---------------------------------------------------------------- STATIC
OK, DEFECT, ODD, UNREADABLE = "in Ordnung", "Fehlerbild", "anders auffällig", "nicht lesbar"
csv_header = ["datei", "status", "bilder", "doppelte_pts", "erste_fundstelle_paket", "detail", "quelle"]


class ReadError(Exception):
    """Clip cannot be read at all."""


class NeedFallback(Exception):
    """MP4 index cannot be used, ask ffprobe instead."""


@dataclass
class Result:
    name: str
    status: str
    frames: int = 0
    dup: int = 0
    first: int | None = None
    detail: str = ""
    source: str = ""

    def row(self) -> list:
        return [self.name, self.status, self.frames, self.dup, "" if self.first is None else self.first + 1, self.detail, self.source]


# ---------------------------------------------------------------- FUNC: MP4 index
def _boxes(buf: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, payload_start, box_end) for the boxes in buf[start:end]."""
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, start)
        head = 8
        if size == 1:
            size, head = struct.unpack_from(">Q", buf, start + 8)[0], 16
        elif size == 0:
            size = end - start
        if size < head:
            raise ValueError("invalid box")
        yield kind, start + head, min(start + size, end)
        start += size


def _child(buf: bytes, span: tuple[int, int] | None, kind: bytes) -> tuple[int, int] | None:
    return span and next(((s, e) for k, s, e in _boxes(buf, *span) if k == kind), None)


def _read_moov(path: Path) -> bytes:
    """Return the moov box (header included); seeks over mdat, so the clip is not read."""
    with path.open("rb") as f:
        total, pos = f.seek(0, 2), 0
        while pos + 8 <= total:
            f.seek(pos)
            head = f.read(16)
            size, kind = struct.unpack_from(">I4s", head)
            if size == 1:
                size = struct.unpack_from(">Q", head, 8)[0]
            elif size == 0:
                size = total - pos
            if size < 8:
                raise ReadError("Dateistruktur defekt")
            if kind == b"moof":
                raise NeedFallback
            if kind == b"moov":
                if size > max_moov_bytes:
                    raise ReadError("moov-Index unplausibel groß")
                f.seek(pos)
                return f.read(size)
            pos += size
    raise ReadError("moov-Index fehlt (Datei unvollständig?)")


def _table(buf: bytes, span: tuple[int, int], fmt: str) -> list[tuple]:
    s, e = span
    count = struct.unpack_from(">I", buf, s + 4)[0]
    rows = list(struct.iter_unpack(fmt, buf[s + 8:e]))
    if len(rows) != count:
        raise ValueError("table size mismatch")
    return rows


def _mp4_timestamps(path: Path) -> tuple[list, list, float]:
    """Return (dts, pts, tick_seconds) of the first video track in decode order."""
    moov = _read_moov(path)
    _, ms, me = next(_boxes(moov, 0, len(moov)))
    if _child(moov, (ms, me), b"mvex"):
        raise NeedFallback
    for kind, ts, te in _boxes(moov, ms, me):
        mdia = _child(moov, (ts, te), b"mdia") if kind == b"trak" else None
        hdlr = _child(moov, mdia, b"hdlr")
        if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
            continue
        mdhd, stbl = _child(moov, mdia, b"mdhd"), _child(moov, _child(moov, mdia, b"minf"), b"stbl")
        stts, ctts, stsz = (_child(moov, stbl, k) for k in (b"stts", b"ctts", b"stsz"))
        if not (mdhd and stts and stsz):
            raise NeedFallback
        timescale = struct.unpack_from(">I", moov, mdhd[0] + (20 if moov[mdhd[0]] == 1 else 12))[0]
        deltas = list(itertools.chain.from_iterable(itertools.repeat(d, c) for c, d in _table(moov, stts, ">II")))
        if struct.unpack_from(">I", moov, stsz[0] + 8)[0] != len(deltas) or not timescale:
            raise NeedFallback
        dts = list(itertools.accumulate(deltas, initial=0))[:len(deltas)]
        if not ctts:
            return dts, list(dts), 1 / timescale
        offsets = list(itertools.chain.from_iterable(itertools.repeat(o, c) for c, o in _table(moov, ctts, ">Ii")))
        # missing ctts entries stay None and are reported as missing timestamps
        return dts, [d + o for d, o in zip(dts, offsets)] + [None] * (len(dts) - len(offsets)), 1 / timescale
    raise NeedFallback


# ---------------------------------------------------------------- FUNC: ffprobe fallback
def _ffprobe_timestamps(path: Path) -> tuple[list, list, float]:
    exe = shutil.which(ffprobe_path)
    if not exe:
        raise ReadError("ffprobe nicht gefunden, wird für diese Datei benötigt (ffprobe_path in CONFIG)")
    cmd = [exe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts,dts:stream=time_base", "-of", "compact=p=0", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=ffprobe_timeout_s)
    if proc.returncode or proc.stderr.strip():
        raise ReadError(f"ffprobe: {(proc.stderr.strip() or 'Fehler').splitlines()[0]}")
    dts, pts, tick_s = [], [], 0.0
    for line in proc.stdout.splitlines():
        fields = dict(f.split("=", 1) for f in line.split("|") if "=" in f)
        if "time_base" in fields:
            num, den = fields["time_base"].split("/")
            tick_s = int(num) / int(den)
        else:
            dts.append(None if fields.get("dts", "N/A") == "N/A" else int(fields["dts"]))
            pts.append(None if fields.get("pts", "N/A") == "N/A" else int(fields["pts"]))
    return dts, pts, tick_s


# ---------------------------------------------------------------- FUNC: evaluation
def judge(dts: list, pts: list) -> Result:
    """Classify stored timestamps (decode order). Defect = duplicate PTS while DTS are regular."""
    if not dts:
        return Result("", UNREADABLE, detail="keine Videobilder gefunden")
    n = len(dts)
    if (missing := sum(v is None for v in dts + pts)):
        return Result("", ODD, n, detail=f"{missing} Zeitstempel fehlen")
    diffs = [b - a for a, b in zip(dts, dts[1:])]
    step = Counter(diffs).most_common(1)[0][0] if diffs else 0
    regular = not diffs or (step > 0 and all(d == step for d in diffs))
    seen, dup, first = set(), 0, None
    for i, p in enumerate(pts):
        if p in seen:
            dup, first = dup + 1, i if first is None else first
        seen.add(p)
    if dup and regular:
        return Result("", DEFECT, n, dup, first, f"{dup} doppelte PTS bei regelmäßigen DTS")
    if dup:
        return Result("", ODD, n, dup, first, f"{dup} doppelte PTS bei unregelmäßigen DTS")
    if any(d <= 0 for d in diffs):
        return Result("", ODD, n, detail="DTS nicht streng steigend")
    ordered = sorted(pts)
    if regular and (gaps := sum(b - a != step for a, b in zip(ordered, ordered[1:]))):
        return Result("", ODD, n, detail=f"PTS nicht lückenlos auf dem Bildraster ({gaps} Lücken)")
    return Result("", OK, n)


def check_clip(path: Path) -> Result:
    try:
        try:
            (dts, pts, tick_s), source = _mp4_timestamps(path), "mp4-index"
        except (NeedFallback, struct.error, ValueError, IndexError):
            (dts, pts, tick_s), source = _ffprobe_timestamps(path), "ffprobe"
    except (ReadError, OSError, subprocess.SubprocessError) as err:
        return Result(path.name, UNREADABLE, detail=str(err) or type(err).__name__)
    res = judge(dts, pts)
    res.name, res.source = path.name, source
    if res.first is not None:
        res.detail += f"; erste Fundstelle: Paket {res.first + 1} (PTS {pts[res.first]}" + (f" = {pts[res.first] * tick_s:.3f} s)".replace(".", ",") if tick_s else ")")
    return res


# ---------------------------------------------------------------- FUNC: reports
def _hms(seconds: float) -> str:
    return f"{int(seconds) // 3600}:{int(seconds) % 3600 // 60:02d}:{int(seconds) % 60:02d}"


def write_error_list(path: Path, results: list[Result], src: Path, total: int, state: str) -> None:
    lines = [f"{app_name} {app_version} | Ordner: {src}", f"Stand: {state} | geprüft {len(results)} von {total} Clips", ""]
    for status in (DEFECT, ODD, UNREADABLE):
        hits = sorted((r for r in results if r.status == status), key=lambda r: r.name)
        lines += [f"== {status} ({len(hits)}) =="] + [f"{r.name} | {r.detail}" for r in hits] + [""]
    path.write_text("\n".join(lines), encoding="utf-8")


def list_clips(src: Path) -> list[Path]:
    """Read the folder once; later changes are ignored."""
    return sorted(p for p in (src.rglob("*") if recursive else src.iterdir()) if p.is_file() and p.suffix.casefold() == ".mp4")


# ---------------------------------------------------------------- MAIN
def main() -> int:
    parser = argparse.ArgumentParser(description="Schnellprüfung der Zeitstempel (nur lesend).")
    parser.add_argument("--folder", type=Path, help="Proxy-Ordner (Standard: folder in CONFIG)")
    src = parser.parse_args().folder or folder
    try:
        clips = list_clips(src) if src.is_dir() else None
    except OSError as err:
        print(f"Ordner nicht lesbar: {src} ({err})")
        return 1
    if clips is None:
        print(f"Ordner nicht gefunden: {src}\nTrage den Pfad im Block CONFIG (folder) ein oder nutze --folder.")
        return 1
    if not clips:
        print(f"Keine .mp4-Dateien in {src} gefunden.")
        return 1
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    csv_path, list_path = report_dir / f"pruefung_{stamp}.csv", report_dir / f"fehlerliste_{stamp}.txt"
    print(f"{app_name} {app_version}: {len(clips)} Clips in {src}\nBerichte: {list_path.name}, {csv_path.name} in {report_dir}")
    results, counts, started, state = [], Counter(), time.monotonic(), "abgeschlossen"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(csv_header)
        try:
            for i, clip in enumerate(clips, 1):
                res = check_clip(clip)
                results.append(res)
                counts[res.status] += 1
                writer.writerow(res.row())
                f.flush()
                if res.status != OK:
                    print(f"\r{'':<120}\r{res.status}: {res.name} | {res.detail}")
                    write_error_list(list_path, results, src, len(clips), "läuft")
                elapsed = time.monotonic() - started
                line = f"[{i}/{len(clips)}] {i / len(clips):.0%} | Fehlerbild {counts[DEFECT]} | auffällig {counts[ODD]} | nicht lesbar {counts[UNREADABLE]} | {_hms(elapsed)} | Rest ~{_hms(elapsed / i * (len(clips) - i))}"
                print(f"\r{line:<120}", end="", flush=True)
        except KeyboardInterrupt:
            state = "ABGEBROCHEN"
    write_error_list(list_path, results, src, len(clips), state)
    print(f"\n{state}: {len(results)}/{len(clips)} geprüft | in Ordnung {counts[OK]} | Fehlerbild {counts[DEFECT]} | anders auffällig {counts[ODD]} | nicht lesbar {counts[UNREADABLE]} | {_hms(time.monotonic() - started)}")
    return 0


# ---------------------------------------------------------------- EXEC
if __name__ == "__main__":
    sys.exit(main())
