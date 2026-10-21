# --------- IMPORTS ---------
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from functools import cache
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from toolbox import tb_write_log
import transcoder
from transcoder import _media, _presets     # ffmpeg path, timecode arithmetic and presets of the core
from transcoder._source import Source


# --------- CONFIG ---------
app_name = "One-off Transcode"
app_version = "0.2.0"

qc_list = "details_qc_2026-10-13_09-30-50-536327.txt"                               # Marathon details_qc_*.txt
qc_folder = r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon"          # proxies; searched including subfolders
dry_run = False                                                                     # True: only show what would be done, change nothing
rules = ["video_bitrate", "fps_50", "audio_stereo"]                                 # active rules, see USE CASE
max_video_mbit = 11                                                                 # video_bitrate: above this the video is encoded again
wrong_fps, target_fps = 50, 25                                                      # fps_50: frame rate found, frame rate of the new file
proxy_preset = "h264_10mbit_1080p"                                                  # encoder values for new video and new audio
duration_tolerance = 0.5                                                            # seconds the new file may differ from the original
report_folder = "reports"                                                           # result list; relative to this file
done_lists = ["one-off_transcode_2026-10-20_19-17-04.txt"]                          # earlier result lists; OK clips are not rendered again

# Exe: files next to the exe; the exe content is unpacked to sys._MEIPASS.
base_path = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
bundle_path = Path(getattr(sys, "_MEIPASS", base_path))
main_log = base_path / "one-off_transcode.log"


# --------- STATIC ---------
part_suffix = ".one-off.part"
entry_pattern = re.compile(r"#(\d+)\s+(\S+)\s+(.*)")
row_pattern = re.compile(r" {4}(\S+)\s+(.*)")
error_pattern = re.compile(r"(\S+?\(.*?\)): (\w+) (.*)")
ist_pattern = re.compile(r"Stream (\d+): (\w+=[^,]*(?:, \w+=[^,]*)*)")
value_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*([kM]?)")
units = {"": 1, "k": 1e3, "M": 1e6}
count_labels = {"ok": "ersetzt", "geplant": "geplant (Testlauf)", "fehler": "mit Fehler", "übersprungen": "übersprungen",
                "ohne Regel": "ohne passende Regel"}
started = datetime.now()


# --------- INIT ---------
tb_write_log(main_log, f"{app_name} {app_version} started.")


# --------- FUNC CONSOLE ---------
_progress_length = 0


def say(text: str) -> None:
    """Console line with time, replacing the progress line, and the same line in the log."""
    global _progress_length
    print(f"\r{' ' * _progress_length}\r{datetime.now():%H:%M:%S} {text}", flush=True)
    _progress_length = 0
    tb_write_log(main_log, text)


def _progress(text: str) -> None:
    global _progress_length
    # A wrapped line cannot be replaced with \r.
    line = f"{datetime.now():%H:%M:%S} {text}"[:max(20, shutil.get_terminal_size().columns - 1)]
    print(f"\r{line:<{_progress_length}}", end="", flush=True)
    _progress_length = len(line)


def _fps_text(value) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".") if value else "?"


# --------- FUNC FILES ---------
def _local(name: str) -> Path:
    """Relative file next to the exe or script, else inside the exe; absolute paths stay as they are."""
    return next((path for path in (base_path / name, bundle_path / name) if path.is_file()), base_path / name)


# --------- FUNC QC LIST ---------
@dataclass
class Entry:
    """One entry of the Marathon QC list: '#<n>  <STATUS>  <title>' and its labelled rows."""
    number: int
    status: str
    title: str
    rows: dict[str, list[str]] = field(default_factory=dict)

    def row(self, label: str) -> str:
        return (self.rows.get(label) or [""])[0]

    @property
    def name(self) -> str:
        return self.row("Ergebnis").split(":", 1)[0].strip()

    def errors(self) -> list[tuple[str, str, str]]:
        """(tool, criterion, text) per 'Fehler' row."""
        return [match.groups() for text in self.rows.get("Fehler", []) if (match := error_pattern.match(text))]

    def streams(self) -> dict[int, dict[str, str]]:
        """Values of the 'Ist' rows of stream_specs per stream number."""
        found = {}
        for match in (ist_pattern.fullmatch(text) for text in self.rows.get("Ist", [])):
            if match:
                found.setdefault(int(match[1]), {}).update(item.split("=", 1) for item in match[2].split(", "))
        return found


def _read_qc(path: Path) -> list[Entry]:
    entries = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if match := entry_pattern.fullmatch(line):
            entries.append(Entry(int(match[1]), match[2], match[3].strip()))
        elif entries and (match := row_pattern.fullmatch(line)):
            entries[-1].rows.setdefault(match[1], []).append(match[2].strip())
    return entries


def _latest(entries: list[Entry]) -> list[Entry]:
    """Latest REJECTED entry per file name, in the order of the list."""
    chosen = {}
    for entry in (entry for entry in entries if entry.status == "REJECTED" and entry.name):
        key = entry.name.casefold()
        if key not in chosen or (entry.row("Zeit"), entry.number) >= (chosen[key].row("Zeit"), chosen[key].number):
            chosen[key] = entry
    return sorted(chosen.values(), key=lambda entry: entry.number)


def _done_lists() -> list[Path]:
    """Configured result lists and all result lists in report_folder, so a restart skips them too."""
    reports = sorted((base_path / report_folder).glob("one-off_transcode_*.txt"))
    return list(dict.fromkeys([*map(_local, done_lists), *reports]))


def _done_names(paths: list[Path]) -> set[str]:
    """File names with status OK; result lists use the entry layout of the QC list, the title is the file name."""
    return {entry.title.casefold() for path in paths for entry in _read_qc(path) if entry.status == "OK"}


def _value(text: str) -> float | None:
    """Number of a QC value like '16.02M', '193.4k' or '50'."""
    match = value_pattern.fullmatch(str(text).strip())
    return float(match[1]) * units[match[2]] if match else None


def _qc_video(entry: Entry) -> dict[str, str]:
    return next((stream for stream in entry.streams().values() if "fps" in stream), {})


def _qc_audio(entry: Entry) -> list[dict[str, str]]:
    return [stream for stream in entry.streams().values() if "channels" in stream]


# --------- USE CASE ---------
# A rule gets the QC entry and source(), which probes the file on first use; the QC entry decides whether the rule
# applies, the file confirms it. Return None (not applicable), a reason against any change, or the correction:
# {"video": {} | {"fps": Fraction}} encodes the video again, {"audio": [left, right]} builds one stereo track.

def video_bitrate(entry: Entry, source: Callable[[], Source]) -> dict | str | None:
    found = _value(_qc_video(entry).get("bitrate", ""))
    if not found or found <= max_video_mbit * 1e6:
        return None
    actual = _media.number(source().video.data.get("bit_rate")) if source().video else None
    if actual is not None and actual <= max_video_mbit * 1e6:
        return f"Video laut QC {found / 1e6:.2f} Mbit/s, Datei {actual / 1e6:.2f} Mbit/s"
    return {"video": {}}


def fps_50(entry: Entry, source: Callable[[], Source]) -> dict | str | None:
    if _value(_qc_video(entry).get("fps", "")) != wrong_fps:
        return None
    if not source().fps or abs(float(source().fps) - wrong_fps) > 0.01:
        return f"laut QC {wrong_fps} fps, Datei {_fps_text(source().fps)} fps"
    return {"video": {"fps": Fraction(target_fps)}}


def audio_stereo(entry: Entry, source: Callable[[], Source]) -> dict | str | None:
    listed = [int(_value(stream["channels"]) or 0) for stream in _qc_audio(entry)]
    if not 1 <= sum(listed) <= 2 or listed == [2]:
        return None
    counts = [int(_media.number(ref.data.get("channels")) or 0) for ref in source().audio]
    if not 1 <= sum(counts) <= 2 or counts == [2]:
        return f"Ton laut QC {listed} Kanäle je Spur, Datei {counts}"
    return {"audio": [1, 2] if sum(counts) == 2 else [1, 1]}


rule_functions = {"video_bitrate": video_bitrate, "fps_50": fps_50, "audio_stereo": audio_stereo}


# --------- FUNC PLAN ---------
class _Skip(Exception):
    """File cannot be used; the text is the reason."""


@dataclass
class Plan:
    hits: list[str] = field(default_factory=list)
    video: dict | None = None               # None: copy the video
    audio: list[int] | None = None          # None: copy all audio tracks; else source channels of one stereo track

    def text(self) -> str:
        fps = (self.video or {}).get("fps")
        video = "Video übernommen" if self.video is None else f"Video neu{f' mit {fps} fps' if fps else ''}"
        return f"{video}, {'Ton übernommen' if self.audio is None else f'Ton neu als Stereo aus Kanal {self.audio}'}"


def _index(folder: Path, names: set[str]) -> dict[str, list[Path]]:
    found = {}
    for path in folder.rglob("*"):
        if path.name.casefold() in names and path.is_file():
            found.setdefault(path.name.casefold(), []).append(path)
    return found


def _source_getter(entry: Entry, index: dict[str, list[Path]]) -> Callable[[], Source]:
    @cache
    def get() -> Source:
        paths = index.get(entry.name.casefold(), [])
        if len(paths) != 1:
            raise _Skip(f"mehrfach gefunden: {', '.join(map(str, paths))}" if paths else "im QC-Ordner nicht gefunden")
        try:
            return transcoder.source(paths[0])
        except (OSError, RuntimeError, ValueError) as exc:
            raise _Skip(f"nicht lesbar: {exc}") from exc
    return get


def _plan(entry: Entry, source: Callable[[], Source]) -> Plan | str | None:
    """Merged corrections of all active rules, a reason against any change, or None without applying rule."""
    plan = Plan()
    for name in rules:
        found = rule_functions[name](entry, source)
        if isinstance(found, str):
            return f"{name}: {found}"
        if found:
            plan.hits.append(name)
            plan.video = {**(plan.video or {}), **found["video"]} if "video" in found else plan.video
            plan.audio = found.get("audio", plan.audio)
    if plan.hits and (not source().video or not source().fps):
        return "Datei ohne lesbares Video"
    return plan if plan.hits else None


# --------- FUNC FFMPEG ---------
def _timecode(source: Source, fps: Fraction) -> str | None:
    """Start TC of the file at the new frame rate; None if the file has no readable TC."""
    if source.tc_origin != "file":
        return None
    return _media.tc_text(int(_media.tc_frames(source.start_tc, source.fps) * fps / source.fps), fps)


def _video_args(video: dict) -> list[str]:
    args = ["-c:v", video["codec"], *(["-profile:v", video["profile"]] if video.get("profile") else [])]
    args += ["-preset", video.get("x264_preset", "medium")] if video["codec"] == "libx264" else []
    args += [option for key, name in (("bitrate", "-b:v"), ("maxrate", "-maxrate"), ("bufsize", "-bufsize"))
             if video.get(key) for option in (name, video[key])]
    if video.get("color") == "bt709":
        args += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    return args


def _stereo_graph(source: Source, channels: list[int], rate: int) -> str:
    left, right = (source.channels[number - 1] for number in channels)
    if left.stream is right.stream:
        return f"[0:a:{left.stream.ordinal}]pan=stereo|c0=c{left.index}|c1=c{right.index},aresample={rate}[a]"
    parts = [f"[0:a:{ref.stream.ordinal}]pan=mono|c0=c{ref.index},aresample={rate}[a{number}]"
             for number, ref in enumerate((left, right))]
    return ";".join(parts) + ";[a0][a1]join=inputs=2:channel_layout=stereo[a]"


def _command(source: Source, plan: Plan, preset: dict, part: Path, tc: str | None) -> list[str]:
    video, audio, graph, args = preset["video"], preset["audio"], [], []
    pad = f"0:v:{source.video.ordinal}"
    if plan.video is None:
        args += ["-map", pad, "-c:v", "copy"]
    else:
        filters = [f"fps={plan.video['fps']}"] if plan.video.get("fps") else []
        filters += [f"format={video['pix_fmt']}"] if video.get("pix_fmt") else []
        graph.append(f"[{pad}]{','.join(filters) or 'null'}[v]")
        args += ["-map", "[v]", *_video_args(video)]
    if plan.audio is None:
        args += ["-map", "0:a?", "-c:a", "copy"]
    else:
        rate = audio.get("sample_rate") or source.sample_rate
        graph.append(_stereo_graph(source, plan.audio, rate))
        args += ["-map", "[a]", "-c:a", "aac", "-b:a", audio.get("bitrate", "320k"), "-ar", str(rate)]
    return [_media.tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
            "-i", str(source.files[0]), *(["-filter_complex", ";".join(graph)] if graph else []), *args,
            "-map_metadata", "-1", "-map_chapters", "-1", *(["-timecode", tc, "-write_tmcd", "1"] if tc else []),
            *(["-movflags", "+faststart"] if preset.get("faststart") else []), "-f", "mp4", str(part)]


def _run(command: list[str], duration: float) -> None:
    """Run ffmpeg with a progress line; RuntimeError with the last lines of its errors."""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                   text=True, encoding="utf-8", errors="replace")
        try:
            values, last = {}, 0.0
            for line in process.stdout:
                name, _, value = line.strip().partition("=")
                values[name] = value
                if name == "progress" and time.monotonic() - last >= 0.5:
                    last = time.monotonic()
                    position = (_media.number(values.get("out_time_us")) or 0) / 1e6
                    _progress(f"{min(100.0, position / (duration or 1) * 100):5.1f} % · {_media.clock(position)} · "
                              f"Tempo {values.get('speed', '?').strip()}")
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            errors.seek(0)
            raise RuntimeError(f"ffmpeg: {_media.tail(errors.read().decode('utf-8', 'replace'))}")


# --------- FUNC CHECK AND REPLACE ---------
def _channels(source: Source) -> list[int]:
    return [int(_media.number(ref.data.get("channels")) or 0) for ref in source.audio]


def _check(source: Source, plan: Plan, part: Path, tc: str | None) -> list[str]:
    """Problems of the new file: rule not met, other tracks changed, other length or TC."""
    new, fps = transcoder.source(part), (plan.video or {}).get("fps") or source.fps
    problems = [] if new.video and new.fps and abs(float(new.fps) - float(fps)) <= 0.01 else \
        [f"{_fps_text(new.fps)} fps statt {_fps_text(fps)} fps"]
    rate = _media.number(new.video.data.get("bit_rate")) if new.video and plan.video is not None else None
    problems += [f"Video {rate / 1e6:.2f} Mbit/s"] if rate and rate > max_video_mbit * 1e6 else []
    wanted = [2] if plan.audio else _channels(source)
    problems += [] if _channels(new) == wanted else [f"Ton {_channels(new)} Kanäle je Spur statt {wanted}"]
    if abs(new.duration - source.duration) > duration_tolerance:
        problems.append(f"Dauer {new.duration:.2f} s statt {source.duration:.2f} s")
    return problems + ([f"TC {new.start_tc} statt {tc}"] if tc and new.start_tc != tc else [])


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        say(f"{path.name} | nicht löschbar: {exc}")


def _replace(source: Source, plan: Plan, preset: dict) -> None:
    """New file next to the original, checked, then moved onto it; the part file never stays."""
    target = source.files[0]
    part = target.with_name(target.name + part_suffix)
    try:
        tc = _timecode(source, (plan.video or {}).get("fps") or source.fps)
        _run(_command(source, plan, preset, part, tc), source.duration)
        if problems := _check(source, plan, part, tc):
            raise RuntimeError(f"Prüfung der neuen Datei: {'; '.join(problems)}")
        os.replace(part, target)
    finally:
        _remove(part)


# --------- FUNC RESULTS ---------
def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n\n")


def _rows(rows: list[tuple[str, object]]) -> list[str]:
    width = max(len(label) for label, _ in rows)
    return [f"    {label:<{width}}  {value}" for label, value in rows if value]


def _result(path: Path, counts: Counter, status: str, entry: Entry, rows: list[tuple[str, object]]) -> None:
    counts[status] += 1
    _write(path, [f"#{sum(counts[key] for key in counts if key != 'ohne Regel')}  {status.upper()}  {entry.name}",
                  *_rows([("QC", f"#{entry.number} {entry.title} · Job {entry.row('Job')}"), *rows])])


def _process(entry: Entry, index: dict[str, list[Path]], preset: dict, path: Path, counts: Counter) -> None:
    source = _source_getter(entry, index)
    try:
        plan = _plan(entry, source)
    except _Skip as exc:
        plan = str(exc)
    if plan is None:
        counts["ohne Regel"] += 1
        return
    if isinstance(plan, str):
        say(f"{entry.name} | übersprungen: {plan}")
        _result(path, counts, "übersprungen", entry, [("Grund", plan)])
        return
    file, rows = source().files[0], [("Datei", source().files[0]), ("Regeln", ", ".join(plan.hits)), ("Aktion", plan.text())]
    say(f"{entry.name} | {', '.join(plan.hits)}: {plan.text()}")
    if dry_run:
        _result(path, counts, "geplant", entry, rows)
        return
    begin = time.monotonic()
    try:
        _replace(source(), plan, preset)
    except (OSError, RuntimeError, ValueError) as exc:
        say(f"{entry.name} | Fehler, Original bleibt: {exc}")
        _result(path, counts, "fehler", entry, [*rows, ("Fehler", exc)])
        return
    say(f"{file.name} | ersetzt in {_media.clock(time.monotonic() - begin)}")
    _result(path, counts, "ok", entry, [*rows, ("Dauer", _media.clock(time.monotonic() - begin))])


# --------- FUNC CONFIG ---------
def _config_problems() -> list[str]:
    problems = [] if qc_list and _local(qc_list).is_file() else [f"qc_list: QC-Liste {qc_list!r} nicht gefunden"]
    problems += [f"done_lists: Ergebnisliste {name!r} nicht gefunden" for name in done_lists if not _local(name).is_file()]
    problems += [] if Path(qc_folder).is_dir() else [f"qc_folder: Ordner {qc_folder!r} nicht gefunden"]
    problems += [] if rules else ["rules ist leer"]
    if unknown := [name for name in rules if name not in rule_functions]:
        problems.append(f"rules: unbekannt {', '.join(unknown)}; erlaubt: {', '.join(rule_functions)}")
    try:
        _presets.load(proxy_preset)
    except ValueError as exc:
        problems.append(f"proxy_preset: {exc}")
    return problems


# --------- MAIN ---------
def main() -> None:
    if problems := _config_problems():
        for problem in problems:
            say(f"Einstellung ungültig: {problem}")
        return
    versions = transcoder.versions()
    version_text = f"{app_name} {app_version} · transcoder {versions['transcoder']} · ffmpeg {versions['ffmpeg']}"
    mode = "Testlauf, nichts wird geändert" if dry_run else "Dateien werden ersetzt"
    say(f"{version_text} · {mode}")
    preset = _presets.load(proxy_preset)[1]
    listed, done_paths = _latest(_read_qc(_local(qc_list))), _done_lists()
    done = _done_names(done_paths)
    entries = [entry for entry in listed if entry.name.casefold() not in done]
    done_text = f"{len(listed) - len(entries)} schon gerechnet laut {', '.join(path.name for path in done_paths)}"
    say(f"{len(listed)} abgelehnte Dateien in der QC-Liste, davon {done_text}; suche {len(entries)} im QC-Ordner …")
    index = _index(Path(qc_folder), {entry.name.casefold() for entry in entries})
    say(f"{len(index)} davon im QC-Ordner gefunden")
    path = base_path / report_folder / f"one-off_transcode_{started:%Y-%m-%d_%H-%M-%S}.txt"
    title = f"{app_name} · Ergebnisliste"
    _write(path, [title, "=" * len(title), *_rows([("Start", f"{started:%Y-%m-%d %H:%M:%S}"), ("Versionen", version_text),
                                                     ("Modus", mode), ("QC-Liste", qc_list), ("QC-Ordner", qc_folder),
                                                     ("Regeln", ", ".join(rules)), ("Erledigt", done_text)])])
    counts = Counter()
    for entry in entries:
        _process(entry, index, preset, path, counts)
    summary = "Fertig: " + ", ".join(f"{count} {count_labels[key]}" for key, count in counts.items())
    _write(path, [summary])
    say(summary)
    say(f"Ergebnisliste: {path}")


# --------- EXEC ---------
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("Abgebrochen mit Strg+C.")
    except Exception as exc:
        error_message = f"Unhandled error in main: {exc}"
        print(error_message)
        tb_write_log(main_log, error_message)
        tb_write_log(main_log, traceback.format_exc())
