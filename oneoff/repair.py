"""Create a repaired candidate without changing its source clip."""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Callable

from .contracts import (
    ClipAnalysis,
    CommandResult,
    Diagnosis,
    FfmpegRunner,
    RepairArtifact,
)


class RepairError(RuntimeError):
    """Raised when a clip cannot be repaired safely."""


def run_ffmpeg(
    argv: tuple[str, ...],
    progress_callback: Callable[[dict[str, str]], None] | None = None,
) -> CommandResult:
    """Run FFmpeg without a shell and retain its measured output statistics."""
    del progress_callback
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return CommandResult(tuple(argv), result.returncode, result.stderr)


def _video_stream(clip: ClipAnalysis) -> tuple[dict[str, object], list[dict[str, object]]]:
    streams = clip.raw.get("streams")
    if not isinstance(streams, list) or not streams:
        raise RepairError("Probe data has no stream list.")
    if any(not isinstance(item, dict) for item in streams):
        raise RepairError("Probe data contains an invalid stream entry.")
    valid_streams = streams
    all_videos = [item for item in valid_streams if item.get("codec_type") == "video"]
    videos = [
        item
        for item in all_videos
        if not bool((item.get("disposition") or {}).get("attached_pic", 0))
    ]
    if len(all_videos) != 1 or len(videos) != 1:
        raise RepairError("Repair requires exactly one video stream and no cover stream.")
    return videos[0], valid_streams


def _positive_int(value: object, label: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RepairError(f"Missing or invalid {label}.") from exc
    if number <= 0:
        raise RepairError(f"Missing or invalid {label}.")
    return number


def _optional_ratio(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or value in {"N/A", "0:1"}:
        return None
    try:
        ratio = Fraction(value.replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        return None
    if ratio <= 0:
        return None
    return f"{ratio.numerator}/{ratio.denominator}"


def _rate(diagnosis: Diagnosis) -> Fraction:
    rate = diagnosis.frame_rate
    if not isinstance(rate, Fraction) or rate <= 0:
        raise RepairError("A confirmed positive source frame rate is required.")
    return rate


def _stream_metadata_args(streams: list[dict[str, object]]) -> list[str]:
    type_names = {
        "video": "v",
        "audio": "a",
        "subtitle": "s",
        "data": "d",
        "attachment": "t",
    }
    typed_indices: dict[str, int] = {}
    result: list[str] = []
    for output_stream in streams:
        kind = output_stream.get("codec_type")
        specifier = type_names.get(kind) if isinstance(kind, str) else None
        raw_index = output_stream.get("index")
        if specifier is None or raw_index is None:
            continue
        try:
            source_index = int(str(raw_index))
        except (TypeError, ValueError):
            continue
        output_index = typed_indices.get(specifier, 0)
        typed_indices[specifier] = output_index + 1
        result.extend(
            [
                f"-map_metadata:s:{specifier}:{output_index}",
                f"0:{source_index}",
            ]
        )
    return result


def _container_format(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    formats = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}
    if suffix not in formats:
        raise RepairError(f"Unsupported source container extension: {suffix or '(none)'}.")
    return formats[suffix]


def _build_command(
    clip: ClipAnalysis,
    video: dict[str, object],
    streams: list[dict[str, object]],
    rate: Fraction,
    bitrate: int,
    part_path: Path,
) -> tuple[str, ...]:
    sar = _optional_ratio(video.get("sample_aspect_ratio"))
    filters = [f"settb=expr={rate.denominator}/{rate.numerator}", "setpts=N"]
    if sar:
        filters.append(f"setsar={sar}")

    args = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-n",
        "-copyts",
        "-i",
        str(clip.candidate.source_path),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-copy_unknown",
        "-c",
        "copy",
        "-c:a",
        "copy",
        "-c:v:0",
        "libx264",
        "-bf:v:0",
        "0",
        "-b:v:0",
        str(bitrate),
        "-pix_fmt:v:0",
        str(video["pix_fmt"]),
        "-vf:v:0",
        ",".join(filters),
        "-fps_mode:v:0",
        "passthrough",
        "-enc_time_base:v:0",
        f"{rate.denominator}/{rate.numerator}",
        "-avoid_negative_ts",
        "disabled",
    ]
    args.extend(_stream_metadata_args(streams))

    color_options = (
        ("color_range", "-color_range:v:0"),
        ("color_primaries", "-color_primaries:v:0"),
        ("color_transfer", "-color_trc:v:0"),
        ("color_space", "-colorspace:v:0"),
    )
    for source_key, option in color_options:
        value = video.get(source_key)
        if source_key == "color_transfer" and value is None:
            value = video.get("color_trc")
        if isinstance(value, str) and value.strip() and value not in {"unknown", "N/A"}:
            args.extend([option, value])

    # FFmpeg's normal final statistics include the measured output bitrate.
    args.extend(["-stats", "-f", _container_format(clip.candidate.source_path), str(part_path)])
    return tuple(args)


def encode_repair(
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    part_path: Path,
    runner: FfmpegRunner = run_ffmpeg,
) -> RepairArtifact:
    """Encode one confirmed clip to a fresh .part candidate, never the source."""
    if diagnosis.kind != "confirmed":
        raise RepairError("Only confirmed cases may be repaired.")
    if diagnosis.decoder_frame_count is None or diagnosis.decoder_frame_count <= 0:
        raise RepairError("A positive normal-decoder frame count is required.")

    source_path = clip.candidate.source_path
    if not source_path.is_file():
        raise RepairError("Source clip is not a readable file.")
    if part_path.suffix.lower() != ".part":
        raise RepairError("Repair output must be a .part candidate path.")
    if part_path.exists() or part_path.is_symlink():
        raise RepairError("Repair candidate already exists; refusing to overwrite it.")
    if part_path.resolve() == source_path.resolve():
        raise RepairError("Repair candidate must not be the source clip.")
    if not part_path.parent.is_dir():
        raise RepairError("Repair candidate directory does not exist.")

    video, streams = _video_stream(clip)
    if video.get("codec_name") != "h264":
        raise RepairError("Confirmed repair requires an H.264 source video stream.")
    bitrate = _positive_int(video.get("bit_rate"), "source video bitrate")
    _positive_int(video.get("width"), "source video width")
    _positive_int(video.get("height"), "source video height")
    pixel_format = video.get("pix_fmt")
    if not isinstance(pixel_format, str) or not pixel_format.strip() or pixel_format == "unknown":
        raise RepairError("Missing or invalid source pixel format.")

    rate = _rate(diagnosis)
    argv = _build_command(
        clip,
        video,
        streams,
        rate,
        bitrate,
        part_path,
    )
    result = runner(argv, None)
    if result.returncode != 0:
        raise RepairError(f"FFmpeg repair failed with exit code {result.returncode}: {result.stderr}")
    if not part_path.is_file() or part_path.stat().st_size <= 0:
        raise RepairError("FFmpeg reported success but did not create a non-empty candidate.")
    return RepairArtifact(part_path, diagnosis.decoder_frame_count, result)
