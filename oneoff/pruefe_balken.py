"""One-off check of MP4 files for black bars on all four sides."""

# --------- IMPORTS ---------
import configparser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

# --------- CONFIG ---------
app_name = "Prüfe Balken"
app_version = "0.1.0"
sample_count = 20
min_valid_frames = 5
flat_frame_std = 2.0
min_bar_px = 4
width_tolerance_px = 4
width_tolerance_share = 0.01
level_tolerance = 4.0
line_std_max = 4.0
line_change_max = 4.0
max_bar_share = 0.4
config_name = "pruefe_balken.ini"
probe_timeout = 60
frame_timeout = 60
base_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
bundle_dir = Path(getattr(sys, "_MEIPASS", base_dir))

# --------- INIT ---------
_tool_cache: dict[str, str] = {}

# --------- FUNC DETECT ---------
def valid_frames(frames: np.ndarray) -> np.ndarray:
    """Return frames whose whole-image standard deviation exceeds the flatness threshold."""
    frames = np.asarray(frames, dtype=np.float32)
    return frames[np.std(frames, axis=(1, 2)) > flat_frame_std]


def line_stats(frames: np.ndarray, axis: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Summarize each row or column's level, temporal change, and within-line spread."""
    frames = np.asarray(frames, dtype=np.float32)
    if axis == "rows":
        band = frames[:, :, frames.shape[2] // 4:frames.shape[2] - frames.shape[2] // 4]
        line_levels = np.mean(band, axis=2)
        line_spreads = np.std(band, axis=2)
    elif axis == "cols":
        band = frames[:, frames.shape[1] // 4:frames.shape[1] - frames.shape[1] // 4, :]
        line_levels = np.mean(band, axis=1)
        line_spreads = np.std(band, axis=1)
    else:
        raise ValueError("axis must be 'rows' or 'cols'")
    return (
        np.mean(line_levels, axis=0),
        np.max(line_levels, axis=0) - np.min(line_levels, axis=0),
        np.median(line_spreads, axis=0),
    )


def edge_width(level: np.ndarray, change: np.ndarray, spread: np.ndarray, reverse: bool = False) -> int:
    """Count consistent lines from one edge until the first non-matching line."""
    size = len(level)
    for width in range(size):
        index = size - width - 1 if reverse else width
        if (
            change[index] > line_change_max
            or spread[index] > line_std_max
            or abs(level[index] - level[0 if not reverse else size - 1]) > level_tolerance
        ):
            return width
    return size


def bar_pair(level: np.ndarray, change: np.ndarray, spread: np.ndarray) -> dict | None:
    """Return matching edge widths and levels when both sides satisfy the bar criteria."""
    first = edge_width(level, change, spread)
    second = edge_width(level, change, spread, reverse=True)
    size = len(level)
    tolerance = max(width_tolerance_px, round(size * width_tolerance_share))
    if (
        first < min_bar_px
        or second < min_bar_px
        or first > size * max_bar_share
        or second > size * max_bar_share
        or abs(first - second) > tolerance
    ):
        return None
    level_first = float(np.mean(level[:first]))
    level_second = float(np.mean(level[size - second:]))
    if abs(level_first - level_second) > level_tolerance:
        return None
    return {
        "first": first,
        "second": second,
        "level_first": round(level_first, 1),
        "level_second": round(level_second, 1),
    }


def rate_frames(frames: np.ndarray) -> dict:
    """Classify the detected bars across the supplied grayscale frames."""
    frames = valid_frames(frames)
    valid = len(frames)
    if valid < min_valid_frames:
        return {"state": "nicht prüfbar", "valid": valid}
    top_bottom = bar_pair(*line_stats(frames, "rows"))
    left_right = bar_pair(*line_stats(frames, "cols"))
    if top_bottom is not None and left_right is not None:
        state = "betroffen"
    elif top_bottom is not None:
        state = "nur oben/unten"
    elif left_right is not None:
        state = "nur links/rechts"
    else:
        state = "keine Balken"
    return {"state": state, "valid": valid, "top_bottom": top_bottom, "left_right": left_right}


# --------- FUNC TOOLS ---------
def _find_tool(name: str) -> str:
    if name in _tool_cache:
        return _tool_cache[name]
    for folder in (base_dir / "res", bundle_dir / "res"):
        candidates = [folder / name]
        if os.name == "nt":
            candidates.append(folder / f"{name}.exe")
        for candidate in candidates:
            if candidate.is_file():
                _tool_cache[name] = str(candidate)
                return _tool_cache[name]
    found = shutil.which(name)
    if found:
        _tool_cache[name] = found
        return found
    raise RuntimeError(f"{name} nicht gefunden. Lege das Programm in res ab oder installiere es im PATH.")


def _run(command: list[str], timeout: float) -> bytes:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Zeitlimit {timeout:g} s überschritten: {Path(command[0]).stem}") from exc
    if completed.returncode != 0:
        lines = [line for line in completed.stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
        detail = " | ".join(lines[-3:]) if lines else "keine stderr-Ausgabe"
        raise RuntimeError(
            f"{Path(command[0]).stem} fehlgeschlagen (Exit-Code {completed.returncode}): {detail}"
        )
    return completed.stdout


# --------- FUNC FILES ---------
def read_config(path: Path) -> Path:
    if not path.is_file():
        raise ValueError(f"{path.name} fehlt in {path.parent}: anlegen mit [paths] und pruef_path.")
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(path, encoding="utf-8-sig")
    except configparser.Error as exc:
        raise ValueError(f"{path.name} nicht lesbar: {exc}") from exc
    folder = config.get("paths", "pruef_path", fallback="").strip().strip('"')
    if not folder:
        raise ValueError(f"{path.name}: [paths] pruef_path fehlt oder ist leer.")
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise ValueError(f"Prüfpfad nicht gefunden: {folder}")
    return folder_path


def mp4_files(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() == ".mp4"),
        key=lambda path: path.name.casefold(),
    )


def probe(path: Path) -> tuple[int, int, float]:
    data = json.loads(_run([
        _find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
    ], probe_timeout))
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("kein Videostream")
    duration_value = data.get("format", {}).get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        raise RuntimeError("Dauer unbekannt") from None
    if duration <= 0:
        raise RuntimeError("Dauer unbekannt")
    stream = streams[0]
    return int(stream["width"]), int(stream["height"]), duration


def sample_times(duration: float, count: int = sample_count) -> list[float]:
    return [(index + 0.5) * duration / count for index in range(count)]


def read_frame(path: Path, time: float, width: int, height: int) -> np.ndarray | None:
    data = _run([
        _find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{time:.3f}",
        "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", "format=gray",
        "-f", "rawvideo", "pipe:1",
    ], frame_timeout)
    if len(data) != width * height:
        return None
    return np.frombuffer(data, np.uint8).reshape(height, width)


def check_file(path: Path) -> dict:
    try:
        width, height, duration = probe(path)
        frames = [
            frame for sample_time in sample_times(duration)
            if (frame := read_frame(path, sample_time, width, height)) is not None
        ]
        if not frames:
            return {"state": "nicht prüfbar", "valid": 0}
        return rate_frames(np.stack(frames))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {"state": "Fehler", "reason": str(exc)}


# --------- FUNC OUTPUT ---------
def format_line(name: str, result: dict) -> str:
    line = f"{result['state']:<17}{name}"
    for key, label in (("top_bottom", "oben/unten"), ("left_right", "links/rechts")):
        value = result.get(key)
        if value is not None:
            line += (
                f" | {label} {value['first']}/{value['second']} px, "
                f"Pegel {value['level_first']}/{value['level_second']}"
            )
    if result["state"] == "Fehler":
        line += f" | {result['reason']}"
    elif result["state"] == "nicht prüfbar":
        line += f" | gültige Bilder {result['valid']}"
    return line


def summary_lines(results: list[dict]) -> list[str]:
    lines = [
        f"Geprüft: {len(results)} MP4-Dateien",
        f"Betroffen (Balken an allen vier Seiten): {sum(result['state'] == 'betroffen' for result in results)}",
    ]
    untested = sum(result["state"] in ("nicht prüfbar", "Fehler") for result in results)
    if untested:
        lines.append(f"Nicht geprüft (nicht prüfbar oder Fehler): {untested}")
    return lines


# --------- MAIN ---------
def main() -> int:
    print(f"{app_name} {app_version}")
    try:
        folder = read_config(base_dir / config_name)
    except ValueError as exc:
        print(f"Fehler: {exc}")
        return 1
    files = mp4_files(folder)
    count = len(files)
    print(f"Prüfordner: {folder}, {count} MP4-Dateien")
    results = []
    try:
        for index, path in enumerate(files, start=1):
            result = check_file(path)
            results.append(result)
            print(f"[{index}/{count}] " + format_line(path.name, result))
    except KeyboardInterrupt:
        print(f"Abgebrochen nach {len(results)} von {count} Dateien.")
        print(*summary_lines(results), sep="\n")
        return 1
    print()
    print(*summary_lines(results), sep="\n")
    return 0


def run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("Abgebrochen.")
        return 1
    except Exception as exc:
        print(f"Unerwarteter Fehler: {exc}")
        return 1


# --------- EXEC ---------
if __name__ == "__main__":
    code = run()
    if getattr(sys, "frozen", False):
        try:
            input("Enter zum Beenden …")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
