# --------- IMPORTS ---------
from fractions import Fraction
from pathlib import Path, PureWindowsPath
import json
import re
import sys
import traceback
from toolbox import tb_write_log
from marathon import marathon_adapter as adapter
import transcoder


# --------- CONFIG ---------
app_name = "Transcode Marathon"
app_version = "0.1.0"

root_path = Path(r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit")   # like root_path in Marathon's res/config.ini
work_dir = ".marathon"                      # like work_dir in Marathon's res/config.ini
master_dir = r"\\10.0.77.11\Ablage KI Proxy_1\++ Master repariert"   # like [ingest] master_dir; only to shorten paths
stage = "Transcode"
poll_seconds = 30
heartbeat_seconds = 30

proxy_preset = "h264_10mbit_1080p"
master_preset = "dnxhr_hqx"                 # new masters from progressive sources
master_preset_interlaced = "dnxhd_185x"     # new masters from interlaced sources
ffe_black_seconds = 1                       # black between FFE plate and film
settings = {"samples": {"full": True}, "black_bars": {"extra_crop": 10}}


# --------- STATIC ---------
checks = ["streams", "audio", "black_bars", "interlace"]
failed_steps = ("ffmpeg", "file", "preset", "timecode")    # errors a new attempt may solve; all others need a person
master_suffix = ".mov"
roles = ("Proxy", "Master")


def _run_folder() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def _parts(path) -> list[str]:
    return [part for part in re.split(r"[\\/]+", str(path)) if part]


def _short_pattern(parts: list[str], keep: str) -> re.Pattern:
    """Pattern of the folder parts and any folders after them up to keep; "\\" or "/" as separators, any case."""
    return re.compile(r"[\\/]*" + r"[\\/]+".join(map(re.escape, parts)) + r"[\\/]+(?:[^\\/]+[\\/]+)*?(?="
                      + re.escape(keep) + r"(?:[\\/]|$))", re.IGNORECASE)


main_log = _run_folder() / "transcode_marathon.log"
# Console and log lines show paths from the first work_dir and from the master_dir folder on.
short_patterns = (_short_pattern(_parts(root_path), work_dir), _short_pattern(_parts(master_dir)[:-1], _parts(master_dir)[-1]))


# --------- INIT ---------
_plates: dict[tuple[str, int, float], tuple | None] = {}    # (path, size, mtime) of an FFE plate: its format


# --------- FUNC CONSOLE ---------
def _short(text) -> str:
    text = str(text)
    for pattern in short_patterns:
        text = pattern.sub("", text)
    return text


def _console(message) -> None:
    if message.kind == "progress":
        adapter.status(_short(message.text))
    else:
        adapter.log(_short(message.text))


def _size_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"nicht lesbar ({exc})"
    return f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.1f} MB"


def _name(preset) -> str:
    return Path(str(preset)).stem             # presets may be given as path


def _fps(value) -> str:
    return f"{float(Fraction(str(value))):.3f}".rstrip("0").rstrip(".")


def _analysis_text(reports: dict) -> str:
    video, audio = reports["streams"]["video"] or {}, reports["audio"]
    return (f"Analyse: {video.get('width')}×{video.get('height')} {_fps(video.get('fps', 0))} fps, "
            f"Scan {reports['interlace']['rating']}, Ränder {reports['black_bars']['rating']}, "
            f"hörbar {', '.join(map(str, audio['audible'])) or 'keiner'} von {len(audio['channels'])} Kanälen")


# --------- FUNC JOB ---------
def _texts(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)


def _job_problem(job: dict) -> str | None:
    if not _texts(job.get("inputs")):
        return "inputs fehlt oder ist keine Liste von Pfaden"
    name = job.get("proxy_name")
    if not isinstance(name, str) or not name.strip() or PureWindowsPath(name).name != name:
        return "proxy_name fehlt oder ist kein Dateiname"
    if not isinstance(job.get("ffe_tafel"), bool):
        return "ffe_tafel fehlt oder ist nicht true/false"
    if job["ffe_tafel"] and not _texts(job.get("ffe_reference_clips")):
        return "ffe_reference_clips fehlt bei ffe_tafel true"
    return next((f"{key} fehlt" for key in ("identifier", "title") if not isinstance(job.get(key), str) or not job[key].strip()),
                None)


def _master_name(job: dict) -> str:
    name = f"{job['identifier'].strip()}__{job['title'].strip()}"
    return "".join("_" if char in adapter.invalid_name_chars else char for char in name) + master_suffix


def _fix(name: str, **target):
    """fix() with the section of settings, the common samples merged in like in analyse()."""
    if not settings:
        return transcoder.fix(name, **target)
    section = dict(settings.get(name) or {})
    if samples := {**(settings.get("samples") or {}), **(section.get("samples") or {})}:
        section["samples"] = samples
    return transcoder.fix(name, section, **target)


# --------- FUNC FFE ---------
def _format(streams: dict) -> tuple | None:
    video = streams.get("video")
    return (video.get("width"), video.get("height"), video.get("fps")) if video else None


def _plate_format(path: Path) -> tuple | None:
    """(width, height, fps) of an FFE plate; probed once per file version."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime)
    if key not in _plates:
        _plates[key] = _format(transcoder.analyse(transcoder.source(path), "streams")["streams"])
    return _plates[key]


def _plate(clips: list[str], wanted: tuple) -> Path | str:
    """The one FFE plate with size and fps of the source, or the reason for a manual check."""
    found = []
    for path in map(adapter.share_path, clips):
        try:
            if _plate_format(path) == wanted:
                found.append(path)
        except (OSError, RuntimeError, ValueError) as exc:
            adapter.log(f"FFE-Tafel nicht lesbar: {_short(path)}: {exc}")
    if len(found) == 1:
        return found[0]
    text = f"{wanted[0]}×{wanted[1]} bei {_fps(wanted[2])} fps"
    return f"mehrere FFE-Tafeln mit {text}: {', '.join(path.name for path in found)}" if found else \
        f"keine FFE-Tafel mit {text}"


# --------- FUNC RESULT ---------
def _manual(reasons: list[str], preset: str) -> adapter.JobReport:
    return adapter.JobReport("rejected", f"Manuelle Prüfung: {'; '.join(reasons)}", preset)


def _write_json(result) -> None:
    path = Path(f"{result.file}.render.json")
    try:
        path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        adapter.log(f"Protokoll nicht schreibbar: {_short(path)}: {exc}")


def _job_report(results: list, preset: str) -> adapter.JobReport:
    """ok with all outputs; else the other media files are removed, Marathon gets only complete results."""
    if all(result.state == "ok" for result in results):
        return adapter.JobReport("ok", " · ".join(f"{role} {result.summary}" for role, result in zip(roles, results)),
                                 preset, new_master=results[1].file.name if len(results) > 1 else None)
    for result in (result for result in results if result.state == "ok"):
        try:
            result.file.unlink(missing_ok=True)
            adapter.log(f"Gelöscht: {_short(result.file)}")
        except OSError as exc:
            adapter.log(f"Nicht löschbar: {_short(result.file)}: {exc}")
    errors = [(role, item) for role, result in zip(roles, results) for item in result.errors]
    text = "; ".join(f"{role} {item['step']}: {item['text']}" for role, item in errors)
    if any(item["step"] in failed_steps for _, item in errors):
        return adapter.JobReport("failed", text, preset)
    return _manual([text], preset)


# --------- FUNC USE CASE ---------
def _master_reasons(reports: dict, ffe: bool) -> list[str]:
    """Reasons for a new master: exactly 1 audible channel, black bars of a progressive source, FFE plate."""
    found = ((len(reports["audio"]["audible"]) == 1, "1 hörbarer Kanal"),
             (reports["black_bars"]["rating"] == "bars" and reports["interlace"]["rating"] == "progressive",
              "schwarze Ränder"), (ffe, "FFE-Tafel"))
    return [text for hit, text in found if hit]


def run_job(job: dict, output_folder: Path, log_path: Path) -> adapter.JobReport:
    if problem := _job_problem(job):
        return adapter.JobReport("failed", f"Job ungültig: {problem}", _name(proxy_preset))
    ffe, files = job["ffe_tafel"], [adapter.share_path(item) for item in job["inputs"]]
    adapter.log(f"Clip {job.get('clip_id', '?')}: {job['identifier']} {job['title']}, FFE-Tafel {'ja' if ffe else 'nein'}")
    for path in files:
        adapter.log(f"Eingang: {_short(path)}, {_size_text(path)}")
    try:
        source = transcoder.source(files)
    except (OSError, RuntimeError, ValueError) as exc:
        return adapter.JobReport("failed", f"Source nicht lesbar: {exc}", _name(proxy_preset))
    reports = transcoder.analyse(source, checks, settings, _console)
    if problems := [f"Analyse {name}: {report['error']}" for name, report in reports.items() if "error" in report]:
        return _manual(problems, _name(proxy_preset))
    adapter.log(_analysis_text(reports))
    merge, code = [], None
    if ffe:
        plate = _plate(job["ffe_reference_clips"], _format(reports["streams"]))
        if isinstance(plate, str):
            return _manual([plate], _name(proxy_preset))
        adapter.log(f"FFE-Tafel: {_short(plate)}")
        merge, code = [plate, transcoder.black(ffe_black_seconds), transcoder.SOURCE], transcoder.timecode(anchor=transcoder.SOURCE)
    proxy_fixes = [_fix("audio", channels=2), _fix("black_bars"), _fix("interlace")]
    items = [transcoder.output(proxy_preset, output_folder / job["proxy_name"], proxy_fixes, merge=merge, timecode=code)]
    preset, reasons, scan = _name(proxy_preset), _master_reasons(reports, ffe), reports["interlace"]["rating"]
    if reasons and scan == "unclear":
        # The master preset depends on the scan; the proxy would fail on the same rating anyway.
        return _manual([f"neuer Master nötig ({', '.join(reasons)}), Interlacing unklar "
                        f"(trust {reports['interlace']['trust']:.2f})"], preset)
    if reasons:
        interlaced = scan == "interlaced"
        master = master_preset_interlaced if interlaced else master_preset
        fixes = [_fix("audio", min_audible=2)] + ([] if interlaced else [_fix("black_bars")])
        items.append(transcoder.output(master, output_folder / _master_name(job), fixes, merge=merge, timecode=code))
        preset = f"{preset}+{_name(master)}"
        adapter.log(f"Neuer Master {master}: {', '.join(reasons)}")
    else:
        adapter.log("Kein neuer Master")
    results = transcoder.render(source, items, _console)
    for result in (result for result in results if result.state == "ok"):
        _write_json(result)
    return _job_report(results, preset)


# --------- MAIN ---------
def main() -> None:
    versions = {"transcode_marathon": app_version, "marathon_adapter": adapter.app_version, **transcoder.versions()}
    worker = adapter.Settings(root_path, work_dir, stage, poll_seconds, heartbeat_seconds, main_log, versions)
    adapter.run(worker, run_job)


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
