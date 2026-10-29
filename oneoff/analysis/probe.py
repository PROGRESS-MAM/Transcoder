"""Collect read-only ffprobe data for a media clip."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]


class ProbeError(RuntimeError):
    """Raised when a clip cannot be safely inspected."""


def _resolve_tool(tool_name: str, repo_root: Path | None = None) -> str:
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    bundled_path = root / "res" / f"{tool_name}.exe"
    if bundled_path.is_file():
        return str(bundled_path.resolve())

    path_tool = shutil.which(tool_name)
    if path_tool:
        return path_tool
    raise ProbeError(f"Required tool '{tool_name}' was not found in res/ or on PATH.")


_FORMAT_FIELDS = (
    "filename",
    "nb_streams",
    "nb_programs",
    "format_name",
    "format_long_name",
    "start_time",
    "duration",
    "size",
    "bit_rate",
    "probe_score",
)
_STREAM_FIELDS = (
    "index",
    "codec_name",
    "codec_long_name",
    "profile",
    "codec_type",
    "codec_tag_string",
    "codec_tag",
    "width",
    "height",
    "coded_width",
    "coded_height",
    "pix_fmt",
    "level",
    "field_order",
    "r_frame_rate",
    "avg_frame_rate",
    "time_base",
    "start_pts",
    "start_time",
    "duration_ts",
    "duration",
    "bit_rate",
    "nb_frames",
    "sample_fmt",
    "sample_rate",
    "channels",
    "channel_layout",
    "bits_per_sample",
    "bits_per_raw_sample",
    "color_range",
    "color_space",
    "color_transfer",
    "color_primaries",
    "extradata_size",
)
_CHAPTER_FIELDS = ("id", "time_base", "start", "start_time", "end", "end_time")
_PACKET_FIELDS = ("pts", "dts", "duration", "pos", "size", "flags")
_FRAME_FIELDS = (
    "pict_type",
    "key_frame",
    "pts",
    "pkt_dts",
    "best_effort_timestamp",
    "pkt_pos",
    "coded_picture_number",
    "display_picture_number",
)


def _known(value: Any) -> Any:
    if value is None or value == "":
        return "unknown"
    return value


def _complete_record(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = dict(record)
    for field in fields:
        result[field] = _known(record.get(field))
    return result


def _command_diagnostics(stderr: str) -> dict[str, Any]:
    return {"stderr": stderr, "warnings": stderr.splitlines()}


def _run(
    command_name: str,
    args: list[str],
    commands: dict[str, list[str]],
    diagnostics: dict[str, dict[str, Any]],
    tool_label: str,
) -> subprocess.CompletedProcess[str]:
    commands[command_name] = args
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise ProbeError(f"Could not run {tool_label}: {exc}") from None

    diagnostics[command_name] = _command_diagnostics(result.stderr)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        if len(detail) > 1000:
            detail = detail[:997] + "..."
        raise ProbeError(f"{tool_label} failed: {detail}")
    return result


def _run_json(
    command_name: str,
    args: list[str],
    commands: dict[str, list[str]],
    diagnostics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    completed = _run(command_name, args, commands, diagnostics, "ffprobe")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {command_name}: {exc.msg}") from None
    if not isinstance(payload, dict):
        raise ProbeError(f"ffprobe returned an invalid response for {command_name}")
    return payload


def _readable_path(path: Path) -> tuple[Path, int]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
        if not resolved.is_file():
            raise ProbeError(f"Media path is not a regular file: {resolved}")
        with resolved.open("rb") as source:
            source.read(1)
    except ProbeError:
        raise
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise ProbeError(f"Cannot read media file '{path}': {detail}") from None
    return resolved, stat.st_size


def _inventory(payload: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    raw_format = payload.get("format")
    if not isinstance(raw_format, dict):
        raw_format = {}
    format_record = _complete_record(raw_format, _FORMAT_FIELDS)
    format_record["tags"] = raw_format.get("tags", {})

    raw_streams = payload.get("streams", [])
    if not isinstance(raw_streams, list):
        raw_streams = []
    streams: list[dict[str, Any]] = []
    metadata_streams: dict[str, Any] = {}
    timecode_streams: dict[str, Any] = {}
    declared_counts: dict[str, Any] = {}
    video_indices: set[int] = set()
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, dict):
            continue
        stream = _complete_record(raw_stream, _STREAM_FIELDS)
        stream["tags"] = raw_stream.get("tags", {})
        streams.append(stream)
        index = raw_stream.get("index", "unknown")
        index_key = str(index)
        metadata_streams[index_key] = raw_stream.get("tags", {})
        timecode_streams[index_key] = _known((raw_stream.get("tags") or {}).get("timecode"))
        declared_counts[index_key] = _known(raw_stream.get("nb_frames"))
        if raw_stream.get("codec_type") == "video" and isinstance(index, int):
            video_indices.add(index)

    raw_chapters = payload.get("chapters", [])
    if not isinstance(raw_chapters, list):
        raw_chapters = []
    chapters = []
    metadata_chapters: dict[str, Any] = {}
    for position, chapter in enumerate(raw_chapters):
        if not isinstance(chapter, dict):
            continue
        chapters.append(_complete_record(chapter, _CHAPTER_FIELDS))
        metadata_chapters[str(position)] = chapter.get("tags", {})

    frame_counts = dict(declared_counts)
    for index in video_indices:
        frame_counts[str(index)] = 0
    for frame in frames:
        index = frame.get("stream_index")
        if isinstance(index, int):
            frame_counts[str(index)] = frame_counts.get(str(index), 0) + 1

    return {
        "format": format_record,
        "streams": streams,
        "chapters": chapters,
        "metadata": {
            "format": raw_format.get("tags", {}),
            "streams": metadata_streams,
            "chapters": metadata_chapters,
        },
        "timecodes": {
            "format": _known((raw_format.get("tags") or {}).get("timecode")),
            "streams": timecode_streams,
        },
        "frame_counts": {
            "per_stream": frame_counts,
            "decoded_video_total": len(frames),
        },
    }


def collect_clip(path: Path) -> dict[str, Any]:
    """Read a complete clip's inventory, video packets, and decoded frames."""
    source_path, file_size = _readable_path(Path(path))
    ffprobe = _resolve_tool("ffprobe")
    ffmpeg = _resolve_tool("ffmpeg")

    commands: dict[str, list[str]] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    ffprobe_version_output = _run(
        "ffprobe_version", [ffprobe, "-version"], commands, diagnostics, "ffprobe"
    ).stdout
    ffmpeg_version_output = _run(
        "ffmpeg_version", [ffmpeg, "-version"], commands, diagnostics, "ffmpeg"
    ).stdout

    inventory_data = _run_json(
        "inventory",
        [
            ffprobe,
            "-v",
            "warning",
            "-of",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(source_path),
        ],
        commands,
        diagnostics,
    )
    if not inventory_data.get("format") and not inventory_data.get("streams"):
        raise ProbeError(f"ffprobe found no media streams in '{source_path}'.")

    packet_data = _run_json(
        "packets",
        [
            ffprobe,
            "-v",
            "warning",
            "-of",
            "json",
            "-select_streams",
            "v",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,pts,dts,duration,pos,size,flags",
            str(source_path),
        ],
        commands,
        diagnostics,
    )
    streams_by_index = {
        stream.get("index"): stream
        for stream in inventory_data.get("streams", [])
        if isinstance(stream, dict)
    }
    packets: list[dict[str, Any]] = []
    for packet in packet_data.get("packets", []):
        if not isinstance(packet, dict):
            continue
        stream_index = packet.get("stream_index", "unknown")
        stream = streams_by_index.get(stream_index, {})
        packets.append(
            {
                "index": len(packets),
                "stream_index": _known(stream_index),
                **{field: _known(packet.get(field)) for field in _PACKET_FIELDS},
                "time_base": _known(stream.get("time_base")),
            }
        )

    frame_data = _run_json(
        "frames",
        [
            ffprobe,
            "-v",
            "warning",
            "-of",
            "json",
            "-select_streams",
            "v",
            "-show_frames",
            "-show_entries",
            "frame=stream_index,pict_type,key_frame,pts,pkt_dts,best_effort_timestamp,duration,pkt_duration,pkt_pos,coded_picture_number,display_picture_number",
            str(source_path),
        ],
        commands,
        diagnostics,
    )
    frames: list[dict[str, Any]] = []
    for frame in frame_data.get("frames", []):
        if not isinstance(frame, dict):
            continue
        duration = frame.get("duration")
        if duration is None or duration == "":
            duration = frame.get("pkt_duration")
        frames.append(
            {
                "index": len(frames),
                "stream_index": _known(frame.get("stream_index")),
                **{field: _known(frame.get(field)) for field in _FRAME_FIELDS},
                "duration": _known(duration),
                "pkt_duration": _known(frame.get("pkt_duration")),
            }
        )

    return {
        "file": {
            "name": source_path.name,
            "abs_path": str(source_path),
            "size": file_size,
        },
        "analysed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tools": {
            "ffprobe": ffprobe_version_output.splitlines()[0] if ffprobe_version_output else "unknown",
            "ffmpeg": ffmpeg_version_output.splitlines()[0] if ffmpeg_version_output else "unknown",
        },
        "tool_paths": {"ffprobe": ffprobe, "ffmpeg": ffmpeg},
        "commands": commands,
        "diagnostics": diagnostics,
        "inventory": _inventory(inventory_data, frames),
        "packets": packets,
        "frames": frames,
    }
