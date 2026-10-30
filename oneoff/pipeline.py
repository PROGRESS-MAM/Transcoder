"""Orchestrate one-off clip analysis, repair, verification, and reporting."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .analysis.probe import collect_clip
from .config import build_policy
from .contracts import (
    AppConfig,
    AudioFingerprint,
    Candidate,
    ClipAnalysis,
    ClipOutcome,
    CommandResult,
    Diagnosis,
    RepairArtifact,
    PublicationResult,
    ReportEvent,
    RunReport,
    RunSummary,
    Selection,
    Verification,
)
from .diagnosis import classify
from .inventory import collect_candidate, select_candidates
from .publication import publish_repair, verify_repair
from .repair import encode_repair, run_ffmpeg
from .run_report import (
    finish_run,
    process_candidates,
    record_event,
    start_run,
)
from .tool_resolution import ToolResolution, resolve_tools


class PipelineError(RuntimeError):
    """Raised when a run cannot proceed past a global pipeline step."""

    def __init__(self, message: str, report: RunReport | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class PipelineResult:
    report: RunReport
    summary: RunSummary


ClipCollector = Callable[[Path], dict[str, object]]
FfmpegRunner = Callable[
    [tuple[str, ...], Callable[[dict[str, str]], None] | None], CommandResult
]
AudioFingerprinter = Callable[[Path], tuple[AudioFingerprint, ...]]


def _int_or_none(value: object) -> int | None:
    if value is None or value == "N/A":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ffprobe packet timestamp: {value!r}") from exc


def _audio_probe_command(ffprobe_path: Path, path: Path) -> tuple[str, ...]:
    return (
        str(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_streams",
        "-show_packets",
        "-show_data_hash",
        "sha256",
        "-show_entries",
        "stream=index,codec_name,sample_rate,channels,channel_layout,sample_fmt:packet=stream_index,pts,dts,duration,data_hash",
        "-of",
        "json",
        str(path),
    )


def _run_audio_probe(argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"ffprobe exited with {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout


def _load_audio_probe_data(stdout: str) -> tuple[list[object], list[object]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid audio fingerprint JSON") from exc

    streams = payload.get("streams", [])
    packets = payload.get("packets", [])
    if not isinstance(streams, list) or not isinstance(packets, list):
        raise RuntimeError("ffprobe returned invalid audio fingerprint data")
    return streams, packets


def _fingerprint_audio_stream(stream: object, packets: list[object]) -> AudioFingerprint:
    if not isinstance(stream, dict) or not isinstance(stream.get("index"), int):
        raise RuntimeError("ffprobe returned an audio stream without an index")
    stream_index = stream["index"]
    stream_packets = [
        packet
        for packet in packets
        if isinstance(packet, dict) and packet.get("stream_index") == stream_index
    ]
    hashes = []
    timestamps = []
    for packet in stream_packets:
        packet_hash = packet.get("data_hash")
        if not isinstance(packet_hash, str) or not packet_hash:
            raise RuntimeError(f"ffprobe returned no payload hash for audio stream {stream_index}")
        hashes.append(packet_hash)
        timestamps.append(
            (
                _int_or_none(packet.get("pts")),
                _int_or_none(packet.get("dts")),
                _int_or_none(packet.get("duration")),
            )
        )
    if not stream_packets:
        raise RuntimeError(f"ffprobe returned no packets for audio stream {stream_index}")
    properties = {
        key: stream.get(key)
        for key in ("codec_name", "sample_rate", "channels", "channel_layout", "sample_fmt")
    }
    return AudioFingerprint(
        stream_index=stream_index,
        properties=properties,
        packet_hashes=tuple(hashes),
        packet_timestamps=tuple(timestamps),
    )


def _make_audio_fingerprinter(ffprobe_path: Path) -> AudioFingerprinter:
    def fingerprint(path: Path) -> tuple[AudioFingerprint, ...]:
        argv = _audio_probe_command(ffprobe_path, path)
        stdout = _run_audio_probe(argv)
        streams, packets = _load_audio_probe_data(stdout)
        return tuple(_fingerprint_audio_stream(stream, packets) for stream in streams)

    return fingerprint


def _repair_input(clip: ClipAnalysis) -> ClipAnalysis:
    """Expose stream metadata in the shape expected by the repair module."""
    metadata = clip.raw.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("streams"), list):
        return clip
    raw = dict(clip.raw)
    raw["streams"] = metadata["streams"]
    return ClipAnalysis(candidate=clip.candidate, raw=raw)


def _part_path(report: RunReport, candidate: Candidate, sequence: int) -> Path:
    target = candidate.target_path
    return target.with_name(f"{target.name}.{report.run_dir.name}.{sequence:06d}.part")


def _outcome(
    candidate: Candidate,
    status: str,
    reason: str,
    *,
    clip: ClipAnalysis | None = None,
    diagnosis: Diagnosis | None = None,
    artifact: RepairArtifact | None = None,
    verification: Verification | None = None,
) -> ClipOutcome:
    # Report consumers support additional statuses; this keeps dry runs distinct from publication.
    return ClipOutcome(
        candidate=candidate,
        clip=clip,
        diagnosis=diagnosis,
        artifact=artifact,
        verification=verification,
        status=status,  # type: ignore[arg-type]
        reason=reason,
    )


def _collect_clip(
    report: RunReport,
    candidate: Candidate,
    clip_collector: ClipCollector,
) -> ClipAnalysis | ClipOutcome:
    try:
        clip = collect_candidate(candidate, collector=clip_collector)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_event(report, ReportEvent("probe", "failed", candidate.source_path, reason, {}))
        return _outcome(candidate, "analysis_error", reason)
    record_event(
        report,
        ReportEvent(
            "probe",
            "complete",
            candidate.source_path,
            "Clip-Erfassung abgeschlossen",
            {"status": clip.raw.get("status")},
        ),
    )
    return clip


def _classify_clip(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
) -> Diagnosis | ClipOutcome:
    try:
        diagnosis = classify(clip)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_event(report, ReportEvent("diagnosis", "failed", candidate.source_path, reason, {}))
        return _outcome(candidate, "analysis_error", reason, clip=clip)
    record_event(
        report,
        ReportEvent(
            "diagnosis",
            diagnosis.kind,
            candidate.source_path,
            diagnosis.reason,
            {"diagnosis": diagnosis},
        ),
    )
    return diagnosis


def _skip_unconfirmed_clip(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
) -> ClipOutcome | None:
    if diagnosis.kind == "confirmed":
        return None
    record_event(
        report,
        ReportEvent(
            "repair",
            "not_required",
            candidate.source_path,
            "Clip wird nicht repariert, da kein bestätigter Reparaturfall vorliegt.",
            {"diagnosis": diagnosis.kind},
        ),
    )
    return _outcome(candidate, diagnosis.kind, diagnosis.reason, clip=clip, diagnosis=diagnosis)


def _dry_run_outcome(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
) -> ClipOutcome:
    reason = "Bestätigter Reparaturfall; im Dry-Run wird keine Datei erstellt oder verändert."
    record_event(
        report,
        ReportEvent(
            "repair",
            "dry_run",
            candidate.source_path,
            reason,
            {"diagnosis": diagnosis.kind, "target_path": candidate.target_path},
        ),
    )
    return _outcome(candidate, "dry_run", reason, clip=clip, diagnosis=diagnosis)


def _encode_candidate_repair(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    sequence: int,
    ffmpeg_runner: FfmpegRunner,
) -> RepairArtifact | ClipOutcome:
    part_path = _part_path(report, candidate, sequence)
    try:
        artifact = encode_repair(_repair_input(clip), diagnosis, part_path, runner=ffmpeg_runner)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_event(
            report,
            ReportEvent("repair", "failed", candidate.source_path, reason, {"part_path": part_path}),
        )
        return _outcome(candidate, "repair_error", reason, clip=clip, diagnosis=diagnosis)
    record_event(
        report,
        ReportEvent(
            "repair",
            "complete",
            candidate.source_path,
            "Reparaturkandidat erstellt",
            {"part_path": artifact.part_path, "expected_video_frames": artifact.expected_video_frames},
        ),
    )
    return artifact


def _record_verification(
    report: RunReport,
    candidate: Candidate,
    verification: Verification,
) -> bool:
    passed = verification.passed and not verification.differences
    record_event(
        report,
        ReportEvent(
            "verification",
            "passed" if passed else "failed",
            candidate.source_path,
            "Ausgabeprüfung abgeschlossen",
            {"verification": verification},
        ),
    )
    return passed


def _verification_outcome(
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    artifact: RepairArtifact,
    verification: Verification,
) -> ClipOutcome:
    reason = "; ".join(verification.differences) or "Ausgabeprüfung nicht bestanden"
    return _outcome(
        candidate,
        "verification_error",
        reason,
        clip=clip,
        diagnosis=diagnosis,
        artifact=artifact,
        verification=verification,
    )


def _verify_candidate_repair(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    artifact: RepairArtifact,
    clip_collector: ClipCollector,
    audio_fingerprinter: AudioFingerprinter,
) -> Verification | ClipOutcome:
    try:
        verification = verify_repair(
            clip,
            diagnosis,
            artifact,
            clip_collector=clip_collector,
            audio_fingerprinter=audio_fingerprinter,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_event(report, ReportEvent("verification", "failed", candidate.source_path, reason, {}))
        return _outcome(
            candidate,
            "verification_error",
            reason,
            clip=clip,
            diagnosis=diagnosis,
            artifact=artifact,
        )
    if _record_verification(report, candidate, verification):
        return verification
    return _verification_outcome(candidate, clip, diagnosis, artifact, verification)


def _record_publication(
    report: RunReport,
    candidate: Candidate,
    publication: PublicationResult,
    dry_run: bool,
) -> None:
    status = "published" if publication.published else ("dry_run" if dry_run else "failed")
    reason = publication.reason or ("Veröffentlicht" if publication.published else "Nicht veröffentlicht")
    record_event(
        report,
        ReportEvent(
            "publication",
            status,
            candidate.source_path,
            reason,
            {"final_path": publication.final_path, "part_path": publication.part_path},
        ),
    )


def _publication_outcome(
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    artifact: RepairArtifact,
    verification: Verification,
    publication: PublicationResult,
    dry_run: bool,
) -> ClipOutcome:
    if publication.published and publication.final_path is not None:
        return _outcome(
            candidate,
            "published",
            "Verifiziert und veröffentlicht.",
            clip=clip,
            diagnosis=diagnosis,
            artifact=artifact,
            verification=verification,
        )
    return _outcome(
        candidate,
        "dry_run" if dry_run else "publication_error",
        publication.reason or "Nicht veröffentlicht",
        clip=clip,
        diagnosis=diagnosis,
        artifact=artifact,
        verification=verification,
    )


def _publish_candidate_repair(
    report: RunReport,
    candidate: Candidate,
    clip: ClipAnalysis,
    diagnosis: Diagnosis,
    artifact: RepairArtifact,
    verification: Verification,
    dry_run: bool,
) -> ClipOutcome:
    try:
        publication = publish_repair(candidate, artifact, verification, dry_run=dry_run)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_event(report, ReportEvent("publication", "failed", candidate.source_path, reason, {}))
        return _outcome(
            candidate,
            "publication_error",
            reason,
            clip=clip,
            diagnosis=diagnosis,
            artifact=artifact,
            verification=verification,
        )
    _record_publication(report, candidate, publication, dry_run)
    return _publication_outcome(
        candidate, clip, diagnosis, artifact, verification, publication, dry_run
    )


def _process_candidate(
    report: RunReport,
    candidate: Candidate,
    *,
    dry_run: bool,
    sequence: int,
    clip_collector: ClipCollector,
    ffmpeg_runner: FfmpegRunner,
    audio_fingerprinter: AudioFingerprinter,
) -> ClipOutcome:
    clip = _collect_clip(report, candidate, clip_collector)
    if isinstance(clip, ClipOutcome):
        return clip
    diagnosis = _classify_clip(report, candidate, clip)
    if isinstance(diagnosis, ClipOutcome):
        return diagnosis
    skipped = _skip_unconfirmed_clip(report, candidate, clip, diagnosis)
    if skipped is not None:
        return skipped
    if dry_run:
        return _dry_run_outcome(report, candidate, clip, diagnosis)
    artifact = _encode_candidate_repair(
        report, candidate, clip, diagnosis, sequence, ffmpeg_runner
    )
    if isinstance(artifact, ClipOutcome):
        return artifact
    verification = _verify_candidate_repair(
        report, candidate, clip, diagnosis, artifact, clip_collector, audio_fingerprinter
    )
    if isinstance(verification, ClipOutcome):
        return verification
    return _publish_candidate_repair(
        report, candidate, clip, diagnosis, artifact, verification, dry_run
    )


def _clip_processor(
    report: RunReport,
    *,
    dry_run: bool,
    clip_collector: ClipCollector,
    ffmpeg_runner: FfmpegRunner,
    audio_fingerprinter: AudioFingerprinter,
) -> Callable[[Candidate], ClipOutcome]:
    sequence = 0

    def process(candidate: Candidate) -> ClipOutcome:
        nonlocal sequence
        sequence += 1
        return _process_candidate(
            report,
            candidate,
            dry_run=dry_run,
            sequence=sequence,
            clip_collector=clip_collector,
            ffmpeg_runner=ffmpeg_runner,
            audio_fingerprinter=audio_fingerprinter,
        )

    return process


def _resolve_pipeline_tools(
    config: AppConfig,
    resolver: Callable[[AppConfig], ToolResolution],
    argv: tuple[str, ...],
    dry_run: bool,
    output_callback: Callable[[str], None] | None,
) -> ToolResolution:
    try:
        return resolver(config)
    except Exception as exc:
        report = start_run(
            config,
            {"ffmpeg": "unavailable", "ffprobe": "unavailable"},
            argv,
            dry_run=dry_run,
            output_callback=output_callback,
        )
        reason = f"{type(exc).__name__}: {exc}"
        record_event(report, ReportEvent("tools", "failed", None, reason, {}))
        record_event(report, ReportEvent("run", "failed", None, reason, {}))
        finish_run(report, RunSummary(counts={}))
        raise PipelineError(f"Werkzeugauflösung fehlgeschlagen: {reason}", report) from exc


def _start_pipeline_report(
    config: AppConfig,
    tools: ToolResolution,
    argv: tuple[str, ...],
    dry_run: bool,
    output_callback: Callable[[str], None] | None,
) -> RunReport:
    return start_run(
        config,
        {
            "ffmpeg": tools.ffmpeg_version or "unavailable",
            "ffprobe": tools.ffprobe_version or "unavailable",
        },
        argv,
        dry_run=dry_run,
        output_callback=output_callback,
    )


def _record_inventory(report: RunReport, selection: Selection) -> None:
    record_event(
        report,
        ReportEvent(
            "inventory",
            "complete",
            None,
            "Auswahl und Inventar abgeschlossen",
            {
                "candidate_count": len(selection.candidates),
                "skipped_count": len(selection.skipped),
                "missing_test_sources": selection.missing_test_sources,
            },
        ),
    )
    for skipped in selection.skipped:
        status = "target_exists" if skipped.reason == "matching_final_target_exists" else "skipped"
        record_event(report, ReportEvent("inventory", status, skipped.source_path, skipped.reason, {}))
    for missing in selection.missing_test_sources:
        record_event(
            report,
            ReportEvent("inventory", "missing_test_source", missing, "Testquelle fehlt.", {}),
        )


def _resolve_ffmpeg_command(
    command: tuple[str, ...],
    ffmpeg_path: Path,
) -> tuple[tuple[str, ...], Path | None]:
    resolved_command = (str(ffmpeg_path), *command[1:])
    source_path = None
    if "-i" in resolved_command:
        input_index = resolved_command.index("-i") + 1
        if input_index < len(resolved_command):
            source_path = Path(resolved_command[input_index])
    return resolved_command, source_path


def _record_ffmpeg_progress(
    report: RunReport,
    source_path: Path | None,
    values: dict[str, str],
) -> None:
    record_event(
        report,
        ReportEvent("repair_command", "progress", source_path, "FFmpeg-Fortschritt", {"values": values}),
    )


def _record_ffmpeg_result(
    report: RunReport,
    source_path: Path | None,
    result: CommandResult,
) -> None:
    status = "complete" if result.returncode == 0 else "failed"
    reason = "FFmpeg-Reparatur abgeschlossen" if result.returncode == 0 else "FFmpeg-Reparatur fehlgeschlagen"
    record_event(
        report,
        ReportEvent(
            "repair_command",
            status,
            source_path,
            reason,
            {"argv": result.argv, "returncode": result.returncode, "stderr": result.stderr},
        ),
    )


def _run_ffmpeg_with_events(
    report: RunReport,
    runner: FfmpegRunner,
    command: tuple[str, ...],
    source_path: Path | None,
    progress_callback: Callable[[dict[str, str]], None] | None,
) -> CommandResult:
    record_event(
        report,
        ReportEvent("repair_command", "started", source_path, "FFmpeg-Reparatur gestartet", {"argv": command}),
    )

    def report_progress(values: dict[str, str]) -> None:
        _record_ffmpeg_progress(report, source_path, values)

    callback = progress_callback or report_progress
    try:
        result = runner(command, callback)
    except Exception as exc:
        record_event(
            report,
            ReportEvent(
                "repair_command",
                "failed",
                source_path,
                f"{type(exc).__name__}: {exc}",
                {},
            ),
        )
        raise
    _record_ffmpeg_result(report, source_path, result)
    return result


def _run_resolved_ffmpeg(
    report: RunReport,
    ffmpeg_path: Path,
    runner: FfmpegRunner,
    command: tuple[str, ...],
    progress_callback: Callable[[dict[str, str]], None] | None = None,
) -> CommandResult:
    resolved_command, source_path = _resolve_ffmpeg_command(command, ffmpeg_path)
    return _run_ffmpeg_with_events(
        report, runner, resolved_command, source_path, progress_callback
    )


def _make_resolved_runner(
    report: RunReport,
    ffmpeg_path: Path,
    runner: FfmpegRunner,
) -> FfmpegRunner:
    def resolved_runner(
        command: tuple[str, ...],
        progress_callback: Callable[[dict[str, str]], None] | None = None,
    ) -> CommandResult:
        return _run_resolved_ffmpeg(
            report, ffmpeg_path, runner, command, progress_callback
        )

    return resolved_runner


def _run_candidates(
    report: RunReport,
    config: AppConfig,
    tools: ToolResolution,
    dry_run: bool,
    clip_collector: ClipCollector,
    ffmpeg_runner: FfmpegRunner | None,
    audio_fingerprinter: AudioFingerprinter | None,
) -> RunSummary:
    record_event(report, ReportEvent("tools", "complete" if tools.ok else "failed", None, "Werkzeugprüfung abgeschlossen", {"errors": tools.errors}))
    if not tools.ok or tools.ffmpeg_path is None or tools.ffprobe_path is None:
        raise PipelineError("Werkzeugauflösung fehlgeschlagen: " + "; ".join(tools.errors), report)
    policy = build_policy(config)
    selection = select_candidates(config, policy)
    _record_inventory(report, selection)
    runner = ffmpeg_runner or run_ffmpeg
    resolved_runner = _make_resolved_runner(report, tools.ffmpeg_path, runner)
    fingerprinter = audio_fingerprinter or _make_audio_fingerprinter(tools.ffprobe_path)
    processor = _clip_processor(
        report,
        dry_run=dry_run,
        clip_collector=clip_collector,
        ffmpeg_runner=resolved_runner,
        audio_fingerprinter=fingerprinter,
    )
    return process_candidates(report, selection.candidates, processor)


def _execute_started_pipeline(
    report: RunReport,
    config: AppConfig,
    tools: ToolResolution,
    dry_run: bool,
    clip_collector: ClipCollector,
    ffmpeg_runner: FfmpegRunner | None,
    audio_fingerprinter: AudioFingerprinter | None,
) -> PipelineResult:
    summary = RunSummary(counts={})
    try:
        summary = _run_candidates(
            report,
            config,
            tools,
            dry_run,
            clip_collector,
            ffmpeg_runner,
            audio_fingerprinter,
        )
        finish_run(report, summary)
        return PipelineResult(report=report, summary=summary)
    except Exception as exc:
        record_event(
            report,
            ReportEvent("run", "failed", None, f"{type(exc).__name__}: {exc}", {}),
        )
        finish_run(report, summary)
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(f"Pipeline failed: {type(exc).__name__}: {exc}", report) from exc


def run_pipeline(
    config: AppConfig,
    *,
    dry_run: bool = False,
    argv: tuple[str, ...] = (),
    tool_resolver: Callable[[AppConfig], ToolResolution] | None = None,
    clip_collector: ClipCollector | None = None,
    ffmpeg_runner: FfmpegRunner | None = None,
    audio_fingerprinter: AudioFingerprinter | None = None,
    output_callback: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Run the configured workflow and keep dry-run decisions in memory."""
    resolver = tool_resolver or resolve_tools
    collector = clip_collector or collect_clip
    tools = _resolve_pipeline_tools(config, resolver, argv, dry_run, output_callback)
    report = _start_pipeline_report(config, tools, argv, dry_run, output_callback)
    return _execute_started_pipeline(
        report,
        config,
        tools,
        dry_run,
        collector,
        ffmpeg_runner,
        audio_fingerprinter,
    )
