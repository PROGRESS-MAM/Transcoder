"""ffmpeg/ffprobe access, number parsing and timecode arithmetic of the transcoder."""
# --------- IMPORTS ---------
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile


# --------- CONFIG ---------
res_name = "res"                    # folder with ffmpeg/ffprobe, searched before PATH
error_lines = 12                    # last lines of ffmpeg's stderr in an error text
x264_scan_bytes = 4 * 1024 * 1024   # x264 writes its options into the first frames


# --------- STATIC ---------
tc_pattern = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[:;.](\d{2})")
shift_pattern = re.compile(r"([+-]?)(\d{1,2}):(\d{2}):(\d{2})[:;.](\d{2})")
seconds_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*s?", re.IGNORECASE)
db_pattern = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:dbfs|db)?", re.IGNORECASE)
percent_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%?")
x264_pattern = re.compile(rb"x264 - core \d+[^\x00]*")
day_seconds = 24 * 3600


# --------- FUNC TOOLS ---------
def _bundle_folder() -> Path:
    # PyInstaller unpacks the exe content to sys._MEIPASS.
    return Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parents[1]


def tool_path(tool: str) -> str:
    """Path of ffmpeg or ffprobe: res folder first, then the system PATH."""
    found = shutil.which(tool, path=str(_bundle_folder() / res_name)) or shutil.which(tool)
    if not found:
        raise RuntimeError(f"{tool} weder in {_bundle_folder() / res_name} noch im Systempfad gefunden.")
    return found


def tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-error_lines:]) or "ohne Meldung"


def run_text(tool: str, args: list[str], timeout: float | None = None) -> tuple[str, str]:
    """Run ffmpeg/ffprobe and return (stdout, stderr); RuntimeError with the last lines on failure."""
    try:
        process = subprocess.run([tool_path(tool), "-hide_banner", *args], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{tool}: Zeitlimit von {timeout:g} Sekunden überschritten.") from exc
    if process.returncode:
        raise RuntimeError(f"{tool}: {tail(process.stderr)}")
    return process.stdout, process.stderr


def read_chunks(args: list[str], chunk_size: int, stderr: list[str] | None = None) -> Iterator[bytes]:
    """Yield stdout chunks; optional stderr receives the info log after the stream is fully consumed."""
    # stderr goes to a file, a full pipe would block the process.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([tool_path("ffmpeg"), "-hide_banner", "-nostdin",
                                    "-v", "info" if stderr is not None else "error", *args],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
        try:
            while chunk := process.stdout.read(chunk_size):
                yield chunk
            code = process.wait()
            if code or stderr is not None:
                errors.seek(0)
                text = errors.read().decode("utf-8", "replace")
                if stderr is not None:
                    stderr.append(text)
                if code:
                    raise RuntimeError(f"ffmpeg: {tail(text)}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


def probe(path: Path) -> dict:
    """ffprobe format, streams and chapters of one file."""
    stdout, _ = run_text("ffprobe", ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)])
    return json.loads(stdout or "{}")


def x264_options(path: Path) -> str | None:
    """Options string x264 wrote into the stream, if any."""
    try:
        with path.open("rb") as handle:
            match = x264_pattern.search(handle.read(x264_scan_bytes))
    except OSError:
        return None
    return match[0].decode("ascii", "replace") if match else None


# --------- FUNC NUMBERS ---------
def rate(value) -> Fraction | None:
    """Frame or sample rate like '25/1' as Fraction; None for missing or zero."""
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result or None


def number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse(value, pattern: re.Pattern, name: str, example: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and (match := pattern.fullmatch(value.strip())):
        return float(match[1])
    raise ValueError(f"{name}: ungültiger Wert {value!r}, erwartet z. B. {example!r}.")


def seconds(value, name: str) -> float:
    return _parse(value, seconds_pattern, name, "1s")


def decibel(value, name: str) -> float:
    return _parse(value, db_pattern, name, "-60dB")


def percent(value, name: str) -> float:
    return _parse(value, percent_pattern, name, "1%")


def clock(value: float) -> str:
    """Seconds as HH:MM:SS."""
    value = max(0, int(value))
    return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"


# --------- FUNC TIMECODE ---------
def tc_base(fps: Fraction) -> int:
    """Frames per timecode second; non drop frame, e.g. 24 for 23.976."""
    return max(1, round(fps))


def tc_frames(text: str, fps: Fraction) -> int:
    """Frame number of a timecode HH:MM:SS:FF; ValueError for invalid text."""
    match = tc_pattern.fullmatch(text.strip()) if isinstance(text, str) else None
    base = tc_base(fps)
    if not match or int(match[2]) > 59 or int(match[3]) > 59 or int(match[4]) >= base:
        raise ValueError(f"Ungültiger Timecode {text!r}, erwartet HH:MM:SS:FF mit FF < {base}.")
    hours, minutes, secs, frames = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + secs) * base + frames


def shift_frames(text: str, fps: Fraction) -> int:
    """Signed frame count of a shift like '-00:00:10:00'."""
    match = shift_pattern.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"Ungültige Verschiebung {text!r}, erwartet z. B. '-00:00:10:00'.")
    return (-1 if match[1] == "-" else 1) * tc_frames(text.strip().lstrip("+-"), fps)


def tc_text(frames: int, fps: Fraction) -> str:
    """Timecode of a frame number; wraps around 24 hours."""
    base = tc_base(fps)
    frames %= day_seconds * base
    secs, frame = divmod(frames, base)
    return f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}:{frame:02d}"


def frame_count(duration: float, fps: Fraction) -> int:
    return max(0, math.floor(duration * fps + Fraction(1, 2)))
