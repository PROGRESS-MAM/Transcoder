# --------- IMPORTS ---------
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
import json
import shutil
import traceback
from toolbox import tb_write_log
import transcoder


# --------- CONFIG ---------
app_name = "Transcoder Sample"
app_version = "0.1.0"

mode = "analyse"                            # "analyse": write reports, render nothing; "render": render outputs
inputs = []                                 # files and/or folders
recursive = True                            # folders including subfolders
media_suffixes = (".mov", ".mxf", ".mp4", ".wav")
group_op_atom = True                        # OP-Atom files of one clip in one folder form one source
checks = ["streams", "interlace", "black_bars", "audio"]   # checks in mode "analyse"
settings = None                             # settings per check for the analysis and all fixes; None = defaults
report_folder = "reports"                   # analysis report and error list; relative to this file

outputs = [                                 # outputs per source in mode "render"; {stem} = name of the source
    {"preset": "h264_10mbit_1080p", "file": "D:/out/web/{stem}_10Mbit.mp4",
     "fixes": [transcoder.fix("audio", channels=2), transcoder.fix("black_bars"), transcoder.fix("interlace")]},
    {"preset": "dnxhr_hqx", "file": "D:/out/edit/{stem}.mov",
     "fixes": [transcoder.fix("audio", min_audible=2)]},
]

source_settings = {}                        # per {stem}: "tc", "modify", "outputs" (indices into outputs)

base_path = Path(__file__).resolve().parent
main_log = base_path / "sample.log"


# --------- USE CASE ---------
decide_checks = []                          # checks decide() needs; empty = no analysis before rendering


def decide(source, reports: dict, outputs: list[dict]) -> list[dict] | str:
    """Use case logic: entries of outputs to render for this source, or a reason to put it on the error list.

    reports: {check: report} of decide_checks. outputs: entries for this source, {stem} already filled in.
    """
    return outputs


# --------- STATIC ---------
modes = ("analyse", "render")
output_keys = ("preset", "file", "fixes", "modify", "merge", "timecode")
source_keys = ("tc", "modify", "outputs")
invalid_name_chars = frozenset('<>:"/\\|?*' + "".join(map(chr, range(32))))
tc_origins = {"tc": "Einstellung", "file": "aus der Datei", "default": "Standard, Datei ohne TC"}
bar_sides = (("top", "oben"), ("bottom", "unten"), ("left", "links"), ("right", "rechts"))
count_labels = {"analysiert": "analysiert", "ok": "Outputs ok", "error": "Outputs mit Fehler", "aussortiert": "aussortiert",
                "fehler": "Sources mit Fehler", "übersprungen": "übersprungen"}
started = datetime.now()


# --------- INIT ---------
tb_write_log(main_log, f"{app_name} {app_version} started.")


# --------- FUNC CONSOLE ---------
_progress_length = 0


def say(text: str) -> None:
    """Console line with time, replacing the progress line, and the same line in sample.log."""
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


def on_message(message) -> None:
    if message.kind == "progress":
        _progress(message.text)
    else:
        say(message.text)


# --------- FUNC TEXT FILES ---------
def _empty(value) -> bool:
    return value is None or value is False or value == "" or value == []


def _join(*parts) -> str:
    return " · ".join(str(part) for part in parts if not _empty(part))


def _head(title: str, rows: list[tuple[str, object]]) -> list[str]:
    width = max((len(label) for label, _ in rows), default=0)
    return [title, "=" * len(title), *(f"{label:<{width}}  {value}" for label, value in rows), ""]


def _block(number: int, status: str, title: str, facts: list[tuple[str, object]],
           rows: list[tuple[str, object]]) -> list[str]:
    """Entry '#<n>  <STATUS>  <title>', a fact line 'label value · …' and labelled rows; empty values are left out."""
    lines = [f"#{number}  {status.upper()}  {title}".rstrip()]
    if found := [f"{label} {value}" for label, value in facts if not _empty(value)]:
        lines.append(f"    {' · '.join(found)}")
    rows = [(label, value) for label, value in rows if not _empty(value)]
    width = max([8, *(len(label) for label, _ in rows)])
    return [*lines, *(f"    {label:<{width}} {value}" for label, value in rows), ""]


@dataclass
class _TextFile:
    """Text report, written with the first entry and rewritten after each further entry."""
    path: Path
    title: str
    rows: list[tuple[str, object]]
    state: str = ""
    entries: list[list[str]] = field(default_factory=list)

    def add(self, status: str, title: str, facts: list[tuple[str, object]], rows: list[tuple[str, object]]) -> None:
        self.entries.append(_block(len(self.entries) + 1, status, title, facts, rows))
        self.write()

    def write(self) -> None:
        if not self.entries:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = _head(self.title, [*self.rows, ("Stand", self.state)]) + [line for entry in self.entries for line in entry]
        self.path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _facts(files: list[Path]) -> list[tuple[str, object]]:
    return [("Ordner", files[0].parent), ("Dateien", ", ".join(path.name for path in files))]


# --------- FUNC ANALYSIS TEXT ---------
def _clock(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    minutes, rest = divmod(float(seconds), 60)
    return f"{int(minutes // 60):02d}:{int(minutes % 60):02d}:{rest:06.3f}"


def _fps(value) -> str:
    try:
        return f"{float(Fraction(str(value))):.3f}".rstrip("0").rstrip(".")
    except (ValueError, ZeroDivisionError):
        return str(value)


def _mbit(value) -> str | None:
    try:
        return f"{float(value) / 1e6:.1f} Mbit/s"
    except (TypeError, ValueError):
        return None


def _codec(data: dict) -> str:
    return " ".join(str(data[key]) for key in ("codec_name", "profile") if data.get(key))


def _db(value: float, sign: str = "") -> str:
    return f"{value + 0.0:{sign}.1f}"           # + 0.0 turns -0.0 into 0.0


def _streams_rows(report: dict) -> list[tuple[str, object]]:
    rows = [("TC", f"{report['start_tc']} ({tc_origins.get(report['tc_origin'], report['tc_origin'])})"),
            ("Dauer", _clock(report["duration"]))]
    for item in report["files"]:
        clip = item.get("material_package_name")
        rows += [("Datei", _join(Path(item["file"]).name, item["container"], _mbit(item["bit_rate"]),
                                 item["op_atom"] and "OP-Atom", clip and f"Clipname {clip}")),
                 ("Encoder", item["encoder"]), ("x264", item["x264"])]
    if video := report["video"]:
        rows.append(("Video", _join(_codec(video), f"{video.get('width')}×{video.get('height')}",
                                    f"SAR {video.get('sample_aspect_ratio', '–')}", f"DAR {video.get('display_aspect_ratio', '–')}",
                                    f"{_fps(video['fps'])} fps", report["field_order"] or "Scan ohne Angabe",
                                    video.get("pix_fmt"), _mbit(video.get("bit_rate")))))
    for number, audio in enumerate(report["audio"], 1):
        bits, count = audio.get("bits_per_raw_sample"), audio.get("channels")
        rows.append((f"Audio {number}", _join(_codec(audio), f"{audio.get('sample_rate')} Hz",
                                              f"{count} {'Kanal' if count == 1 else 'Kanäle'}", audio.get("channel_layout"),
                                              f"{bits} bit" if bits and bits != "0" else audio.get("sample_fmt"))))
    return rows


def _interlace_rows(report: dict) -> list[tuple[str, object]]:
    agrees = {True: "Messung bestätigt Metadaten", False: "Messung widerspricht Metadaten"}.get(report.get("agrees"))
    rows = [("Scan", _join(report["rating"], f"trust {report['trust']:.2f}", report.get("field_order"),
                           f"Metadaten {report['metadata'] or 'ohne Angabe'}", agrees))]
    if frames := report.get("frames"):
        rows.append(("Frames", _join(*(f"{name} {count}" for name, count in frames.items()),
                                     f"interlaced in {report['votes']} von {report['voters']} Stichproben")))
    return rows


def _bars_rows(report: dict) -> list[tuple[str, object]]:
    level = report.get("black_level")
    rows = [("Ränder", _join(report["rating"], f"trust {report['trust']:.2f}",
                             level is not None and f"Schwarzpegel {level:g}", report.get("reason")))]
    if bars := report.get("bars"):
        x, y, width, height = report["active"]
        rows += [("Fläche", _join(f"aktiv {width}×{height} bei {x},{y}", *(f"{label} {bars[key]}" for key, label in bar_sides))),
                 ("Proben", _join(f"{report['samples']} Stichproben", f"{report['valid_samples']} gültig",
                                  f"{report['black_samples']} schwarz"))]
    return rows


def _audio_rows(report: dict) -> list[tuple[str, object]]:
    duplicates = "; ".join("/".join(map(str, pair)) for pair in report["duplicates"])
    rows = [("Ton", _join(f"hörbar {', '.join(map(str, report['audible'])) or 'keiner'}",
                          duplicates and f"doppelt {duplicates}", f"trust {report['trust']:.2f}"))]
    several = len({channel["file"] for channel in report["channels"]}) > 1
    for channel in report["channels"]:
        peak = f"Spitze {_db(channel['peak_db'])} dBFS" if channel["peak_db"] is not None else "digital still"
        rows.append((f"Kanal {channel['channel']}", _join("hörbar" if channel["audible"] else "still",
                                                          f"Ton {channel['active']:g} %", peak, f"trust {channel['trust']:.2f}",
                                                          several and channel["file"], f"Spur {channel['stream'] + 1}")))
    for pair in report["pairs"]:
        found = pair["similarity"] is not None
        rows.append((f"Paar {pair['channels'][0]}/{pair['channels'][1]}",
                     _join("doppelt" if pair["duplicate"] else "verschieden",
                           found and f"Ähnlichkeit {pair['similarity']:.3f}", found and f"Versatz {pair['offset_ms']:g} ms",
                           found and f"Pegel {_db(pair['level_db'], '+')} dB", f"trust {pair['trust']:.2f}",
                           f"{pair['blocks']} Vergleiche")))
    return rows


check_rows = {"streams": _streams_rows, "interlace": _interlace_rows, "black_bars": _bars_rows, "audio": _audio_rows}


def _analysis_rows(reports: dict) -> list[tuple[str, object]]:
    rows = []
    for check, report in reports.items():
        rows += [("Fehler", f"{check}: {report['error']}")] if "error" in report else check_rows[check](report)
    return rows


# --------- FUNC SOURCES ---------
def _media_files() -> list[Path]:
    suffixes, found = {suffix.casefold() for suffix in media_suffixes}, []
    for item in map(Path, inputs):
        if item.is_dir():
            found += sorted(path for path in (item.rglob("*") if recursive else item.glob("*"))
                            if path.is_file() and path.suffix.casefold() in suffixes)
        else:
            found.append(item)
    return list(dict.fromkeys(found))


def _mxf_data(path: Path) -> dict | str:
    """File data of the streams check, or the reason why the file cannot be read."""
    try:
        report = transcoder.analyse(transcoder.source(path), "streams")["streams"]
    except (OSError, RuntimeError, ValueError) as exc:
        return f"nicht lesbar: {exc}"
    return report["files"][0] if "files" in report else f"nicht lesbar: {report.get('error')}"


def _file_name(text: str) -> str:
    return "".join("_" if char in invalid_name_chars else char for char in text).strip().rstrip(". ")


def _op_atom_source(members: list[tuple[Path, dict]]) -> tuple[str, list[Path], str | None]:
    def kinds(data: dict) -> set[str]:
        return {stream["type"] for stream in data["streams"]}
    members = sorted(members, key=lambda member: ("video" not in kinds(member[1]), member[0].name.casefold()))
    found = set().union(*(kinds(data) for _, data in members))
    clip = next((data["material_package_name"] for _, data in members if data.get("material_package_name")), None)
    missing = [label for kind, label in (("video", "Video"), ("audio", "Ton")) if kind not in found]
    problem = f"OP-Atom-Gruppe unvollständig: ohne {' und '.join(missing)}" if missing else None
    return _file_name(clip) if clip else members[0][0].stem, [path for path, _ in members], problem


def _sources(files: list[Path]) -> list[tuple[str, list[Path], str | None]]:
    """(stem, files, problem) per source in the order of the first file; OP-Atom files of one clip form one source."""
    slots, groups = [], {}
    for path in files:
        data = _mxf_data(path) if group_op_atom and path.suffix.casefold() == ".mxf" else None
        if isinstance(data, dict) and data["op_atom"]:
            key = (path.parent, data["material_package_umid"] or path.name)
            if key not in groups:
                groups[key] = []
                slots.append(groups[key])
            groups[key].append((path, data))
        else:
            slots.append((path.stem, [path], data if isinstance(data, str) else None))
    return [_op_atom_source(slot) if isinstance(slot, list) else slot for slot in slots]


# --------- FUNC OUTPUTS ---------
def _fix_settings(name: str) -> dict:
    """Section of settings for a fix, with the common samples merged in like in analyse()."""
    section = dict(settings.get(name) or {})
    if samples := {**(settings.get("samples") or {}), **(section.get("samples") or {})}:
        section["samples"] = samples
    return section


def _output(entry: dict, modify: list) -> object:
    """output() of an entry; fixes without own settings get settings, changes of the source come last."""
    # Anything but fix() keeps its place; output() names it in its error.
    fixes = [transcoder.fix(item.name, _fix_settings(item.name), **item.target)
             if settings and getattr(item, "settings", "") is None else item for item in entry.get("fixes", ())]
    return transcoder.output(**{**entry, "fixes": fixes, "modify": [*entry.get("modify", ()), *modify]})


def _write_json(result) -> None:
    path = Path(f"{result.file}.render.json")
    try:
        path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        say(f"{path.name} | nicht schreibbar: {exc}")


def _render(source, stem: str, files: list[Path], errors: _TextFile, counts: Counter) -> None:
    own = source_settings.get(stem, {})
    entries = [{**outputs[index], "file": str(outputs[index]["file"]).replace("{stem}", stem)}
               for index in own.get("outputs", range(len(outputs)))]
    reports = transcoder.analyse(source, decide_checks, settings, on_message) if decide_checks else {}
    try:
        chosen = decide(source, reports, entries)
        items = None if isinstance(chosen, str) else [_output(entry, own.get("modify", [])) for entry in chosen]
    except Exception as exc:  # use case code must not stop the other sources
        tb_write_log(main_log, traceback.format_exc())
        say(f"{stem} | decide() fehlgeschlagen: {type(exc).__name__}: {exc}")
        errors.add("fehler", stem, _facts(files), [("Fehler", f"decide(): {type(exc).__name__}: {exc}")])
        counts["fehler"] += 1
        return
    if items is None:
        say(f"{stem} | aussortiert: {chosen}")
        errors.add("aussortiert", stem, _facts(files), [("Grund", chosen)])
        counts["aussortiert"] += 1
        return
    for result in transcoder.render(source, items, on_message) if items else []:
        counts[result.state] += 1
        if result.state == "ok":
            _write_json(result)
        else:
            errors.add("error", result.file.name, [("Source", stem), ("Preset", result.preset)],
                       [*((item["step"], item["text"]) for item in result.errors), ("Ziel", result.file)])


# --------- FUNC CONFIG ---------
def _entry_problems(name: str, entry) -> list[str]:
    if not isinstance(entry, dict) or {"preset", "file"} - set(entry):
        return [f"{name}: erwartet ein Dict mit mindestens preset und file"]
    if unknown := sorted(set(entry) - set(output_keys)):
        return [f"{name}: unbekannte Schlüssel {', '.join(unknown)}; erlaubt: {', '.join(output_keys)}"]
    try:
        _output(entry, [])
    except (TypeError, ValueError) as exc:
        return [f"{name}: {exc}"]
    return []


def _config_problems() -> list[str]:
    problems = [] if mode in modes else [f"mode = {mode!r}; erlaubt: {', '.join(modes)}"]
    problems += [] if inputs else ["inputs ist leer: Dateien oder Ordner eintragen"]
    problems += [f"inputs: {item} nicht gefunden" for item in inputs if not Path(item).exists()]
    if settings is not None and not isinstance(settings, dict):
        return problems + [f"settings: erwartet ein Dict oder None, erhalten {settings!r}"]
    if mode == "render":
        problems += [] if outputs else ["outputs ist leer"]
        problems += [problem for number, entry in enumerate(outputs) for problem in _entry_problems(f"outputs[{number}]", entry)]
    for stem, values in source_settings.items():
        name = f"source_settings[{stem!r}]"
        if not isinstance(values, dict):
            problems.append(f"{name}: erwartet ein Dict mit {', '.join(source_keys)}")
            continue
        if unknown := sorted(set(values) - set(source_keys)):
            problems.append(f"{name}: unbekannte Schlüssel {', '.join(unknown)}; erlaubt: {', '.join(source_keys)}")
        if bad := [index for index in values.get("outputs", []) if not isinstance(index, int) or not 0 <= index < len(outputs)]:
            problems.append(f"{name}: outputs {bad} nicht in outputs (0 bis {len(outputs) - 1})")
        try:
            transcoder.output("check", "check", modify=values.get("modify", ()))
        except (TypeError, ValueError) as exc:
            problems.append(f"{name}: {exc}")
    return problems


# --------- MAIN ---------
def main() -> None:
    if problems := _config_problems():
        for problem in problems:
            say(f"Einstellung ungültig: {problem}")
        return
    versions = transcoder.versions()
    version_text = f"{app_name} {app_version} · transcoder {versions['transcoder']} · ffmpeg {versions['ffmpeg']}"
    say(f"{version_text} · Modus {mode}")
    files = _media_files()
    sources = _sources(files)
    say(f"{len(sources)} Sources aus {len(files)} Dateien")
    stamp, folder = f"{started:%Y-%m-%d_%H-%M-%S}", base_path / report_folder
    rows = [("Start", f"{started:%Y-%m-%d %H:%M:%S}"), ("Versionen", version_text), ("Modus", mode),
            ("Eingänge", ", ".join(map(str, inputs)))]
    report = _TextFile(folder / f"analyse_{stamp}.txt", f"{app_name} · Analyse",
                       [*rows, ("Prüfungen", ", ".join(checks)),
                        ("Einstellungen", json.dumps(settings, ensure_ascii=False, default=str) if settings else "Standard")])
    errors = _TextFile(folder / f"fehlerliste_{stamp}.txt", f"{app_name} · Fehlerliste", rows)
    counts = Counter()
    for number, (stem, group, problem) in enumerate(sources, 1):
        report.state = errors.state = f"{number} von {len(sources)} Sources"
        say(f"Source {number}/{len(sources)}: {stem} ({', '.join(path.name for path in group)})")
        if problem:
            say(f"{stem} | übersprungen: {problem}")
            errors.add("übersprungen", stem, _facts(group), [("Grund", problem)])
            counts["übersprungen"] += 1
            continue
        try:
            source = transcoder.source(group, source_settings.get(stem, {}).get("tc"))
        except (OSError, RuntimeError, ValueError) as exc:
            say(f"{stem} | Fehler: {exc}")
            errors.add("fehler", stem, _facts(group), [("Fehler", str(exc))])
            counts["fehler"] += 1
            continue
        if mode == "render":
            _render(source, stem, group, errors, counts)
            continue
        reports = transcoder.analyse(source, checks, settings, on_message)
        report.add("fehler" if any("error" in item for item in reports.values()) else "ok", stem, _facts(group),
                   _analysis_rows(reports))
        counts["analysiert"] += 1
    report.state = errors.state = f"fertig, {len(sources)} Sources"
    report.write()
    errors.write()
    say("Fertig: " + (", ".join(f"{count} {count_labels[key]}" for key, count in counts.items()) or "keine Sources"))
    for label, item in (("Analysebericht", report), ("Fehlerliste", errors)):
        if item.entries:
            say(f"{label}: {item.path}")


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
