#!/usr/bin/env python3
"""Check selected MP4 clips, repair affected timestamps, and deliver safe outputs."""

# --------- IMPORTS ---------
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator
import csv
import itertools
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time


# --------- CONFIG ---------
app_name = "Repariere Zeitstempel"
app_version = "2.0.0"
max_moov_bytes = 512 * 2**20
ffprobe_timeout_s = 3600


# --------- STATIC ---------
OK, DEFECT, ODD, UNREADABLE = "in Ordnung", "Fehlerbild", "anders auffällig", "nicht lesbar"
REPAIRED, FAILED, UNSUPPORTED, TARGET_EXISTS, COPIED = (
    "repariert", "Fehler", "nicht reparierbar", "übersprungen (Ziel vorhanden)", "in Ordnung, kopiert")
exists_detail = "Datei mit gleichem Namen liegt bereits im Ausgabeordner, nicht überschrieben"
report_order = (FAILED, UNSUPPORTED, ODD, UNREADABLE, TARGET_EXISTS, REPAIRED, COPIED)
delivered = (REPAIRED, COPIED)
csv_header = ["datei", "status", "bilder", "doppelte_pts", "detail"]
color_options = (("color_range", "-color_range:v:0"), ("color_primaries", "-color_primaries:v:0"),
                 ("color_transfer", "-color_trc:v:0"), ("color_space", "-colorspace:v:0"))
video_keys = ("codec_name", "width", "height", "pix_fmt", "sample_aspect_ratio", *(key for key, _ in color_options))
audio_keys = ("codec_name", "sample_rate", "channels", "channel_layout", "nb_frames", "start_time")
base_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
bundle_dir = Path(getattr(sys, "_MEIPASS", base_dir))


class ConfigError(Exception):
    """Run cannot start."""


class ReadError(Exception):
    """Clip cannot be read."""


class NeedFallback(Exception):
    """MP4 index cannot be used, ask ffprobe instead."""


class Unsupported(Exception):
    """Defect found, but the clip does not meet the repair preconditions."""


class RepairError(Exception):
    """Repair, verification or publication failed."""


@dataclass(frozen=True)
class Timestamps:
    dts: list
    pts: list
    time_base: Fraction


@dataclass
class Check:
    status: str
    frames: int = 0
    dup: int = 0
    first: int | None = None
    step: int = 0
    detail: str = ""


@dataclass(frozen=True)
class Plan:
    video: dict
    rate: Fraction
    time_base: Fraction
    bitrate: int


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    frames: int = 0
    dup: int = 0
    detail: str = ""

    def row(self) -> list:
        return [self.name, self.status, self.frames, self.dup, self.detail]


# --------- INIT ---------
tools: dict[str, str] = {}


# --------- FUNC: dialogs and tools ---------
def ask_inputs() -> tuple[list[Path], Path] | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        files = filedialog.askopenfilenames(parent=root, title="Clips für die Lieferung wählen",
                                            filetypes=[("MP4-Dateien", "*.mp4")])
        if not files:
            return None
        folder = filedialog.askdirectory(parent=root, title="Ausgabeordner wählen", mustexist=True)
        if not folder:
            return None
        return [Path(file) for file in files], Path(folder)
    finally:
        root.destroy()


def show_message(title: str, text: str, warning: bool) -> None:
    root = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        (messagebox.showwarning if warning else messagebox.showinfo)(title, text, parent=root)
    except Exception:
        pass
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def validate_selection(clips: list[Path], target: Path) -> None:
    if not target.is_dir():
        raise ConfigError(f"Ausgabeordner nicht gefunden: {target}")
    if not os.access(target, os.W_OK):
        raise ConfigError(f"Ausgabeordner nicht beschreibbar: {target}")
    for clip in clips:
        if not clip.is_file():
            raise ConfigError(f"Datei nicht gefunden: {clip}")
        if clip.suffix.casefold() != ".mp4":
            raise ConfigError(f"Nur MP4-Dateien möglich: {clip.name}")
        if os.path.normcase(str(clip.resolve().parent)).casefold() == os.path.normcase(str(target.resolve())).casefold():
            raise ConfigError("Ausgabeordner muss sich vom Ordner der gewählten Dateien unterscheiden")


def duplicate_names(clips: list[Path]) -> set[str]:
    counts = Counter(clip.name.casefold() for clip in clips)
    return {name for name, count in counts.items() if count > 1}


def find_tool(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    for folder in dict.fromkeys((base_dir / "res", bundle_dir / "res")):
        if (exe := folder / f"{name}{suffix}").is_file():
            return str(exe)
    if found := shutil.which(name):
        return found
    raise ConfigError(f"{name} nicht gefunden: in res/ neben der EXE ablegen oder in den Systempfad aufnehmen")


def tool_version(exe: str) -> str:
    proc = subprocess.run([exe, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    return (proc.stdout.splitlines() or ["unbekannt"])[0]


# --------- FUNC: MP4 index ---------
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


def _mp4_timestamps(path: Path) -> Timestamps:
    """Stored DTS/PTS of the first video track in decode order."""
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
            return Timestamps(dts, list(dts), Fraction(1, timescale))
        offsets = list(itertools.chain.from_iterable(itertools.repeat(o, c) for c, o in _table(moov, ctts, ">Ii")))
        # missing ctts entries stay None and are reported as missing timestamps
        return Timestamps(dts, [d + o for d, o in zip(dts, offsets)] + [None] * (len(dts) - len(offsets)), Fraction(1, timescale))
    raise NeedFallback


# --------- FUNC: ffprobe ---------
def _ffprobe(args: list[str], path: Path) -> str:
    proc = subprocess.run([tools["ffprobe"], "-v", "error", *args, str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=ffprobe_timeout_s)
    if proc.returncode:
        raise ReadError(f"ffprobe: {(proc.stderr.strip() or 'Fehler').splitlines()[0]}")
    return proc.stdout


def _ffprobe_timestamps(path: Path) -> Timestamps:
    dts, pts, time_base = [], [], Fraction(0)
    for line in _ffprobe(["-select_streams", "v:0", "-show_entries", "packet=pts,dts:stream=time_base", "-of", "compact=p=0"], path).splitlines():
        fields = dict(f.split("=", 1) for f in line.split("|") if "=" in f)
        if "time_base" in fields:
            time_base = Fraction(fields["time_base"])
        else:
            dts.append(None if fields.get("dts", "N/A") == "N/A" else int(fields["dts"]))
            pts.append(None if fields.get("pts", "N/A") == "N/A" else int(fields["pts"]))
    return Timestamps(dts, pts, time_base)


def probe_streams(path: Path) -> dict:
    return json.loads(_ffprobe(["-show_format", "-show_streams", "-show_chapters", "-of", "json"], path))


def _streams(info: dict, kind: str) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def _timecode(info: dict) -> str | None:
    tags = [info.get("format", {}).get("tags", {}), *(s.get("tags", {}) for s in info.get("streams", []))]
    return next((t["timecode"] for t in tags if t.get("timecode")), None)


def _fraction(value) -> Fraction | None:
    try:
        return Fraction(str(value).replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None


# --------- FUNC: detection ---------
def judge(dts: list, pts: list) -> Check:
    """Classify stored timestamps (decode order). Defect = duplicate PTS while DTS are regular."""
    if not dts:
        return Check(UNREADABLE, detail="keine Videobilder gefunden")
    n = len(dts)
    if missing := sum(v is None for v in dts + pts):
        return Check(ODD, n, detail=f"{missing} Zeitstempel fehlen")
    diffs = [b - a for a, b in zip(dts, dts[1:])]
    step = Counter(diffs).most_common(1)[0][0] if diffs else 0
    regular = not diffs or (step > 0 and all(d == step for d in diffs))
    seen, dup, first = set(), 0, None
    for i, p in enumerate(pts):
        if p in seen:
            dup, first = dup + 1, i if first is None else first
        seen.add(p)
    if dup and regular:
        return Check(DEFECT, n, dup, first, step, f"{dup} doppelte PTS bei regelmäßigen DTS")
    if dup:
        return Check(ODD, n, dup, first, step, f"{dup} doppelte PTS bei unregelmäßigen DTS")
    if any(d <= 0 for d in diffs):
        return Check(ODD, n, step=step, detail="DTS nicht streng steigend")
    ordered = sorted(pts)
    if regular and (gaps := sum(b - a != step for a, b in zip(ordered, ordered[1:]))):
        return Check(ODD, n, step=step, detail=f"PTS nicht lückenlos auf dem Bildraster ({gaps} Lücken)")
    return Check(OK, n, step=step)


def check_clip(path: Path) -> tuple[Check, Timestamps | None]:
    try:
        try:
            stamps = _mp4_timestamps(path)
        except (NeedFallback, struct.error, ValueError, IndexError):
            stamps = _ffprobe_timestamps(path)
    except (ReadError, OSError, ValueError, subprocess.SubprocessError) as err:
        return Check(UNREADABLE, detail=str(err) or type(err).__name__), None
    check = judge(stamps.dts, stamps.pts)
    if check.first is not None:
        p = stamps.pts[check.first]
        seconds = f" = {float(p * stamps.time_base):.3f} s".replace(".", ",") if stamps.time_base else ""
        check.detail += f"; erste Fundstelle: Paket {check.first + 1} (PTS {p}{seconds})"
    return check, stamps


# --------- FUNC: repair ---------
def plan_repair(info: dict, stamps: Timestamps, check: Check) -> Plan:
    videos = _streams(info, "video")
    if len(videos) != 1:
        raise Unsupported(f"{len(videos)} Videostreams, erwartet genau einer")
    video = videos[0]
    if video.get("codec_name") != "h264":
        raise Unsupported(f"Videocodec {video.get('codec_name')}, erwartet h264")
    if others := [s.get("codec_tag_string") or s.get("codec_type") for s in info.get("streams", [])
                  if s.get("codec_type") not in ("video", "audio") and s.get("codec_tag_string") != "tmcd"]:
        raise Unsupported(f"weitere Spuren würden verloren gehen: {', '.join(map(str, others))}")
    if not stamps.time_base or check.step <= 0:
        raise Unsupported("Bildraster nicht bestimmbar")
    rate = 1 / (check.step * stamps.time_base)
    if _fraction(video.get("r_frame_rate")) != rate:
        raise Unsupported(f"Bildrate aus DTS ({rate}) passt nicht zu ffprobe ({video.get('r_frame_rate')})")
    bitrate = int(value) if (value := str(video.get("bit_rate", ""))).isdigit() else 0
    if bitrate <= 0:
        raise Unsupported("Video-Bitrate unbekannt")
    if video.get("pix_fmt") in (None, "", "unknown"):
        raise Unsupported("Pixelformat unbekannt")
    return Plan(video, rate, stamps.time_base, bitrate)


def repair_command(src: Path, part: Path, plan: Plan) -> list[str]:
    """Decode in decoder order, rebuild PTS from the frame number, encode without B-frames, copy audio."""
    video, frame_tb = plan.video, f"{plan.rate.denominator}/{plan.rate.numerator}"
    sar = _fraction(video.get("sample_aspect_ratio"))
    filters = [f"settb=expr={frame_tb}", "setpts=N", *([f"setsar={sar.numerator}/{sar.denominator}"] if sar and sar > 0 else [])]
    # only video and audio are mapped: copying the tmcd data track fails in mp4, the muxer rebuilds it from the timecode tag
    command = [tools["ffmpeg"], "-hide_banner", "-nostdin", "-n", "-v", "error", "-nostats", "-progress", "pipe:1",
               "-copyts", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
               "-c", "copy", "-c:v:0", "libx264", "-bf:v:0", "0", "-b:v:0", str(plan.bitrate), "-pix_fmt:v:0", video["pix_fmt"],
               "-vf:v:0", ",".join(filters), "-fps_mode:v:0", "passthrough", "-enc_time_base:v:0", frame_tb,
               "-avoid_negative_ts", "disabled"]
    if plan.time_base.numerator == 1:
        command += ["-video_track_timescale", str(plan.time_base.denominator)]
    command += [arg for key, option in color_options if (value := video.get(key)) not in (None, "", "unknown", "N/A")
                for arg in (option, value)]
    return command + ["-f", "mp4", str(part)]


def run_ffmpeg(command: list[str], show: Callable[[int], None]) -> None:
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                text=True, encoding="utf-8", errors="replace")
        try:
            for line in proc.stdout:
                if line.startswith("frame=") and line[6:].strip().isdigit():
                    show(int(line[6:]))
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        errors.seek(0)
        message = errors.read().decode("utf-8", "replace").strip()
    if code:
        raise RepairError(f"ffmpeg Exit {code}: {(message.splitlines() or ['ohne Meldung'])[-1]}")


def verify(src_info: dict, src_stamps: Timestamps, src_check: Check, part: Path) -> tuple[list[str], int]:
    """Return problems of the repaired file and its frame count."""
    out_check, out_stamps = check_clip(part)
    if out_stamps is None:
        return [f"Ausgabe nicht lesbar: {out_check.detail}"], 0
    problems = [] if out_check.status == OK else [f"Zeitstempel: {out_check.status}, {out_check.detail}"]
    if out_stamps.pts != out_stamps.dts:
        problems.append("Ausgabe enthält umsortierte Bilder (B-Frames)")
    if out_check.step * out_stamps.time_base != src_check.step * src_stamps.time_base:
        problems.append("Bildrate weicht ab")
    if out_check.frames not in (src_check.frames, src_check.frames - 1):
        problems.append(f"{out_check.frames} Bilder statt {src_check.frames}")
    out_info = probe_streams(part)
    out_videos, src_video = _streams(out_info, "video"), _streams(src_info, "video")[0]
    if len(out_videos) != 1:
        return problems + [f"{len(out_videos)} Videostreams in der Ausgabe"], out_check.frames
    problems += [f"Video {key}: {out_videos[0].get(key)} statt {src_video.get(key)}" for key in video_keys
                 if out_videos[0].get(key) != src_video.get(key)]
    if str(out_videos[0].get("has_b_frames")) != "0":
        problems.append("Ausgabe enthält B-Frames")
    src_audio, out_audio = _streams(src_info, "audio"), _streams(out_info, "audio")
    if len(src_audio) != len(out_audio):
        problems.append(f"{len(out_audio)} Audiospuren statt {len(src_audio)}")
    problems += [f"Audio {i} {key}: {o.get(key)} statt {s.get(key)}" for i, (s, o) in enumerate(zip(src_audio, out_audio), 1)
                 for key in audio_keys if o.get(key) != s.get(key)]
    problems += [f"Audio {i} Sprache: {o.get('tags', {}).get('language')} statt {s.get('tags', {}).get('language')}"
                 for i, (s, o) in enumerate(zip(src_audio, out_audio), 1) if o.get("tags", {}).get("language") != s.get("tags", {}).get("language")]
    if _timecode(out_info) != _timecode(src_info):
        problems.append(f"Timecode {_timecode(out_info)} statt {_timecode(src_info)}")
    if len(out_info.get("chapters", [])) != len(src_info.get("chapters", [])):
        problems.append("Kapitel weichen ab")
    return problems, out_check.frames


def publish(part: Path, final: Path) -> None:
    """Rename without replacing an existing file."""
    if os.path.lexists(final):
        raise RepairError("Zieldatei ist inzwischen vorhanden, nicht überschrieben")
    if os.name == "nt":
        os.rename(part, final)  # fails if final exists
    else:
        os.link(part, final)
        part.unlink()


def _discard(part: Path) -> None:
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass


def copy_clip(src: Path, part: Path, final: Path) -> None:
    try:
        shutil.copy2(src, part)
        if part.stat().st_size != src.stat().st_size:
            raise RepairError("Kopie unvollständig")
        publish(part, final)
    except BaseException:
        _discard(part)
        raise


def process_clip(src: Path, target_dir: Path, show: Callable[[str], None]) -> Result:
    final, part = target_dir / src.name, target_dir / f"{src.name}.part"
    if os.path.lexists(final):
        return Result(src.name, TARGET_EXISTS, detail=exists_detail)
    if os.path.lexists(part):
        return Result(src.name, FAILED, detail=f"{part.name} existiert bereits, bitte prüfen und löschen")

    check, stamps = check_clip(src)
    result = lambda status, detail: Result(src.name, status, check.frames, check.dup, detail)
    if check.status == OK:
        try:
            show("Kopieren")
            copy_clip(src, part, final)
        except (RepairError, OSError) as err:
            return result(FAILED, f"Kopieren fehlgeschlagen: {err}")
        return result(COPIED, "")
    if check.status != DEFECT:
        return result(check.status, check.detail)
    try:
        info = probe_streams(src)
        plan = plan_repair(info, stamps, check)
    except Unsupported as err:
        return result(UNSUPPORTED, f"{check.detail}; {err}")
    except (ReadError, OSError, ValueError, subprocess.SubprocessError) as err:
        return result(UNREADABLE, f"{check.detail}; {err}")
    try:
        run_ffmpeg(repair_command(src, part, plan), lambda frame: show(f"Reparatur {frame / check.frames:.0%} ({frame}/{check.frames} Bilder)"))
        show("Prüfung der Ausgabe")
        problems, frames = verify(info, stamps, check, part)
        if problems:
            raise RepairError("Prüfung nicht bestanden: " + "; ".join(problems))
        publish(part, final)
    except (RepairError, ReadError, OSError, ValueError, subprocess.SubprocessError) as err:
        _discard(part)
        return result(FAILED, f"{check.detail}; {err}")
    except KeyboardInterrupt:
        _discard(part)
        raise
    end = "" if frames == check.frames else " (verworfener Schlussframe fehlt)"
    return result(REPAIRED, f"{check.detail}; Ausgabe {frames} Bilder{end}")


# --------- FUNC: reports ---------
def _hms(seconds: float) -> str:
    return f"{int(seconds) // 3600}:{int(seconds) % 3600 // 60:02d}:{int(seconds) % 60:02d}"


def write_protocol(path: Path, header: list[str], results: list[Result], total: int, state: str) -> None:
    not_delivered = sum(result.status not in delivered for result in results)
    delivery_line = f"NICHT GELIEFERT: {not_delivered} Clips" if not_delivered else "Alle Clips geliefert"
    lines = [*header, f"Stand: {state} | bearbeitet {len(results)} von {total} Clips", delivery_line, ""]
    for status in report_order:
        hits = sorted((r for r in results if r.status == status), key=lambda r: r.name.casefold())
        lines += [f"== {status} ({len(hits)}) =="] + [f"{r.name} | {r.detail}" if r.detail else r.name for r in hits] + [""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------- MAIN ---------
def main() -> int:
    selection = ask_inputs()
    if selection is None:
        return 0
    clips, target = selection
    clips = sorted(clips, key=lambda path: path.name.casefold())
    try:
        validate_selection(clips, target)
        tools.update(ffmpeg=find_tool("ffmpeg"), ffprobe=find_tool("ffprobe"))
        report_dir = base_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
    except (ConfigError, OSError) as err:
        print(f"Abbruch: {err}", file=sys.stderr)
        show_message(app_name, f"Abbruch: {err}", True)
        return 2

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    protocol_path = next(path for i in itertools.count()
                         if not (path := report_dir / f"reparatur_{stamp}{f'_{i}' if i else ''}.txt").exists())
    csv_path = protocol_path.with_suffix(".csv")
    header = [f"{app_name} {app_version}", f"Ausgabe: {target}",
              *(f"Quelle: {folder}" for folder in dict.fromkeys(path.parent for path in clips)),
              f"ffmpeg: {tool_version(tools['ffmpeg'])} ({tools['ffmpeg']})",
              f"ffprobe: {tool_version(tools['ffprobe'])} ({tools['ffprobe']})"]
    print("\n".join(header) + f"\n{len(clips)} Clips zu prüfen\nBerichte: {protocol_path}")
    duplicate_set = duplicate_names(clips)
    duplicate_results = {index: Result(clip.name, FAILED, detail="Dateiname mehrfach gewählt, nicht geliefert")
                         for index, clip in enumerate(clips) if clip.name.casefold() in duplicate_set}
    results = list(duplicate_results.values())
    total, started, state = len(clips), time.monotonic(), "abgeschlossen"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerows([csv_header, *(result.row() for result in results)])
        write_protocol(protocol_path, header, results, total, "läuft")
        try:
            for i, clip in enumerate(clips):
                prefix = f"[{i + 1}/{total}] {clip.name}"
                if i in duplicate_results:
                    result = duplicate_results[i]
                else:
                    show = lambda text: print(f"\r{f'{prefix} | {text}':<120}", end="", flush=True)
                    show("Prüfung")
                    result = process_clip(clip, target, show)
                    results.append(result)
                    writer.writerow(result.row())
                    file.flush()
                    write_protocol(protocol_path, header, results, total, "läuft")
                print(f"\r{f'{prefix} | {result.status}':<120}" + (f"\n    {result.detail}" if result.detail else ""))
        except KeyboardInterrupt:
            state = "ABGEBROCHEN"
    write_protocol(protocol_path, header, results, total, state)
    counts = Counter(result.status for result in results)
    print(f"\n{state} nach {_hms(time.monotonic() - started)}: " + " | ".join(f"{status} {counts[status]}" for status in report_order if counts[status]))
    problems = sorted((result for result in results if result.status not in delivered),
                      key=lambda result: (report_order.index(result.status), result.name.casefold()))
    n = len(problems)
    repaired, copied = counts[REPAIRED], counts[COPIED]
    delivered_count = repaired + copied
    prefix = "Abgebrochen. " if state != "abgeschlossen" else ""
    summary = prefix + f"{delivered_count} von {total} Clips im Ausgabeordner ({repaired} repariert, {copied} unverändert)."
    if problems:
        summary += f"\n\nNICHT GELIEFERT ({n}):\n" + "\n".join(
            f"{result.name}: {result.status}" for result in problems[:15])
        if n > 15:
            summary += f"\n… und {n - 15} weitere"
    summary += f"\n\nBericht: {protocol_path}"
    show_message(app_name, summary, bool(problems) or state != "abgeschlossen")
    return 1 if problems or state != "abgeschlossen" else 0


# --------- EXEC ---------
if __name__ == "__main__":
    sys.exit(main())
