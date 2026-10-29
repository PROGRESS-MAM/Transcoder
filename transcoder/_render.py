"""Preparation of outputs (preset, fixes, segments, timecode), ffmpeg filter graph and the render run."""
# --------- IMPORTS ---------
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
import math
import json
import os
import subprocess
import tempfile
import time
from . import _analysis, _media, _presets
from ._source import Source, open_source


# --------- CONFIG ---------
part_suffix = ".part"
script_from = 8000                  # longer filter graphs go into a script file (command line limit on Windows)
progress_interval = 0.5             # seconds between progress messages


# --------- STATIC ---------
replaced_fix = {"crop": "black_bars", "deinterlace": "interlace", "audio_map": "audio"}   # change kind: fix it replaces


# --------- TYPES ---------
class PrepareError(Exception):
    """Output cannot be rendered; errors are (step, text) pairs."""
    def __init__(self, errors: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{step}: {text}" for step, text in errors))
        self.errors = errors


@dataclass
class Plan:
    """Everything needed to render one output."""
    output: object
    preset_name: str
    preset: dict
    target: Path
    frame: tuple[int, int, Fraction]
    field: str | None                       # "tff"/"bff" for interlaced outputs
    deinterlace: bool
    crop: tuple[int, int, int, int] | None
    tracks: list[list[int]]                 # per output audio track: source channel numbers
    rate: int
    bits: int
    segments: list[tuple]                   # ("file", Source, input) | ("black", seconds) | ("source",)
    start_tc: str
    duration: float
    fixes: list[dict] = field(default_factory=list)
    modify: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)

    @property
    def part(self) -> Path:
        return self.target.with_name(self.target.name + part_suffix)


# --------- FUNC GEOMETRY ---------
def _raster(width: int, height: int, sar: Fraction, rasters: list[str]) -> tuple[int, int]:
    """Raster of a picture: its own size if it is one; else of all rasters holding the displayed picture the one
    with the closest aspect ratio (then the smaller one); the largest raster for bigger pictures."""
    sizes = [_presets.frame_size(item) for item in rasters]
    if (width, height) in sizes:
        return width, height
    shown = float(width * sar)
    holding = [size for size in sizes if size[0] >= shown - 1 and size[1] >= height]
    if not holding:
        return max(sizes, key=lambda size: size[0] * size[1])
    return min(holding, key=lambda size: (round(abs(math.log(size[0] / size[1] / (shown / height))), 3),
                                          size[0] * size[1]))


def _frame(source: Source, preset: dict, crop: tuple[int, int, int, int] | None) -> tuple[int, int, Fraction]:
    """Frame of the output; with frame = "raster" the raster of the picture after cropping."""
    frame = preset["video"]["frame"]
    if frame == "source":
        return source.width, source.height, source.sar
    if frame == "raster":
        width, height = (crop[2], crop[3]) if crop else (source.width, source.height)
        return (*_raster(width, height, source.sar, preset["video"]["rasters"]), Fraction(1))
    return (*_presets.frame_size(frame), source.sar if preset.get("preserve_metadata") else Fraction(1))


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def fit(width: int, height: int, sar: Fraction, frame: tuple[int, int, Fraction]) -> tuple[int, int]:
    """Stored size of a width×height picture with sar fitted into frame without cropping."""
    frame_width, frame_height, frame_sar = frame
    display = width * sar
    scale = min(frame_width * frame_sar / display, Fraction(frame_height, height))
    return min(frame_width, _even(display * scale / frame_sar)), min(frame_height, _even(height * scale))


# --------- FUNC PREPARE ---------
def _bits(source: Source, preset: dict) -> int:
    wanted = preset["audio"].get("bit_depth", "source")
    if wanted != "source":
        return wanted
    data = source.audio[0].data if source.audio else {}
    bits = int(_media.number(data.get("bits_per_raw_sample")) or 0) or {"s16": 16, "s16p": 16}.get(data.get("sample_fmt"), 24)
    return 16 if bits <= 16 else 24


def _segments(source: Source, merge, frame: tuple[int, int, Fraction], exact: bool,
              errors: list[tuple[str, str]], cache: dict) -> list[tuple]:
    from .transcoder import SOURCE, _Black, _Canvas
    items = list(merge) or [SOURCE]
    if items.count(SOURCE) != 1:
        errors.append(("merge", "merge braucht SOURCE genau einmal"))
        return []
    segments = []
    for item in items:
        if item is SOURCE:
            segments.append(("source",))
        elif isinstance(item, _Black):
            segments.append(("black", item.seconds))
        elif isinstance(item, (str, Path, _Canvas)):
            canvas = item if isinstance(item, _Canvas) else None
            path = canvas.file if canvas else Path(item)
            try:
                segment = cache.setdefault(path.resolve(), open_source([path], None))
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(("merge", f"{path.name}: {exc}"))
                continue
            if not segment.video:
                errors.append(("merge", f"{path.name}: kein Video"))
            elif segment.fps != source.fps:
                errors.append(("merge", f"{path.name}: {float(segment.fps or 0):g} fps, Source {float(source.fps):g} fps"))
            elif canvas and (canvas.width, canvas.height) != frame[:2]:
                errors.append(("merge", f"{path.name}: Canvas {canvas.width}×{canvas.height}, Output {frame[0]}×{frame[1]}"))
            elif canvas and (segment.width > canvas.width or segment.height > canvas.height):
                errors.append(("merge", f"{path.name}: {segment.width}×{segment.height} passt nicht ohne Beschnitt auf {canvas.width}×{canvas.height}"))
            elif not canvas and exact and (segment.width, segment.height) != frame[:2]:
                errors.append(("merge", f"{path.name}: {segment.width}×{segment.height}, Output {frame[0]}×{frame[1]}"))
            else:
                segments.append(("canvas", segment, canvas.width, canvas.height) if canvas else ("file", segment))
        else:
            errors.append(("merge", f"unbekanntes Segment {item!r}"))
    return segments


def _segment_frames(segment: tuple, fps: Fraction, source: Source) -> int:
    if segment[0] == "black":
        return _media.frame_count(segment[1], fps)
    return (segment[1] if segment[0] in ("file", "canvas") else source).video_frames


def _start_tc(source: Source, code, segments: list[tuple]) -> str:
    fps, start = source.fps, _media.tc_frames(source.start_tc, source.fps)
    if code is None:
        return source.start_tc
    if code.start is not None:
        return _media.tc_text(_media.tc_frames(code.start, fps), fps)
    if code.shift is not None:
        return _media.tc_text(start + _media.shift_frames(code.shift, fps), fps)
    before = segments[:next(index for index, segment in enumerate(segments) if segment[0] == "source")]
    return _media.tc_text(start - sum(_segment_frames(segment, fps, source) for segment in before), fps)


def prepare(source: Source, output, progress: _analysis.Progress, segment_cache: dict) -> Plan:
    """Plan of one output; PrepareError with all problems found."""
    errors: list[tuple[str, str]] = []
    try:
        name, preset = _presets.load(output.preset)
    except ValueError as exc:
        raise PrepareError([("preset", str(exc))]) from exc
    target = Path(output.file)
    if target.exists():
        errors.append(("file", f"Zieldatei vorhanden: {target}"))
    if not source.video or not source.fps:
        raise PrepareError([*errors, ("source", "Source hat kein lesbares Video")])
    if preset.get("preserve_metadata"):
        if output.merge and any(data.get("chapters") for data in source.probes):
            raise PrepareError([*errors, ("source", "Kapitel mit zusätzlichen Segmenten nicht sicher übertragbar; manuelle Prüfung nötig")])
        try:
            check_source_encoder(source, output.preset)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PrepareError([*errors, ("source", f"Master nicht kompatibel: {exc}")]) from exc
    scan = preset["video"]["scan"]
    interlaced = source.field_order in ("tff", "bff")
    field_order = (source.field_order if interlaced else "tff") if scan == "interlaced" else \
        source.field_order if scan == "source" and interlaced else None
    changes, results = {}, []
    replaced = {replaced_fix[change.kind] for change in output.modify}
    for item in (item for item in output.fixes if item.name not in replaced):
        values = _analysis.section(item.name, item.settings)
        step = f"fix:{item.name}"
        try:
            found = _analysis.report(source, item.name, values, progress)
            if item.name == "interlace":
                change, detail = _analysis.fix_interlace(found, values, scan)
            elif item.name == "black_bars":
                change, detail = _analysis.fix_black_bars(found, values, source.width, source.height)
            else:
                change, detail = _analysis.fix_audio(found, values, item.target, len(source.channels))
        except _analysis.FixError as exc:
            errors.append((step, str(exc)))
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append((step, f"Analyse nicht möglich: {exc}"))
            continue
        changes.update(change)
        results.append({"name": item.name, "changed": detail is not None, "detail": detail, "trust": found.get("trust")})
    for change in output.modify:
        changes[change.kind] = change.value
    texts = [f"{change.kind} {change.value}" for change in output.modify]
    crop = changes.get("crop")
    if crop and (crop[0] < 0 or crop[1] < 0 or crop[2] < 2 or crop[3] < 2 or crop[0] + crop[2] > source.width
                 or crop[1] + crop[3] > source.height):
        errors.append(("modify", f"Beschnitt {crop} liegt nicht im Bild {source.width}×{source.height}"))
    layout = preset["audio"]["layout"]
    channels = changes.get("audio_map") or ([1, 2] if layout == "stereo" else list(range(1, len(source.channels) + 1)))
    if not source.channels:
        errors.append(("audio", "Source hat keinen Ton"))
    elif bad := [number for number in channels if not 1 <= number <= len(source.channels)]:
        errors.append(("audio", f"Kanal {', '.join(map(str, bad))} fehlt, Source hat {len(source.channels)} Kanäle"))
    elif layout == "stereo" and len(channels) != 2:
        errors.append(("audio", f"Stereo braucht genau 2 Kanäle, erhalten {channels}"))
    frame = _frame(source, preset, crop)
    segments = _segments(source, output.merge, frame, preset["video"]["frame"] in ("raster", "source"), errors,
                         segment_cache)
    try:
        start_tc = _start_tc(source, output.timecode, segments) if segments else source.start_tc
    except ValueError as exc:
        errors.append(("timecode", str(exc)))
        start_tc = source.start_tc
    if errors:
        raise PrepareError(errors)
    tracks = [channels] if layout == "stereo" else [[number] for number in channels]
    rate = preset["audio"].get("sample_rate") or source.sample_rate
    duration = sum(_segment_frames(segment, source.fps, source) for segment in segments) / float(source.fps)
    return Plan(output, name, preset, target, frame, field_order, bool(changes.get("deinterlace")) and scan == "progressive",
                crop, tracks, rate, _bits(source, preset), segments, start_tc, duration, results, texts)


# --------- FUNC GRAPH ---------
class _Graph:
    """Filter graph text; input pads used more than once are split."""
    def __init__(self) -> None:
        self.lines, self.uses, self.count = [], {}, 0

    def label(self) -> str:
        self.count += 1
        return f"l{self.count}"

    def use(self, pad: str) -> str:
        label = self.label()
        self.uses.setdefault(pad, []).append(label)
        return f"[{label}]"

    def add(self, inputs: str, filters: str) -> str:
        label = self.label()
        self.lines.append(f"{inputs}{filters}[{label}]")
        return f"[{label}]"

    def text(self) -> str:
        splits = []
        for pad, labels in self.uses.items():
            kind = "asplit" if ":a:" in pad else "split"
            splits.append(f"[{pad}]{kind}={len(labels)}" + "".join(f"[{label}]" for label in labels))
        return ";".join(splits + self.lines)


def _picture(plan: Plan, width: int, height: int, sar: Fraction, crop, deinterlace: bool,
             source_field: str | None = None) -> list[str]:
    parity = source_field if source_field in ("tff", "bff") else "auto"
    filters = [f"bwdif=mode=send_frame:parity={parity}:deint=all"] if deinterlace else []
    if crop:
        filters.append(f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}:exact=1")
        width, height = crop[2], crop[3]
    frame_width, frame_height, frame_sar = plan.frame
    cover = plan.preset["video"].get("fit", "contain") == "cover"
    if cover:
        scale = max(Fraction(frame_width) * frame_sar / (width * sar), Fraction(frame_height, height))
        # Round outwards, so rounding never creates a new border.
        size = (math.ceil(width * sar * scale / frame_sar / 2) * 2, math.ceil(height * scale / 2) * 2)
    else:
        size = fit(width, height, sar, plan.frame)
    if size != (width, height) or sar != frame_sar:
        color = ":in_color_matrix=auto:out_color_matrix=bt709" if plan.preset["video"].get("color") == "bt709" else ""
        filters.append(f"scale={size[0]}:{size[1]}:flags=lanczos{':interl=1' if plan.field else ''}{color}")
    if size != (frame_width, frame_height):
        x, y = (size[0] - frame_width) // 4 * 2, (size[1] - frame_height) // 4 * 2
        if cover:
            filters.append(f"crop={frame_width}:{frame_height}:{x}:{y}:exact=1")
        else:
            filters.append(f"pad={frame_width}:{frame_height}:{(frame_width - size[0]) // 4 * 2}:{(frame_height - size[1]) // 4 * 2}:color=black")
    filters.append(f"setsar={frame_sar.numerator}/{frame_sar.denominator}")
    if pix_fmt := plan.preset["video"].get("pix_fmt"):
        filters.append(f"format={pix_fmt}")
    filters.append(f"setfield={plan.field or 'prog'}")
    return filters


def _video(graph: _Graph, plan: Plan, segment: tuple, inputs: dict, source: Source, merged: bool) -> str:
    fps = source.fps
    if segment[0] == "black":
        frame_width, frame_height, frame_sar = plan.frame
        filters = [f"color=c=black:s={frame_width}x{frame_height}:r={fps.numerator}/{fps.denominator}",
                   f"trim=end_frame={_media.frame_count(segment[1], fps)}",
                   *_picture(plan, frame_width, frame_height, frame_sar, None, False)]
        return graph.add("", ",".join(filters + ["setpts=PTS-STARTPTS"]))
    owner = segment[1] if segment[0] in ("file", "canvas") else source
    pad = f"{inputs[(id(owner), owner.video.file)]}:v:{owner.video.ordinal}"
    if segment[0] == "canvas":
        width, height = segment[2:4]
        filters = [f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
                   *_picture(plan, width, height, plan.frame[2], None, False, owner.field_order)]
    else:
        crop, deinterlace = (plan.crop, plan.deinterlace) if segment[0] == "source" else (None, False)
        filters = _picture(plan, owner.width, owner.height, owner.sar, crop, deinterlace, owner.field_order)
    return graph.add(graph.use(pad), ",".join(filters + (["setpts=PTS-STARTPTS"] if merged else [])))


def _audio(graph: _Graph, plan: Plan, segment: tuple, inputs: dict, source: Source, merged: bool) -> list[str]:
    duration = _segment_frames(segment, source.fps, source) / float(source.fps)
    tail = f",apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS" if merged else ""
    owner = segment[1] if segment[0] in ("file", "canvas") else source if segment[0] == "source" else None
    labels = []
    for track_number, track in enumerate(plan.tracks, 1):
        if owner is source:
            numbers = track
        elif owner and owner.channels:
            count = len(owner.channels)
            numbers = ([1, 2 if count > 1 else 1] if len(track) == 2 else
                       [track_number] if track_number <= count else [])
        else:
            numbers = []
        if not numbers:
            layout = "stereo" if len(track) == 2 else "mono"
            labels.append(graph.add("", f"anullsrc=r={plan.rate}:cl={layout},atrim=duration={duration:.6f},"
                                        f"asetpts=PTS-STARTPTS"))
            continue
        parts = []
        for number in numbers:
            ref = owner.channels[number - 1]
            pad = f"{inputs[(id(owner), ref.stream.file)]}:a:{ref.stream.ordinal}"
            parts.append(graph.add(graph.use(pad), f"pan=mono|c0=c{ref.index},aresample={plan.rate}"))
        joined = parts[0] if len(parts) == 1 else graph.add("".join(parts), "join=inputs=2:channel_layout=stereo")
        labels.append(graph.add(joined, tail.lstrip(",")) if tail else joined)
    return labels


def _video_codec_args(video: dict, field_order: str | None = None) -> list[str]:
    args = ["-c:v", video["codec"]]
    if video.get("profile"):
        args += ["-profile:v", video["profile"]]
    if video["codec"] == "libx264":
        args += ["-preset", video.get("x264_preset", "medium")]
    if video["codec"] == "h264_nvenc":
        args += ["-preset", video.get("nvenc_preset", "p5"), "-tune", video.get("nvenc_tune", "hq"),
                 "-rc", video.get("nvenc_rc", "vbr")]
    args += [option for key, name in (("bitrate", "-b:v"), ("maxrate", "-maxrate"), ("bufsize", "-bufsize"))
             if video.get(key) for option in (name, video[key])]
    if video.get("color") == "bt709":
        args += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    args += ["-field_order", {"tff": "tt", "bff": "bb"}.get(field_order, "progressive")]
    if field_order and video["codec"] == "dnxhd":
        args += ["-flags", "+ildct+ilme"]
    return args


def _codec_args(plan: Plan) -> list[str]:
    audio = plan.preset["audio"]
    args = _video_codec_args(plan.preset["video"], plan.field)
    if audio["codec"] == "aac":
        args += ["-c:a", "aac", "-b:a", audio.get("bitrate", "320k"), "-ar", str(plan.rate)]
    else:
        args += ["-c:a", f"pcm_s{plan.bits}le", "-ar", str(plan.rate)]
    container = plan.preset["container"]
    args += ["-timecode", plan.start_tc, "-f", container]
    if not plan.preset.get("preserve_metadata"):
        args += ["-map_metadata", "-1", "-map_chapters", "-1"]
    elif container == "mov":
        args += ["-movflags", "+use_metadata_tags+write_colr"]
    if container == "mp4":
        args += ["-write_tmcd", "1", *(["-movflags", "+faststart"] if plan.preset.get("faststart") else [])]
    return args


def check_encoder(preset: str | Path) -> None:
    """Validate a preset; for NVENC encode one generated frame without writing media files.

    Uses the same ffmpeg and video options as rendering. Raises RuntimeError or OSError if NVENC cannot start.
    CPU presets only undergo preset validation. The NVENC probe has a 30-second time limit.
    """
    _, values = _presets.load(preset)
    video = values["video"]
    if video["codec"] != "h264_nvenc":
        return
    args = ["-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25",
            "-frames:v", "1", "-an", "-pix_fmt", video.get("pix_fmt", "yuv420p"),
            *_video_codec_args(video), "-f", "null", "-"]
    try:
        _media.run_text("ffmpeg", args, timeout=30)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"NVENC nicht nutzbar. Prüfe NVIDIA-Treiber und den verwendeten ffmpeg-Build; "
                           f"für CPU-Betrieb das libx264-Preset wählen. {exc}") from exc



color_options = {"color_space": "-colorspace", "color_primaries": "-color_primaries",
                 "color_transfer": "-color_trc", "color_range": "-color_range",
                 "chroma_location": "-chroma_sample_location"}


def _color_args(data: dict) -> list[str]:
    args = []
    for key, option in color_options.items():
        value = data.get(key)
        if value and value not in ("unknown", "unspecified", "reserved"):
            if key == "color_transfer":
                value = {"bt470m": "gamma22", "bt470bg": "gamma28"}.get(value, value)
            args += [option, value]
    return args


def _decoded_frame(path: Path) -> dict:
    text, _ = _media.run_text("ffprobe", ["-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                                       "-show_entries", "frame=interlaced_frame,top_field_first,color_space,color_range,color_transfer,color_primaries",
                                       "-of", "json", str(path)])
    frames = json.loads(text).get("frames", [])
    if not frames:
        raise ValueError("Erste Bildprüfung: kein decodierbares Bild")
    return frames[0]


def _source_args(source: Source, plan: Plan, inputs: dict) -> list[str]:
    if not plan.preset.get("preserve_metadata"):
        return []
    ref = source.video
    number = inputs[(id(source), ref.file)]
    args = ["-r:v", str(source.fps), "-fps_mode:v", "cfr", "-map_metadata", str(number),
            "-map_metadata:s:v:0", f"{number}:s:v:{ref.ordinal}", "-map_chapters", str(number)]
    args += _color_args(source.video.data)
    for index, track in enumerate(plan.tracks):
        channel = source.channels[track[0] - 1]
        stream = channel.stream
        args += [f"-map_metadata:s:a:{index}", f"{inputs[(id(source), stream.file)]}:s:a:{stream.ordinal}"]
    args += ["-metadata", f"timecode={plan.start_tc}", "-metadata:s:v:0", f"timecode={plan.start_tc}"]
    return args


def check_source_encoder(source: Source, preset: str | Path) -> None:
    """Probe one generated frame before a Marathon job; unsupported inputs must not be transcoded."""
    _, values = _presets.load(preset)
    frame = _frame(source, values, None)
    if frame[0] % 2 or frame[1] % 2 or min(frame[:2]) < 2:
        raise ValueError(f"Quellbildgröße {frame[0]}×{frame[1]} für den Master nicht geeignet")
    if values.get("preserve_metadata") and source.video.data.get("side_data_list"):
        unsupported = [item.get("side_data_type", "unbekannt") for item in source.video.data["side_data_list"]
                       if item.get("side_data_type") not in ("SMPTE 12-1 timecode", "S12M Timecode")]
        if unsupported:
            raise ValueError(f"Zusatzmetadaten nicht sicher übertragbar: {', '.join(unsupported)}; manuelle Prüfung nötig")
    if values.get("preserve_metadata"):
        if source.video.data.get("closed_captions") or source.video.data.get("film_grain"):
            raise ValueError("Untertitel-/Filmgrain-Zusatzdaten nicht sicher übertragbar; manuelle Prüfung nötig")
        extra = [stream.get("codec_type") for data in source.probes for stream in data.get("streams", [])
                 if stream.get("codec_type") not in ("video", "audio")
                 and stream.get("codec_tag_string") != "tmcd"]
        if extra:
            raise ValueError(f"Zusätzliche Quellstreams nicht sicher übertragbar: {', '.join(str(item) for item in extra)}")
    scan = values["video"]["scan"]
    field = source.field_order if source.field_order in ("tff", "bff") else None
    if scan == "interlaced" and not field:
        raise ValueError("DNxHD-Master benötigt eine eindeutige Feldreihenfolge")
    if values["video"].get("profile") == "dnxhd" and values["video"].get("bitrate") == "185M" and source.fps != Fraction(25):
        raise ValueError(f"DNxHD 185x interlaced benötigt 25 Frames/s; Quelle hat {source.fps}. Clip nicht transcodiert")
    with tempfile.TemporaryDirectory(prefix="transcoder_probe_") as folder:
        target = Path(folder) / "probe.mov"
        _media.run_text("ffmpeg", ["-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                  f"color=c=black:s={frame[0]}x{frame[1]}:r={source.fps}", "-frames:v", "1", "-an",
                                  "-vf", f"format={values['video']['pix_fmt']},setsar={frame[2]},setfield={field or 'prog'}",
                                  *_video_codec_args(values["video"], field), *_color_args(source.video.data),
                                  "-threads:v", "1", "-movflags", "+write_colr", "-f", "mov", str(target)], timeout=30)
        video = _media.probe(target)["streams"][0]
        decoded = _decoded_frame(target)
        changed = [key for key in color_options if source.video.data.get(key) not in (None, "unknown", "unspecified", "reserved")
                   and (video.get(key) != source.video.data[key]
                        or key in decoded and decoded[key] != source.video.data[key])]
        if changed:
            raise ValueError(f"Quell-Farbangaben mit dem Master-Codec nicht sicher übertragbar: {', '.join(changed)}; Clip nicht transcodiert")


def _verify_master(source: Source, plan: Plan) -> None:
    data = _media.probe(plan.part)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    errors = []
    expected_profile = {"dnxhr_hqx": "DNXHR HQX", "dnxhd": "DNXHD"}.get(plan.preset["video"]["profile"])
    if video.get("codec_name") != "dnxhd" or video.get("profile") != expected_profile:
        errors.append("Video-Codec/Profil")
    if (video.get("width"), video.get("height")) != plan.frame[:2]:
        errors.append("Bildgröße")
    if _media.rate(video.get("avg_frame_rate")) != source.fps:
        errors.append("Framerate")
    expected_frames = sum(_segment_frames(segment, source.fps, source) for segment in plan.segments)
    if int(video.get("nb_frames") or 0) != expected_frames:
        errors.append("Videolänge/Framezahl")
    if _media.rate(str(video.get("sample_aspect_ratio", "1:1")).replace(":", "/")) != plan.frame[2]:
        errors.append("Pixel-Seitenverhältnis")
    decoded = _decoded_frame(plan.part)
    actual_order = ("tff" if decoded.get("top_field_first") else "bff") if decoded.get("interlaced_frame") else None
    if actual_order != plan.field:
        errors.append("Scan/Feldreihenfolge")
    if video.get("pix_fmt") != plan.preset["video"].get("pix_fmt"):
        errors.append("Video-Pixelformat")
    if len(audio) != len(plan.tracks) or any(item.get("codec_name") != f"pcm_s{plan.bits}le"
                                           or item.get("channels") != 1
                                           or int(item.get("sample_rate", 0)) != plan.rate for item in audio):
        errors.append("Audioformat/Kanäle")
    tags = [data.get("format", {}).get("tags", {})] + [item.get("tags", {}) for item in streams]
    codes = [value for tag in tags for key, value in tag.items() if key.casefold() == "timecode"]
    if not codes or not any(value.replace(";", ":") == plan.start_tc.replace(";", ":") for value in codes):
        errors.append("Timecode")
    for key in color_options:
        expected = source.video.data.get(key)
        if expected and expected not in ("unknown", "unspecified", "reserved") and (video.get(key) != expected or key in decoded and decoded[key] != expected):
            errors.append(key)
    expected_chapters = source.probes[source.video.file].get("chapters", [])
    actual_chapters = data.get("chapters", [])
    if len(expected_chapters) != len(actual_chapters) or any(
            abs(float(old.get(key, 0)) - float(new.get(key, 0))) > 0.001
            for old, new in zip(expected_chapters, actual_chapters) for key in ("start_time", "end_time")):
        errors.append("Kapitel")
    if errors:
        raise ValueError(f"Master-Nachprüfung fehlgeschlagen: {', '.join(errors)}; Datei nicht freigegeben")


def _permanent_error(message: str) -> bool:
    text = message.casefold()
    return ("assertion" in text and "failed" in text) or any(marker in text for marker in (
        "not a encoding option", "not an encoding option", "unrecognized option", "unknown encoder",
        "error setting option", "error parsing options", "unable to parse option value",
        "no nvenc capable devices found", "cannot load nvcuda", "cannot load libcuda", "cannot load nvencodeapi",
        "driver does not support the required nvenc", "minimum required nvidia driver"))

def build(source: Source, plans: list[Plan]) -> tuple[list[str], str]:
    """(input and output arguments, filter graph) for all plans in one ffmpeg run."""
    inputs, args = {}, []
    owners = [source] + list({id(segment[1]): segment[1] for plan in plans for segment in plan.segments
                              if segment[0] in ("file", "canvas")}.values())
    for owner in owners:
        for number, path in enumerate(owner.files):
            inputs[(id(owner), number)] = len(inputs)
            args += ["-i", str(path)]
    graph, outputs = _Graph(), []
    for plan in plans:
        merged = plan.segments != [("source",)]
        parts = [(_video(graph, plan, segment, inputs, source, merged), _audio(graph, plan, segment, inputs, source, merged))
                 for segment in plan.segments]
        if merged:
            labels = "".join(video + "".join(audio) for video, audio in parts)
            video_label = graph.label()
            audio_labels = [graph.label() for _ in plan.tracks]
            graph.lines.append(f"{labels}concat=n={len(parts)}:v=1:a={len(plan.tracks)}[{video_label}]"
                               + "".join(f"[{label}]" for label in audio_labels))
            video_label, audio_labels = f"[{video_label}]", [f"[{label}]" for label in audio_labels]
        else:
            video_label, audio_labels = parts[0]
        outputs += ["-map", video_label, *(option for label in audio_labels for option in ("-map", label)),
                    *_codec_args(plan), *_source_args(source, plan, inputs), str(plan.part)]
    return args + ["@graph"] + outputs, graph.text()


# --------- FUNC RUN ---------
def _remove(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def run(source: Source, plans: list[Plan], on_progress: Callable[[dict], None]) -> dict[int, str]:
    """Render all plans in one ffmpeg run; RuntimeError on failure (part files removed).

    Returns {index of plan: error} for finished files that could not be moved to their target.
    """
    for plan in plans:
        plan.target.parent.mkdir(parents=True, exist_ok=True)
    arguments, graph_text = build(source, plans)
    total = max(plan.duration for plan in plans) or 1.0
    with tempfile.TemporaryDirectory(prefix="transcoder_") as folder:
        if len(graph_text) > script_from:
            script = Path(folder) / "graph.txt"
            script.write_text(graph_text, encoding="utf-8")
            graph_args = ["-filter_complex_script", str(script)]
        else:
            graph_args = ["-filter_complex", graph_text]
        index = arguments.index("@graph")
        command = [_media.tool_path("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats",
                   "-progress", "pipe:1", *arguments[:index], *graph_args, *arguments[index + 1:]]
        for plan in plans:
            plan.command = command
        errors_path = Path(folder) / "stderr.txt"
        with errors_path.open("wb") as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                       text=True, encoding="utf-8", errors="replace")
            try:
                values, last, started = {}, 0.0, time.monotonic()
                for line in process.stdout:
                    name, _, value = line.strip().partition("=")
                    values[name] = value
                    if name == "progress" and time.monotonic() - last >= progress_interval:
                        last = time.monotonic()
                        position = (_media.number(values.get("out_time_us")) or 0) / 1e6
                        speed = _media.number(values.get("speed", "").rstrip("x")) or 0
                        on_progress({"percent": min(100.0, position / total * 100), "media_time": position,
                                     "fps": _media.number(values.get("fps")) or 0, "speed": speed,
                                     "eta": (total - position) / speed if speed else None,
                                     "elapsed": time.monotonic() - started})
                code = process.wait()
            except BaseException:
                process.kill()
                process.wait()
                _remove([plan.part for plan in plans])
                raise
            finally:
                process.stdout.close()
        if code:
            _remove([plan.part for plan in plans])
            raise RuntimeError(_media.tail(errors_path.read_text(encoding="utf-8", errors="replace")))
    failed = {}
    for index, plan in enumerate(plans):
        try:
            if plan.preset.get("preserve_metadata"):
                _verify_master(source, plan)
            if plan.target.exists():
                raise FileExistsError(f"Zieldatei inzwischen vorhanden: {plan.target}")
            os.replace(plan.part, plan.target)
        except (OSError, RuntimeError, ValueError) as exc:
            _remove([plan.part])
            failed[index] = str(exc)
    return failed
