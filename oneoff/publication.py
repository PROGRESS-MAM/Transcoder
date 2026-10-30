from __future__ import annotations

import ctypes
import errno
import os
from fractions import Fraction
from pathlib import Path
from typing import Any

from .contracts import (
    AudioFingerprint,
    AudioFingerprinter,
    Candidate,
    ClipAnalysis,
    ClipCollector,
    Diagnosis,
    NoReplaceRename,
    PublicationResult,
    RepairArtifact,
    Verification,
)


_VIDEO_PROPERTIES = (
    "width",
    "height",
    "pix_fmt",
    "sample_aspect_ratio",
    "color_range",
    "color_space",
    "color_transfer",
    "color_primaries",
    "field_order",
)
_AUDIO_PROPERTIES = ("codec_name", "sample_rate", "channels", "channel_layout", "sample_fmt")
_TIMESTAMP_KEYS = ("best_effort_timestamp_time", "pts_time", "pkt_pts_time")


def _fraction(value: Any) -> Fraction | None:
    if value is None or value == "N/A":
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def _streams(raw: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    metadata = raw.get("metadata")
    streams = metadata.get("streams") if isinstance(metadata, dict) else None
    if not isinstance(streams, list):
        return []
    return [item for item in streams if isinstance(item, dict) and item.get("codec_type") == kind]


def _video_stream(raw: dict[str, Any]) -> dict[str, Any] | None:
    streams = _streams(raw, "video")
    return streams[0] if len(streams) == 1 else None


def _frame_time(frame: dict[str, Any]) -> Fraction | None:
    for key in _TIMESTAMP_KEYS:
        value = _fraction(frame.get(key))
        if value is not None:
            return value
    return None


def _same_metadata(source: dict[str, Any], output: dict[str, Any], differences: list[str]) -> None:
    source_meta = source.get("metadata")
    output_meta = output.get("metadata")
    if not isinstance(source_meta, dict) or not isinstance(output_meta, dict):
        differences.append("container metadata is unavailable")
        return

    _compare_format_tags(source_meta, output_meta, differences)
    _compare_chapters(source_meta, output_meta, differences)
    source_streams = source_meta.get("streams")
    output_streams = output_meta.get("streams")
    if not isinstance(source_streams, list) or not isinstance(output_streams, list):
        differences.append("stream metadata is unavailable")
        return
    _compare_stream_metadata(source_streams, output_streams, differences)
    _compare_container_format(source_meta, output_meta, differences)
    _compare_data_packets(source, output, differences)


def _compare_format_tags(source_meta: dict[str, Any], output_meta: dict[str, Any], differences: list[str]) -> None:
    for label, source_object, output_object in (
        ("format", source_meta.get("format"), output_meta.get("format")),
    ):
        if not isinstance(source_object, dict) or not isinstance(output_object, dict):
            differences.append(f"{label} metadata is unavailable")
            continue
        if source_object.get("tags", {}) != output_object.get("tags", {}):
            differences.append(f"{label} metadata tags differ")


def _compare_chapters(source_meta: dict[str, Any], output_meta: dict[str, Any], differences: list[str]) -> None:
    source_chapters = source_meta.get("chapters")
    output_chapters = output_meta.get("chapters")
    if not isinstance(source_chapters, list) or not isinstance(output_chapters, list):
        differences.append("chapter metadata is unavailable")
    elif source_chapters != output_chapters:
        differences.append("chapter metadata differs")


def _compare_stream_metadata(
    source_streams: list[Any], output_streams: list[Any], differences: list[str]
) -> None:
    combined = source_streams + output_streams
    kinds = {
        stream.get("codec_type")
        for stream in combined
        if isinstance(stream, dict) and isinstance(stream.get("codec_type"), str)
    }
    if any(not isinstance(stream, dict) or not isinstance(stream.get("codec_type"), str) for stream in combined):
        differences.append("stream metadata contains invalid entries")
    for kind in sorted(kinds):
        source_group = [item for item in source_streams if isinstance(item, dict) and item.get("codec_type") == kind]
        output_group = [item for item in output_streams if isinstance(item, dict) and item.get("codec_type") == kind]
        _compare_stream_group(kind, source_group, output_group, differences)


def _compare_stream_group(
    kind: str, source_group: list[dict[str, Any]], output_group: list[dict[str, Any]], differences: list[str]
) -> None:
    if len(source_group) != len(output_group):
        differences.append(f"{kind} stream count differs")
        return
    for number, (source_stream, output_stream) in enumerate(zip(source_group, output_group), 1):
        if source_stream.get("tags", {}) != output_stream.get("tags", {}):
            differences.append(f"{kind} stream {number} metadata tags differ")
        if source_stream.get("disposition", {}) != output_stream.get("disposition", {}):
            differences.append(f"{kind} stream {number} disposition differs")
        if kind not in ("video", "audio") and _non_av_stream_differs(source_stream, output_stream):
            differences.append(f"{kind} stream {number} properties differ")


def _non_av_stream_differs(source: dict[str, Any], output: dict[str, Any]) -> bool:
    keys = (
        "codec_name", "codec_tag_string", "time_base", "start_pts", "start_time",
        "duration_ts", "duration", "disposition",
    )
    return any(source.get(key) != output.get(key) for key in keys)


def _compare_container_format(source_meta: dict[str, Any], output_meta: dict[str, Any], differences: list[str]) -> None:
    source_format = source_meta.get("format")
    output_format = output_meta.get("format")
    if isinstance(source_format, dict) and isinstance(output_format, dict):
        if source_format.get("format_name") != output_format.get("format_name"):
            differences.append("container format differs")


def _compare_data_packets(source: dict[str, Any], output: dict[str, Any], differences: list[str]) -> None:
    if not _streams(source, "data"):
        return
    source_packets = source.get("data_packets")
    output_packets = output.get("data_packets")
    if source_packets is None or output_packets is None:
        differences.append("data-stream payload evidence is unavailable")
    elif source_packets != output_packets:
        differences.append("data-stream payload or timing differs")


def _compare_audio(
    source_raw: dict[str, Any],
    output_raw: dict[str, Any],
    audio_fingerprinter: AudioFingerprinter,
    source_path: Path,
    output_path: Path,
    differences: list[str],
) -> None:
    source_streams = _streams(source_raw, "audio")
    output_streams = _streams(output_raw, "audio")
    if len(source_streams) != len(output_streams):
        differences.append("audio stream count differs")
        return
    _compare_audio_streams(source_streams, output_streams, differences)
    fingerprints = _get_audio_fingerprints(
        audio_fingerprinter, source_path, output_path, differences
    )
    if fingerprints is not None:
        _compare_audio_fingerprints(*fingerprints, differences)


def _compare_audio_streams(
    source_streams: list[dict[str, Any]], output_streams: list[dict[str, Any]], differences: list[str]
) -> None:
    for number, (source_stream, output_stream) in enumerate(zip(source_streams, output_streams), 1):
        for key in _AUDIO_PROPERTIES:
            if source_stream.get(key) != output_stream.get(key):
                differences.append(f"audio stream {number} {key} differs")
        if source_stream.get("codec_name") is None or source_stream.get("sample_rate") is None:
            differences.append(f"audio stream {number} properties are incomplete")


def _get_audio_fingerprints(
    audio_fingerprinter: AudioFingerprinter,
    source_path: Path,
    output_path: Path,
    differences: list[str],
) -> tuple[Any, Any] | None:
    try:
        source_fingerprints = audio_fingerprinter(source_path)
        output_fingerprints = audio_fingerprinter(output_path)
    except Exception as exc:
        differences.append(f"audio payload verification failed: {exc}")
        return None
    if not isinstance(source_fingerprints, (tuple, list)) or not isinstance(output_fingerprints, (tuple, list)):
        differences.append("audio fingerprinter returned invalid data")
        return None
    if len(source_fingerprints) != len(output_fingerprints):
        differences.append("audio fingerprint stream count differs")
        return None
    return source_fingerprints, output_fingerprints


def _compare_audio_fingerprints(
    source_fingerprints: list[Any] | tuple[Any, ...],
    output_fingerprints: list[Any] | tuple[Any, ...],
    differences: list[str],
) -> None:
    for number, (source_fp, output_fp) in enumerate(zip(source_fingerprints, output_fingerprints), 1):
        if not isinstance(source_fp, AudioFingerprint) or not isinstance(output_fp, AudioFingerprint):
            differences.append(f"audio stream {number} fingerprint is invalid")
            continue
        if source_fp.properties != output_fp.properties:
            differences.append(f"audio stream {number} fingerprint properties differ")
        if source_fp.packet_hashes != output_fp.packet_hashes:
            differences.append(f"audio stream {number} payload differs")
        if source_fp.packet_timestamps != output_fp.packet_timestamps:
            differences.append(f"audio stream {number} packet timing differs")


def _check_repair_inputs(
    source: ClipAnalysis, diagnosis: Diagnosis, artifact: RepairArtifact, differences: list[str]
) -> None:
    if diagnosis.kind != "confirmed":
        differences.append("repair diagnosis is not confirmed")
    if artifact.command_result.returncode != 0:
        differences.append("encoding command did not complete successfully")
    if diagnosis.decoder_frame_count is None or diagnosis.decoder_frame_count < 1:
        differences.append("complete decoder frame count is unavailable")
    elif artifact.expected_video_frames != diagnosis.decoder_frame_count:
        differences.append("artifact frame count does not match the complete decoder output")
    if diagnosis.frame_rate is None or diagnosis.frame_rate <= 0:
        differences.append("confirmed source frame rate is unavailable")
    source_raw = source.raw
    if source_raw.get("status") != "complete":
        differences.append("complete source probe data is unavailable")
    reported_source = source_raw.get("source")
    if not isinstance(reported_source, str) or not reported_source:
        differences.append("source probe path is unavailable")
        return
    try:
        if Path(reported_source).resolve() != source.candidate.source_path.resolve():
            differences.append("source probe data belongs to a different file")
    except OSError as exc:
        differences.append(f"source path could not be verified: {exc}")


def _load_output_probe(
    artifact: RepairArtifact, clip_collector: ClipCollector, differences: list[str]
) -> dict[str, Any] | None:
    try:
        if not artifact.part_path.is_file():
            differences.append("repair part file is missing")
            output_raw = None
        else:
            output_raw = clip_collector(artifact.part_path)
            if not isinstance(output_raw, dict):
                differences.append("output probe returned invalid data")
                output_raw = None
    except Exception as exc:
        differences.append(f"output probe failed: {exc}")
        output_raw = None
    if output_raw is None and not any("probe" in item or "part file" in item for item in differences):
        differences.append("output probe data is unavailable")
    return output_raw


def _check_output_probe_path(
    artifact: RepairArtifact, output_raw: dict[str, Any] | None, differences: list[str]
) -> None:
    if output_raw is None:
        return
    if output_raw.get("status") != "complete":
        differences.append("complete output probe data is unavailable")
    reported_output = output_raw.get("source")
    if not isinstance(reported_output, str) or not reported_output:
        differences.append("output probe path is unavailable")
        return
    try:
        if Path(reported_output).resolve() != artifact.part_path.resolve():
            differences.append("output probe data belongs to a different file")
    except OSError as exc:
        differences.append(f"output part path could not be verified: {exc}")


def _check_video_data(
    source_raw: dict[str, Any],
    output_raw: dict[str, Any] | None,
    diagnosis: Diagnosis,
    differences: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, Any, Any]:
    source_video = _video_stream(source_raw)
    output_video = _video_stream(output_raw) if output_raw is not None else None
    source_frames = source_raw.get("decoder_frames")
    output_frames = output_raw.get("decoder_frames") if output_raw is not None else None
    if source_video is None:
        differences.append("source must contain exactly one video stream")
    if output_video is None:
        differences.append("output must contain exactly one video stream")
    if not isinstance(source_frames, list) or len(source_frames) != diagnosis.decoder_frame_count:
        differences.append("source decoder output does not match the confirmed frame count")
    if not isinstance(output_frames, list):
        differences.append("output decoder frame data is unavailable")
    elif diagnosis.decoder_frame_count is not None and len(output_frames) != diagnosis.decoder_frame_count:
        differences.append("output frame count differs from the complete decoder output")
    return source_video, output_video, source_frames, output_frames


def _compare_frame_timestamps(
    output_frames: list[Any], fps: Fraction, tolerance: Fraction, differences: list[str]
) -> None:
    times = [_frame_time(frame) for frame in output_frames if isinstance(frame, dict)]
    if len(times) != len(output_frames) or any(value is None for value in times):
        differences.append("output frame timestamps are incomplete")
        return
    checked = [value for value in times if value is not None]
    interval = Fraction(1, 1) / fps
    for previous, current in zip(checked, checked[1:]):
        delta = current - previous
        if delta <= 0:
            differences.append("output frame timestamps collide or move backwards")
            break
        if abs(delta - interval) > tolerance:
            differences.append("output frame intervals are not constant")
            break


def _compare_frame_timecodes(
    source_frames: Any, output_frames: list[Any], differences: list[str]
) -> None:
    if not isinstance(source_frames, list) or len(source_frames) != len(output_frames):
        return
    for number, (source_frame, output_frame) in enumerate(zip(source_frames, output_frames), 1):
        if not isinstance(source_frame, dict) or not isinstance(output_frame, dict):
            break
        if source_frame.get("timecode") != output_frame.get("timecode"):
            differences.append(f"frame {number} timecode differs")
            break
        source_side_data = _timecode_side_data(source_frame)
        output_side_data = _timecode_side_data(output_frame)
        if source_side_data != output_side_data:
            differences.append(f"frame {number} timecode side data differs")
            break


def _timecode_side_data(frame: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item for item in frame.get("side_data_list", [])
        if isinstance(item, dict) and "timecode" in str(item.get("side_data_type", "")).lower()
    ]


def _compare_video_duration(
    output_video: dict[str, Any], output_frames: list[Any], fps: Fraction,
    tolerance: Fraction, differences: list[str]
) -> None:
    duration = _fraction(output_video.get("duration"))
    expected_duration = Fraction(len(output_frames), 1) / fps
    if duration is None:
        differences.append("output video duration is unavailable")
    elif abs(duration - expected_duration) > tolerance:
        differences.append("output video duration differs from expected frame count")


def _compare_output_frames(
    source_frames: Any,
    output_frames: Any,
    output_video: dict[str, Any],
    fps: Fraction | None,
    tolerance: Fraction,
    differences: list[str],
) -> None:
    if not isinstance(output_frames, list) or fps is None or fps <= 0:
        return
    _compare_frame_timestamps(output_frames, fps, tolerance, differences)
    if any(isinstance(frame, dict) and frame.get("pict_type") == "B" for frame in output_frames):
        differences.append("output decoder frames include B-frames")
    _compare_frame_timecodes(source_frames, output_frames, differences)
    _compare_video_duration(output_video, output_frames, fps, tolerance, differences)


def _compare_av_timing(
    source_video: dict[str, Any],
    output_video: dict[str, Any],
    source_raw: dict[str, Any],
    output_raw: dict[str, Any] | None,
    tolerance: Fraction,
    differences: list[str],
) -> None:
    source_start = _fraction(source_video.get("start_time"))
    output_start = _fraction(output_video.get("start_time"))
    if source_start is None or output_start is None:
        differences.append("video start time is unavailable for A/V comparison")
        return
    output_data = output_raw or {}
    for number, (source_audio, output_audio) in enumerate(zip(
        _streams(source_raw, "audio"), _streams(output_data, "audio")
    ), 1):
        source_audio_start = _fraction(source_audio.get("start_time"))
        output_audio_start = _fraction(output_audio.get("start_time"))
        if source_audio_start is None or output_audio_start is None:
            differences.append(f"audio stream {number} start time is unavailable")
            continue
        if abs((source_audio_start - source_start) - (output_audio_start - output_start)) > tolerance:
            differences.append(f"audio stream {number} A/V time relationship differs")


def _compare_video_streams(
    source_video: dict[str, Any],
    output_video: dict[str, Any],
    diagnosis: Diagnosis,
    source_raw: dict[str, Any],
    output_raw: dict[str, Any] | None,
    source_frames: Any,
    output_frames: Any,
    differences: list[str],
) -> None:
    if output_video.get("codec_name") != "h264":
        differences.append("output video codec is not H.264")
    if output_video.get("has_b_frames") not in (0, "0"):
        differences.append("output contains or may contain B-frames")
    for key in _VIDEO_PROPERTIES:
        if source_video.get(key) != output_video.get(key):
            differences.append(f"video {key} differs from source")
    fps = diagnosis.frame_rate
    output_time_base = _fraction(output_video.get("time_base"))
    tolerance = max(output_time_base or Fraction(0), Fraction(1, 1_000_000))
    if fps is not None and fps > 0 and output_time_base is not None and output_time_base >= 1 / fps:
        differences.append("output video time base is too coarse to verify frame timing")
    _compare_output_frames(source_frames, output_frames, output_video, fps, tolerance, differences)
    _compare_av_timing(source_video, output_video, source_raw, output_raw, tolerance, differences)


def verify_repair(
    source: ClipAnalysis,
    diagnosis: Diagnosis,
    artifact: RepairArtifact,
    clip_collector: ClipCollector,
    audio_fingerprinter: AudioFingerprinter,
) -> Verification:
    """Verify a repaired .part file against its source before it can be published."""
    differences: list[str] = []
    source_raw = source.raw
    _check_repair_inputs(source, diagnosis, artifact, differences)
    output_raw = _load_output_probe(artifact, clip_collector, differences)
    _check_output_probe_path(artifact, output_raw, differences)
    source_video, output_video, source_frames, output_frames = _check_video_data(
        source_raw, output_raw, diagnosis, differences
    )
    if source_video is not None and output_video is not None:
        _compare_video_streams(
            source_video, output_video, diagnosis, source_raw, output_raw,
            source_frames, output_frames, differences,
        )
    if output_raw is not None:
        _same_metadata(source_raw, output_raw, differences)
        _compare_audio(
            source_raw, output_raw, audio_fingerprinter,
            source.candidate.source_path, artifact.part_path, differences,
        )
    differences = list(dict.fromkeys(differences))
    return Verification(
        passed=not differences,
        differences=tuple(differences),
        accepted_terminal_discard=diagnosis.accepted_terminal_discard,
        output_raw=output_raw,
    )


def _atomic_no_replace_rename(source: Path, target: Path) -> None:
    """Atomically move a file without replacing an existing destination."""
    if os.name == "nt":
        os.rename(source, target)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        if result == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise OSError(error, os.strerror(error), str(target))

    # Creating a hard link exposes the complete file atomically and fails if the
    # destination exists; both paths are in the same directory by contract.
    os.link(source, target)
    try:
        os.unlink(source)
    except OSError:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def _verification_failure(
    artifact: RepairArtifact, verification: Verification
) -> str | None:
    output_raw = verification.output_raw
    if (
        not verification.passed
        or verification.differences
        or not isinstance(output_raw, dict)
        or output_raw.get("status") != "complete"
        or not isinstance(output_raw.get("decoder_frames"), list)
        or len(output_raw["decoder_frames"]) != artifact.expected_video_frames
    ):
        reason = "; ".join(verification.differences) or "complete verification evidence is missing"
        return f"verification failed: {reason}"
    return None


def _verified_part_failure(artifact: RepairArtifact, verification: Verification) -> str | None:
    reported_output = verification.output_raw.get("source")
    try:
        if not isinstance(reported_output, str) or Path(reported_output).resolve() != artifact.part_path.resolve():
            return "verification evidence does not match the part file"
    except OSError as exc:
        return f"part path could not be verified: {exc}"
    return None


def _candidate_path_failure(candidate: Candidate, artifact: RepairArtifact) -> str | None:
    target = candidate.target_path
    part = artifact.part_path
    if artifact.command_result.returncode != 0:
        return "encoding command did not complete successfully"
    if candidate.source_path.name != target.name:
        return "target filename must exactly match source filename"
    if candidate.source_path.resolve() == target.resolve():
        return "source and target paths must differ"
    if part.parent != target.parent or part.suffix != ".part":
        return "part file must be in the target folder with a .part suffix"
    return None


def _publish_part_file(
    candidate: Candidate, artifact: RepairArtifact, no_replace_rename: NoReplaceRename
) -> PublicationResult:
    target = candidate.target_path
    part = artifact.part_path
    try:
        if not candidate.source_path.is_file():
            return PublicationResult(False, None, part, "source file is missing")
        if not part.is_file():
            return PublicationResult(False, None, part, "verified part file is missing")
        if os.path.lexists(target):
            return PublicationResult(False, None, part, "target already exists")
        no_replace_rename(part, target)
        if not target.is_file() or os.path.lexists(part):
            return PublicationResult(False, None, part, "atomic publication did not complete")
    except FileExistsError:
        return PublicationResult(False, None, part, "target appeared during publication")
    except OSError as exc:
        return PublicationResult(False, None, part, f"publication failed: {exc}")
    return PublicationResult(True, target, part, None)


def publish_repair(
    candidate: Candidate,
    artifact: RepairArtifact,
    verification: Verification,
    no_replace_rename: NoReplaceRename = _atomic_no_replace_rename,
    dry_run: bool = False,
) -> PublicationResult:
    """Publish only a verified artifact, preserving both source and failed .part files."""
    part = artifact.part_path
    if dry_run:
        return PublicationResult(False, None, part, "dry-run: publication skipped")
    failure = _verification_failure(artifact, verification)
    if failure is not None:
        return PublicationResult(False, None, part, failure)
    failure = _verified_part_failure(artifact, verification)
    if failure is not None:
        return PublicationResult(False, None, part, failure)
    failure = _candidate_path_failure(candidate, artifact)
    if failure is not None:
        return PublicationResult(False, None, part, failure)
    return _publish_part_file(candidate, artifact, no_replace_rename)
