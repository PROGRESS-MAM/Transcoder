"""Source of the transcoder: one or more files whose streams form one source; holds the analysis reports."""
# --------- IMPORTS ---------
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from . import _media


# --------- CONFIG ---------
default_tc = "01:00:00:00"
interlaced_orders = {"tt": "tff", "tb": "tff", "bb": "bff", "bt": "bff"}


# --------- TYPES ---------
@dataclass(frozen=True)
class StreamRef:
    """One stream of the source: file index, ordinal within its type in that file (for 0:v:n / 0:a:n), data."""
    file: int
    ordinal: int
    data: dict


@dataclass(frozen=True)
class ChannelRef:
    """One audio channel of the source; number is 1-based over all audio streams of all files."""
    number: int
    stream: StreamRef
    index: int                              # channel within its stream, 0-based


@dataclass(eq=False)
class Source:
    """Probed source; created by transcoder.source()."""
    files: tuple[Path, ...]
    probes: tuple[dict, ...]
    video: StreamRef | None
    audio: tuple[StreamRef, ...]
    channels: tuple[ChannelRef, ...]
    fps: Fraction | None
    duration: float
    start_tc: str
    tc_origin: str                          # "tc", "file" or "default"
    reports: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        return int(self.video.data.get("width") or 0) if self.video else 0

    @property
    def height(self) -> int:
        return int(self.video.data.get("height") or 0) if self.video else 0

    @property
    def sar(self) -> Fraction:
        value = _media.rate(str(self.video.data.get("sample_aspect_ratio", "1:1")).replace(":", "/")) if self.video else None
        return value or Fraction(1)

    @property
    def field_order(self) -> str | None:
        """'tff', 'bff', 'progressive' or None from the metadata."""
        order = self.video.data.get("field_order") if self.video else None
        return "progressive" if order == "progressive" else interlaced_orders.get(order)

    @property
    def sample_rate(self) -> int:
        return int(_media.number(self.audio[0].data.get("sample_rate")) or 48000) if self.audio else 48000

    @property
    def video_frames(self) -> int:
        return _media.frame_count(self.duration, self.fps) if self.fps else 0

    def name(self) -> str:
        return self.files[0].name if len(self.files) == 1 else f"{self.files[0].name} (+{len(self.files) - 1})"


# --------- FUNC ---------
def _streams(probes: tuple[dict, ...], kind: str) -> list[StreamRef]:
    refs = []
    for number, data in enumerate(probes):
        typed = [stream for stream in data.get("streams", []) if stream.get("codec_type") == kind]
        refs += [StreamRef(number, ordinal, stream) for ordinal, stream in enumerate(typed)]
    return refs


def _file_tc(probes: tuple[dict, ...], fps: Fraction | None) -> str | None:
    tags = [data.get("format", {}).get("tags", {}) for data in probes]
    tags += [stream.get("tags", {}) for data in probes for stream in data.get("streams", [])]
    for value in (value for tag in tags for key, value in tag.items() if key.casefold() == "timecode"):
        try:
            return _media.tc_text(_media.tc_frames(value, fps or Fraction(25)), fps or Fraction(25))
        except ValueError:
            continue
    return None


def open_source(files: list[str | Path], tc: str | None) -> Source:
    paths = tuple(Path(file) for file in files)
    if not paths:
        raise ValueError("source braucht mindestens eine Datei.")
    if missing := [str(path) for path in paths if not path.is_file()]:
        raise FileNotFoundError(f"Datei nicht gefunden: {', '.join(missing)}")
    probes = tuple(_media.probe(path) for path in paths)
    # Cover images are reported as video streams by ffprobe; they keep their ordinal for 0:v:n.
    video = next((ref for ref in _streams(probes, "video") if not ref.data.get("disposition", {}).get("attached_pic")),
                 None)
    audio = tuple(_streams(probes, "audio"))
    channels, number = [], 0
    for ref in audio:
        for index in range(int(ref.data.get("channels") or 0)):
            number += 1
            channels.append(ChannelRef(number, ref, index))
    fps = _media.rate(video.data.get("avg_frame_rate")) or _media.rate(video.data.get("r_frame_rate")) if video else None
    durations = [_media.number((video or (audio[0] if audio else None) or StreamRef(0, 0, {})).data.get("duration"))]
    durations += [_media.number(data.get("format", {}).get("duration")) for data in probes]
    duration = next((value for value in durations if value), 0.0)
    if tc is not None:
        start_tc, origin = _media.tc_text(_media.tc_frames(tc, fps or Fraction(25)), fps or Fraction(25)), "tc"
    elif found := _file_tc(probes, fps):
        start_tc, origin = found, "file"
    else:
        start_tc, origin = default_tc, "default"
    return Source(paths, probes, video, audio, tuple(channels), fps, duration, start_tc, origin)
