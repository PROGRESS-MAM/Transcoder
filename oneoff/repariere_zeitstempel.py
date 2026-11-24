#!/usr/bin/env python3
"""Repair proxy clips with duplicate PTS at regular DTS.

Reads config.ini next to the exe (or this script), finds affected clips quickly via the MP4 index,
re-encodes their video without B-frames on a regular frame grid and publishes them in the target folder.
"""

# --------- IMPORTS ---------
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator
import argparse
import configparser
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
app_version = "1.0.0"

config_name = "config.ini"                                  # next to the exe or this script
test_clips = ("58238241.mp4", "58238527.mp4")               # repair-test = true: only these clips are processed
max_moov_bytes = 512 * 2**20
ffprobe_timeout_s = 3600


# --------- STATIC ---------
OK, DEFECT, ODD, UNREADABLE = "in Ordnung", "Fehlerbild", "anders auffällig", "nicht lesbar"
REPAIRED, PLANNED, FAILED, UNSUPPORTED, TARGET_EXISTS = (
    "repariert", "Fehlerbild (Dry-Run, nicht repariert)", "Fehler", "nicht reparierbar", "übersprungen (Ziel vorhanden)")
report_order = (REPAIRED, PLANNED, FAILED, UNSUPPORTED, ODD, UNREADABLE, TARGET_EXISTS, OK)
csv_header = ["datei", "status", "bilder", "doppelte_pts", "detail"]
config_keys = {"paths": {"source_path", "target_path"}, "repair": {"repair-test"}}
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
class Settings:
    source: Path
    target: Path
    repair_test: bool


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


# --------- FUNC: config and tools ---------
def _config_path(value: str, label: str) -> Path:
    if not value.strip():
        raise ConfigError(f"{label} ist leer")
    path = Path(value.strip())
    return (path if path.is_absolute() else base_dir / path).resolve(strict=False)


def load_settings() -> Settings:
    path = base_dir / config_name
    if not path.is_file():
        raise ConfigError(f"{config_name} fehlt: {path}")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8-sig") as f:
            parser.read_file(f)
    except (OSError, UnicodeError, configparser.Error) as err:
        raise ConfigError(f"{config_name} nicht lesbar: {err}") from err
    problems = [f"Abschnitt [{s}] fehlt" for s in config_keys if not parser.has_section(s)]
    problems += [f"Abschnitt [{s}] unbekannt" for s in parser.sections() if s not in config_keys]
    for section, keys in config_keys.items():
        found = set(parser[section]) if parser.has_section(section) else keys
        problems += [f"[{section}] {k} fehlt" for k in sorted(keys - found)] + [f"[{section}] {k} unbekannt" for k in sorted(found - keys)]
    if problems:
        raise ConfigError(f"{config_name} ungültig: " + "; ".join(problems))
    repair_test = parser["repair"]["repair-test"].strip().casefold()
    if repair_test not in ("true", "false"):
        raise ConfigError("[repair] repair-test muss true oder false sein")
    settings = Settings(_config_path(parser["paths"]["source_path"], "source_path"),
                        _config_path(parser["paths"]["target_path"], "target_path"), repair_test == "true")
    if not settings.source.is_dir():
        raise ConfigError(f"Quellordner nicht gefunden: {settings.source}")
    if not settings.target.is_dir():
        raise ConfigError(f"Zielordner nicht gefunden: {settings.target}")
    if not os.access(settings.target, os.W_OK):
        raise ConfigError(f"Zielordner nicht beschreibbar: {settings.target}")
    if os.path.normcase(str(settings.source)).casefold() == os.path.normcase(str(settings.target)).casefold():
        raise ConfigError("Quell- und Zielordner müssen verschieden sein")
    return settings


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


def process_clip(src: Path, target_dir: Path, dry_run: bool, show: Callable[[str], None]) -> Result:
    check, stamps = check_clip(src)
    result = lambda status, detail: Result(src.name, status, check.frames, check.dup, detail)
    if check.status != DEFECT:
        return result(check.status, check.detail)
    try:
        info = probe_streams(src)
        plan = plan_repair(info, stamps, check)
    except Unsupported as err:
        return result(UNSUPPORTED, f"{check.detail}; {err}")
    except (ReadError, OSError, ValueError, subprocess.SubprocessError) as err:
        return result(UNREADABLE, f"{check.detail}; {err}")
    if dry_run:
        return result(PLANNED, check.detail)
    part, final = target_dir / f"{src.name}.part", target_dir / src.name
    if os.path.lexists(part):
        return result(FAILED, f"{check.detail}; {part.name} existiert bereits, bitte prüfen und löschen")
    try:
        run_ffmpeg(repair_command(src, part, plan), lambda frame: show(f"Reparatur {frame / check.frames:.0%} ({frame}/{check.frames} Bilder)"))
        show("Prüfung der Ausgabe")
        problems, frames = verify(info, stamps, check, part)
        if problems:
            raise RepairError("Prüfung nicht bestanden: " + "; ".join(problems))
        publish(part, final)
    except (RepairError, ReadError, OSError, ValueError, subprocess.SubprocessError) as err:
        left = f"; Teildatei bleibt liegen: {part.name}" if part.exists() else ""
        return result(FAILED, f"{check.detail}; {err}{left}")
    end = "" if frames == check.frames else " (verworfener Schlussframe fehlt)"
    return result(REPAIRED, f"{check.detail}; Ausgabe {frames} Bilder{end}")


# --------- FUNC: selection and reports ---------
def select_clips(settings: Settings) -> tuple[list[Path], list[Path], list[str]]:
    """Return (candidates, sources with existing target, missing test clips); folders are read once."""
    sources = sorted((p for p in settings.source.iterdir() if p.is_file() and p.suffix.casefold() == ".mp4"),
                     key=lambda p: p.name.casefold())
    missing = []
    if settings.repair_test:
        sources = [p for p in sources if p.name.casefold() in {n.casefold() for n in test_clips}]
        missing = [n for n in test_clips if n.casefold() not in {p.name.casefold() for p in sources}]
    done = {p.name.casefold() for p in settings.target.iterdir() if p.is_file()}
    return [p for p in sources if p.name.casefold() not in done], [p for p in sources if p.name.casefold() in done], missing


def _hms(seconds: float) -> str:
    return f"{int(seconds) // 3600}:{int(seconds) % 3600 // 60:02d}:{int(seconds) % 60:02d}"


def write_protocol(path: Path, header: list[str], results: list[Result], total: int, state: str) -> None:
    lines = [*header, f"Stand: {state} | bearbeitet {len(results)} von {total} Clips", ""]
    for status in report_order:
        hits = sorted((r for r in results if r.status == status), key=lambda r: r.name.casefold())
        lines += [f"== {status} ({len(hits)}) =="] + [f"{r.name} | {r.detail}" if r.detail else r.name for r in hits] + [""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------- MAIN ---------
def main() -> int:
    parser = argparse.ArgumentParser(description=f"{app_name} {app_version}: repariert doppelte PTS bei regelmäßigen DTS.")
    parser.add_argument("--dry-run", action="store_true", help="nur prüfen, nichts schreiben außer dem Bericht")
    dry_run = parser.parse_args().dry_run
    try:
        settings = load_settings()
        tools.update(ffmpeg=find_tool("ffmpeg"), ffprobe=find_tool("ffprobe"))
        clips, existing, missing = select_clips(settings)
        report_dir = base_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
    except (ConfigError, OSError) as err:
        print(f"Abbruch: {err}", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    protocol_path = next(path for i in itertools.count()
                         if not (path := report_dir / f"reparatur_{stamp}{f'_{i}' if i else ''}.txt").exists())
    csv_path = protocol_path.with_suffix(".csv")
    mode = ("Test (nur " + ", ".join(test_clips) + ")" if settings.repair_test else "Serie") + (", Dry-Run" if dry_run else "")
    header = [f"{app_name} {app_version}", f"Modus: {mode}", f"Quelle: {settings.source}", f"Ziel: {settings.target}",
              f"ffmpeg: {tool_version(tools['ffmpeg'])} ({tools['ffmpeg']})", f"ffprobe: {tool_version(tools['ffprobe'])} ({tools['ffprobe']})"]
    header += [f"Testclip fehlt in der Quelle: {name}" for name in missing]
    print("\n".join(header) + f"\n{len(clips)} Clips zu prüfen, {len(existing)} mit vorhandenem Ziel\nBerichte: {protocol_path}")
    results = [Result(p.name, TARGET_EXISTS) for p in existing]
    total, started, state = len(results) + len(clips), time.monotonic(), "abgeschlossen"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerows([csv_header, *(r.row() for r in results)])
        write_protocol(protocol_path, header, results, total, "läuft")
        try:
            for i, clip in enumerate(clips, 1):
                prefix = f"[{i}/{len(clips)}] {clip.name}"
                show = lambda text: print(f"\r{f'{prefix} | {text}':<120}", end="", flush=True)
                show("Prüfung")
                result = process_clip(clip, settings.target, dry_run, show)
                results.append(result)
                writer.writerow(result.row())
                f.flush()
                write_protocol(protocol_path, header, results, total, "läuft")
                print(f"\r{f'{prefix} | {result.status}':<120}" + (f"\n    {result.detail}" if result.status not in (OK, TARGET_EXISTS) else ""))
        except KeyboardInterrupt:
            state = "ABGEBROCHEN"
    write_protocol(protocol_path, header, results, total, state)
    counts = Counter(r.status for r in results)
    print(f"\n{state} nach {_hms(time.monotonic() - started)}: " + " | ".join(f"{s} {counts[s]}" for s in report_order if counts[s]))
    return 1 if counts[FAILED] or state != "abgeschlossen" else 0


# --------- EXEC ---------
if __name__ == "__main__":
    sys.exit(main())
