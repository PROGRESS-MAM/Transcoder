"""Transcoder core: source → render → outputs, source → analyse → reports."""
# --------- IMPORTS ---------
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
import time
from . import _analysis, _media, _render
from ._source import Source, open_source


# --------- CONFIG ---------
app_name = "Transcoder"
app_version = "0.3.0"


# --------- TYPES ---------
@dataclass(frozen=True)
class _Message:
    """Message of the core: kind 'info', 'warning' or 'progress', ready text and data."""
    kind: str
    text: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Fix:
    name: str
    target: dict
    settings: dict | None


@dataclass(frozen=True)
class _Modify:
    kind: str                               # "crop", "deinterlace" or "audio_map"
    value: object


@dataclass(frozen=True)
class _Black:
    seconds: float


@dataclass(frozen=True)
class _Canvas:
    file: Path
    width: int
    height: int


@dataclass(frozen=True)
class _Segment:
    name: str

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True)
class _Timecode:
    start: str | None
    shift: str | None
    anchor: _Segment | None


@dataclass(frozen=True)
class _Output:
    preset: str | Path
    file: Path
    fixes: tuple[_Fix, ...]
    modify: tuple[_Modify, ...]
    merge: tuple
    timecode: _Timecode | None


@dataclass
class _Result:
    """Result of one output."""
    state: str                              # "ok" or "error"
    file: Path
    summary: str
    errors: list[dict] = field(default_factory=list)
    preset: str | None = None
    fixes: list[dict] = field(default_factory=list)
    modify: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    timecode: str | None = None
    command: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        """JSON-ready content."""
        return {**self.__dict__, "file": str(self.file)}


# --------- STATIC ---------
SOURCE = _Segment("SOURCE")
fix_targets = {"audio": ({"channels": 2}, {"min_audible": 2}), "black_bars": ({},), "interlace": ({},)}
OnMessage = Callable[[_Message], None] | None


# --------- FUNC MESSAGES ---------
def _sender(on_message: OnMessage) -> Callable[[str, str, dict | None], None]:
    def send(kind: str, text: str, data: dict | None = None) -> None:
        if on_message:
            on_message(_Message(kind, text, data or {}))
    return send


# --------- FUNC SOURCE ---------
def source(files: str | Path | Iterable[str | Path], tc: str | None = None) -> Source:
    """Source of one or more files (e.g. OP-Atom: one file per stream); their streams form one source.

    tc sets the start timecode HH:MM:SS:FF; otherwise it comes from the files, without any 01:00:00:00.
    Analyses are stored at the source: each check runs once per source and settings, also across analyse and fix.
    Raises FileNotFoundError for missing files, RuntimeError if ffprobe fails, ValueError for an invalid tc.
    """
    files = [files] if isinstance(files, (str, Path)) else list(files)
    return open_source(files, tc)


def analyse(source: Source, checks: str | Iterable[str], settings: dict | None = None,
            on_message: OnMessage = None) -> dict[str, dict]:
    """Reports {check: report} of the source without changes.

    checks: "streams", "interlace", "black_bars", "audio" as name or list.
    settings: {"samples": {…}, "<check>": {…}}; missing entries use the defaults.
    A check that cannot run gets {"error": text}. Raises ValueError for unknown checks or settings.
    """
    names = [checks] if isinstance(checks, str) else list(checks)
    sections = _analysis.split_settings(settings)
    if unknown := [name for name in names if name not in _analysis.checks]:
        raise ValueError(f"Unbekannte Prüfung {', '.join(unknown)}; erlaubt: {', '.join(_analysis.checks)}.")
    send, reports = _sender(on_message), {}
    combine = _analysis._can_combine(source, names, sections)
    done = set()
    for name in names:
        if name in done:
            continue
        group = [item for item in dict.fromkeys(names) if item in ("black_bars", "interlace")] \
            if combine and name in ("black_bars", "interlace") else [name]
        label, started = "+".join(group), time.monotonic()
        send("info", f"{source.name()} | Analyse {label} …")
        progress = lambda text, share: send("progress", text, {"percent": share * 100})
        try:
            if len(group) == 2:
                reports.update(_analysis._video_reports(source, sections, progress))
            else:
                reports[name] = _analysis.report(source, name, sections[name], progress)
        except (OSError, RuntimeError, ValueError) as exc:
            for item in group:
                reports[item] = {"error": str(exc)}
            send("warning", f"{source.name()} | Analyse {label} nicht möglich: {exc}")
        elapsed = time.monotonic() - started
        send("info", f"{source.name()} | Analyse {label} fertig in {_media.clock(elapsed)}",
             {"checks": group, "elapsed": elapsed})
        done.update(group)
    return {name: reports[name] for name in names}


# --------- FUNC OUTPUT ---------
def fix(name: str, settings: dict | None = None, **target) -> _Fix:
    """Fix of an output: analysis with repair advice plus the change that applies it.

    name: "audio", "black_bars" or "interlace". target: what the fix shall reach, for audio channels=2 (exactly
    2 audible channels) or min_audible=2 (at least 2 audible channels as tracks). settings: section of this check,
    e.g. {"trust": 0.99}, optionally with own "samples". Raises ValueError for unknown names, targets or settings.
    """
    if name not in fix_targets:
        raise ValueError(f"Unbekannter Fix {name!r}; erlaubt: {', '.join(fix_targets)}.")
    if target not in fix_targets[name]:
        raise ValueError(f"fix({name!r}): Ziel {target or 'ohne Angabe'} nicht unterstützt; erlaubt: "
                         f"{' oder '.join(str(item) or 'ohne Angabe' for item in fix_targets[name])}.")
    _analysis.section(name, settings)
    return _Fix(name, dict(target), settings)


def crop(x: int, y: int, w: int, h: int) -> _Modify:
    """Change: crop to this area of the source, then fit into the frame of the preset."""
    if not all(isinstance(value, int) and value >= 0 for value in (x, y, w, h)) or w < 2 or h < 2:
        raise ValueError(f"crop braucht ganze Zahlen ab 0 und eine Fläche ab 2×2, erhalten {(x, y, w, h)}.")
    return _Modify("crop", (x, y, w, h))


def deinterlace() -> _Modify:
    """Change: bwdif, one picture per frame (25i → 25p); only for progressive presets."""
    return _Modify("deinterlace", True)


def audio_map(channels: Iterable[int]) -> _Modify:
    """Change: source channels of the output in order, e.g. [1, 1]; 1-based over all audio streams of the source."""
    values = list(channels)
    if not values or not all(isinstance(value, int) and value >= 1 for value in values):
        raise ValueError(f"audio_map braucht Kanalnummern ab 1, erhalten {values!r}.")
    return _Modify("audio_map", values)


def black(seconds: float) -> _Black:
    """Segment: black picture with silence in the format of the output."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError(f"black braucht eine Dauer größer 0, erhalten {seconds!r}.")
    return _Black(float(seconds))


def centered(file: str | Path, width: int, height: int) -> _Canvas:
    """Segment: center a picture at its original size on a black frame."""
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 2 for value in (width, height)):
        raise ValueError(f"centered braucht Bildgrößen ab 2×2, erhalten {(width, height)}.")
    return _Canvas(Path(file), width, height)


def timecode(start: str | None = None, shift: str | None = None, anchor: _Segment | None = None) -> _Timecode:
    """Start timecode of an output: fixed start, start of the source plus shift, or anchor=SOURCE.

    anchor=SOURCE keeps the source at its own start timecode; segments before it move the start forward.
    Exactly one argument; without timecode() an output starts with the timecode of the source.
    """
    given = [value for value in (start, shift, anchor) if value is not None]
    if len(given) != 1 or anchor not in (None, SOURCE):
        raise ValueError("timecode braucht genau eins von start, shift oder anchor=SOURCE.")
    if start is not None and not _media.tc_pattern.fullmatch(start.strip()):
        raise ValueError(f"timecode(start={start!r}): erwartet HH:MM:SS:FF.")
    if shift is not None and not _media.shift_pattern.fullmatch(shift.strip()):
        raise ValueError(f"timecode(shift={shift!r}): erwartet z. B. '-00:00:10:00'.")
    return _Timecode(start, shift, anchor)


def output(preset: str | Path, file: str | Path, fixes: Iterable[_Fix] = (), modify: Iterable[_Modify] = (),
           merge: Iterable = (), timecode: _Timecode | None = None) -> _Output:
    """Description of one output file; render() creates it.

    preset: name in transcoder/presets or path to a TOML file. merge: segments in order (file paths, centered(),
    black(), SOURCE); without merge only the source. A change in modify replaces the fix of the same kind.
    """
    fixes, modify, merge = tuple(fixes), tuple(modify), tuple(merge)
    if bad := [item for item in fixes if not isinstance(item, _Fix)]:
        raise ValueError(f"fixes: nur fix(…), erhalten {bad!r}.")
    if bad := [item for item in modify if not isinstance(item, _Modify)]:
        raise ValueError(f"modify: nur crop(), deinterlace(), audio_map(), erhalten {bad!r}.")
    if timecode is not None and not isinstance(timecode, _Timecode):
        raise ValueError("timecode: nur timecode(…).")
    return _Output(preset, Path(file), fixes, modify, merge, timecode)


# --------- FUNC RENDER ---------
def _segment_texts(plan: _render.Plan) -> list[str]:
    names = {"source": lambda segment: "SOURCE", "black": lambda segment: f"black({segment[1]:g})",
             "file": lambda segment: str(segment[1].files[0]),
             "canvas": lambda segment: str(segment[1].files[0])}
    return [names[segment[0]](segment) for segment in plan.segments]


def _ok(plan: _render.Plan, seconds: float) -> _Result:
    changed = [item["detail"] for item in plan.fixes if item["changed"]] + plan.modify
    return _Result("ok", plan.target, f"{plan.preset_name}: " + ("; ".join(changed) or "ohne Änderung"), [],
                   plan.preset_name, plan.fixes, plan.modify, _segment_texts(plan), plan.start_tc, plan.command,
                   round(seconds, 1))


def _error(item: _Output, errors: list[tuple[str, str]], plan: _render.Plan | None = None,
           seconds: float = 0.0) -> _Result:
    texts = [{"step": step, "text": text} for step, text in errors]
    return _Result("error", item.file, "; ".join(f"{step}: {text}" for step, text in errors), texts,
                   plan.preset_name if plan else Path(str(item.preset)).stem, plan.fixes if plan else [],
                   plan.modify if plan else [],
                   _segment_texts(plan) if plan else [], plan.start_tc if plan else None,
                   plan.command if plan else [], round(seconds, 1))


def _render_plans(source: Source, plans: list[_render.Plan], send) -> dict[int, str]:
    def progress(data: dict) -> None:
        eta = f"{_media.clock(data['eta'])}" if data["eta"] is not None else "–"
        send("progress", f"{source.name()} | {data['percent']:5.1f}% {_media.clock(data['media_time'])} | "
                         f"{data['fps']:.0f} fps {data['speed']:.1f}x | ETA {eta}", data)
    return _render.run(source, plans, progress)


def render(source: Source, outputs, on_message: OnMessage = None) -> list[_Result]:
    """Render the source into one output or a list of outputs in one run; the source is read once.

    Returns one result per output in the same order. Each output stands alone: an output with an error is not
    rendered, the others go on; joint failures retry individually unless recognized as permanent.
    Ctrl+C stops ffmpeg, removes part files and raises KeyboardInterrupt.
    """
    items = [outputs] if isinstance(outputs, _Output) else list(outputs)
    if not items or not all(isinstance(item, _Output) for item in items):
        raise ValueError("render braucht output(…) oder eine Liste davon.")
    send, started = _sender(on_message), time.monotonic()
    results: list[_Result | None] = [None] * len(items)
    plans, cache = {}, {}
    for index, item in enumerate(items):
        try:
            plans[index] = _render.prepare(source, item, lambda text, share: send("progress", text,
                                                                                   {"percent": share * 100}), cache)
        except _render.PrepareError as exc:
            results[index] = _error(item, exc.errors)
            send("warning", f"{item.file.name} | Fehler: {exc}")
    groups = [list(plans)] if plans else []
    while groups:
        group = groups.pop(0)
        names = ", ".join(plans[index].target.name for index in group)
        encoders = ", ".join(plans[index].preset["video"]["codec"] for index in group)
        send("info", f"{source.name()} | Rendern: {names} | Encoder: {encoders}")
        group_start = time.monotonic()
        try:
            failed = _render_plans(source, [plans[index] for index in group], send)
        except RuntimeError as exc:
            elapsed = time.monotonic() - group_start
            send("warning", f"{source.name()} | Durchlauf gescheitert in {_media.clock(elapsed)} | "
                            f"Encoder: {encoders}", {"elapsed": elapsed, "encoders": encoders})
            if len(group) > 1 and not _render._permanent_error(str(exc)):
                send("warning", f"{source.name()} | gemeinsamer Durchlauf gescheitert, einzeln weiter: {exc}")
                groups += [[index] for index in group]
                continue
            for index in group:
                results[index] = _error(items[index], [("ffmpeg", str(exc))], plans[index], elapsed)
                send("warning", f"{plans[index].target.name} | Fehler: ffmpeg: {exc}")
            continue
        seconds = time.monotonic() - group_start
        send("info", f"{source.name()} | Durchlauf fertig in {_media.clock(seconds)} | Encoder: {encoders}",
             {"elapsed": seconds, "encoders": encoders})
        for position, index in enumerate(group):
            if position in failed:
                results[index] = _error(items[index], [("file", failed[position])], plans[index], seconds)
            else:
                results[index] = _ok(plans[index], seconds)
            send("info" if results[index].state == "ok" else "warning",
                 f"{plans[index].target.name} | {results[index].state}: {results[index].summary}")
    send("info", f"{source.name()} | fertig in {_media.clock(time.monotonic() - started)}")
    return results


def versions() -> dict[str, str]:
    """Versions of the core and of ffmpeg."""
    try:
        stdout, _ = _media.run_text("ffmpeg", ["-version"])
        ffmpeg = stdout.split("\n", 1)[0].split(" ")[2]
    except (RuntimeError, IndexError, OSError):
        ffmpeg = "nicht gefunden"
    return {"transcoder": app_version, "ffmpeg": ffmpeg}
