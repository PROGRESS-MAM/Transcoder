"""One-off repair of MP4 proxies with black bars on all four sides."""

# --------- IMPORTS ---------
from datetime import datetime
from fractions import Fraction
import json
import os
from pathlib import Path
import sys

import pruefe_balken as pb

# --------- CONFIG ---------
app_name = "Repariere Balken"
app_version = "0.1.0"
source_folders = tuple(Path(item) for item in (
    r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA",
    r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\Doclights",
    r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\Historiathek",
    r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\Katholisches Filmwerk",
    r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\LYNXarchive",
))
encode_timeout_factor = 2.0
encode_timeout_extra = 300
probe_timeout = 60
nvenc_timeout = 30
duration_tolerance = 0.5
part_suffix = ".part"
log_name = "repariere_balken.log"
log_path = pb.base_dir / log_name

# --------- STATIC ---------
encoder_args = (
    "-c:v", "h264_nvenc", "-profile:v", "high", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
    "-b:v", "10M", "-maxrate", "10.5M", "-bufsize", "20M",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
    "-field_order", "progressive", "-fps_mode:v", "passthrough",
)

# --------- FUNC GEOMETRY ---------
def _even(value: Fraction) -> int:
    return max(2, round(value / 2) * 2)


def crop_box(width: int, height: int, top: int, bottom: int, left: int, right: int) -> tuple[int, int, int, int]:
    active_height = height - top - bottom
    active_width = width - left - right
    target = active_height * width // height
    crop_width = max(active_width, min(width, target))
    x = min(max(left - (crop_width - active_width) // 2, 0), width - crop_width)
    return x, top, crop_width, active_height


def picture_size(crop_width: int, crop_height: int, width: int, height: int) -> tuple[int, int]:
    scale = min(Fraction(width, crop_width), Fraction(height, crop_height))
    return min(width, _even(crop_width * scale)), min(height, _even(crop_height * scale))


def video_filter(box: tuple[int, int, int, int], width: int, height: int) -> str:
    x, y, crop_width, crop_height = box
    scaled_width, scaled_height = picture_size(crop_width, crop_height, width, height)
    result = (
        f"crop={crop_width}:{crop_height}:{x}:{y}:exact=1,"
        f"scale={scaled_width}:{scaled_height}:flags=lanczos:"
        "in_color_matrix=auto:out_color_matrix=bt709"
    )
    if (scaled_width, scaled_height) != (width, height):
        result += f",pad={width}:{height}:{(width - scaled_width) // 4 * 2}:{(height - scaled_height) // 4 * 2}:color=black"
    return f"{result},setsar=1/1,format=yuv420p"

# --------- FUNC ENCODE ---------
def encode_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    box: tuple[int, int, int, int],
    width: int,
    height: int,
    timecode: str | None,
) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-vf", video_filter(box, width, height),
        *encoder_args, "-c:a", "copy", "-map_metadata", "0",
        *( ["-timecode", timecode] if timecode else [] ),
        "-write_tmcd", "1", "-movflags", "+faststart", "-f", "mp4", str(target),
    ]


def encode_timeout(duration: float) -> float:
    return duration * encode_timeout_factor + encode_timeout_extra


# --------- INIT ---------
_log_failed = False

# --------- FUNC FILES ---------
def stream_info(path: Path) -> dict:
    data = json.loads(pb._run([
        pb._find_tool("ffprobe"), "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height:stream_tags=timecode:format=duration:format_tags=timecode",
        "-of", "json", str(path),
    ], probe_timeout))
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = sum(item.get("codec_type") == "audio" for item in streams)
    format_data = data.get("format", {})
    duration_value = format_data.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = 0.0
    timecode = format_data.get("tags", {}).get("timecode")
    if timecode is None:
        timecode = next((
            item.get("tags", {}).get("timecode") for item in streams
            if item.get("tags", {}).get("timecode") is not None
        ), None)
    return {
        "video": video.get("codec_name"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "audio": audio,
        "duration": duration,
        "timecode": timecode,
    }


def check_nvenc() -> None:
    pb._run([
        pb._find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
        "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25",
        "-frames:v", "1", *encoder_args, "-f", "null", "-",
    ], nvenc_timeout)


# --------- FUNC REPAIR ---------
def verify_output(part: Path, source: dict) -> None:
    info = stream_info(part)
    problems = []
    if info["video"] != "h264":
        problems.append(f"Codec {info['video']}")
    if (info["width"], info["height"]) != (source["width"], source["height"]):
        problems.append(f"Größe {info['width']}×{info['height']}")
    if info["audio"] != source["audio"]:
        problems.append(f"Tonspuren {info['audio']}")
    if abs(info["duration"] - source["duration"]) > duration_tolerance:
        problems.append(f"Dauer {info['duration']:.2f} s")
    if source.get("timecode") is not None and info["timecode"] != source["timecode"]:
        problems.append(f"Timecode {info['timecode']}")
    if problems:
        raise RuntimeError(f"Prüfung fehlgeschlagen: {', '.join(problems)}")


def repair_file(path: Path) -> dict:
    found = pb.check_file(path)
    if found["state"] == "Fehler":
        return {"state": "Fehler", "detail": found["reason"]}
    if found["state"] != "betroffen":
        return {"state": "übersprungen", "detail": found["state"]}
    part = path.with_name(path.name + part_suffix)
    try:
        info = stream_info(path)
        top_bottom = found["top_bottom"]
        left_right = found["left_right"]
        box = crop_box(
            info["width"], info["height"], top_bottom["first"], top_bottom["second"],
            left_right["first"], left_right["second"],
        )
        if part.exists():
            part.unlink()
        pb._run(
            encode_command(pb._find_tool("ffmpeg"), path, part, box, info["width"], info["height"], info["timecode"]),
            encode_timeout(info["duration"]),
        )
        verify_output(part, info)
        os.replace(part, path)
        x, y, width, height = box
        scaled_width, scaled_height = picture_size(width, height, info["width"], info["height"])
        detail = f"Beschnitt {width}×{height} bei {x},{y} → {scaled_width}×{scaled_height}"
        if scaled_height < info["height"]:
            detail += " (oben/unten bleiben Balken)"
        return {"state": "repariert", "detail": detail}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {"state": "Fehler", "detail": str(exc)}
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


# --------- FUNC OUTPUT ---------
def log(text: str) -> None:
    global _log_failed
    print(text)
    try:
        with log_path.open("a", encoding="utf-8") as file:
            file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError as exc:
        if not _log_failed:
            print(f"Log nicht schreibbar: {log_path}: {exc}")
            _log_failed = True


def summary_lines(results: list[dict]) -> list[str]:
    return [
        f"Geprüft: {len(results)} MP4-Dateien",
        f"Repariert: {sum(result['state'] == 'repariert' for result in results)}",
        f"Übersprungen: {sum(result['state'] == 'übersprungen' for result in results)}",
        f"Fehler: {sum(result['state'] == 'Fehler' for result in results)}",
    ]


# --------- MAIN ---------
def main() -> int:
    try:
        check_nvenc()
    except RuntimeError as exc:
        log(f"{app_name} {app_version}")
        log(f"Abbruch: NVENC nicht nutzbar: {exc}")
        return 1
    log(f"{app_name} {app_version}")
    files = []
    inaccessible = 0
    folders = 0
    for folder in source_folders:
        try:
            if not folder.is_dir():
                raise OSError
            files.extend(pb.mp4_files(folder))
            folders += 1
        except OSError:
            inaccessible += 1
            log(f"Ordner nicht erreichbar: {folder}")
    count = len(files)
    log(f"{count} MP4-Dateien in {folders} Ordnern")
    results = []
    try:
        for index, path in enumerate(files, start=1):
            print(f"[{index}/{count}] prüfe {path.parent.name}\\{path.name} …")
            result = repair_file(path)
            results.append(result)
            log(f"[{index}/{count}] {result['state']:<13}{path.parent.name}\\{path.name} | {result['detail']}")
    except KeyboardInterrupt:
        log(f"Abgebrochen nach {len(results)} von {count} Dateien.")
        for line in summary_lines(results):
            log(line)
        return 1
    for line in summary_lines(results):
        log(line)
    if inaccessible:
        log(f"Ordner nicht erreichbar: {inaccessible}")
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
