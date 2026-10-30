from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from oneoff.contracts import (
    AppConfig,
    Candidate,
    ClipOutcome,
    ReportEvent,
    RunReport,
    RunSummary,
)


_EVENT_FILE = "events.jsonl"
_METADATA_FILE = "run.json"
_CLIPS_DIR = "clips"
_STATUS_ORDER = (
    "clean",
    "unclear",
    "unsupported",
    "analysis_error",
    "repair_error",
    "verification_error",
    "published",
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Cannot serialize {type(value).__name__} to JSON")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _assert_report_paths(report: RunReport) -> None:
    run_dir = report.run_dir.resolve()
    if report.log_path.resolve().parent != run_dir:
        raise ValueError("The log file must be inside the run report directory")
    if report.markdown_path.resolve().parent != run_dir:
        raise ValueError("The Markdown report must be inside the run report directory")


def _append_log(report: RunReport, message: str) -> None:
    with report.log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(message.replace("\r", " ").replace("\n", " ") + "\n")


def _append_event(report: RunReport, record: dict[str, Any]) -> None:
    event_path = report.run_dir / _EVENT_FILE
    with event_path.open("a", encoding="utf-8") as event_file:
        event_file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")



@dataclass
class _ReportState:
    dry_run: bool
    output_callback: Callable[[str], None]
    events: list[dict[str, Any]]


_REPORT_STATES: dict[Path, _ReportState] = {}


def _state_key(report: RunReport) -> Path:
    return report.run_dir.resolve()


def _report_state(report: RunReport) -> _ReportState | None:
    return _REPORT_STATES.get(_state_key(report))


def _write_progress(message: str) -> None:
    print(message, file=sys.stderr)


def _emit_progress(report: RunReport, record: dict[str, Any]) -> None:
    source_path = record.get("source_path")
    source = f" | {source_path}" if source_path is not None else ""
    message = f"[{record['timestamp']}] {record['stage']}/{record['status']}{source} | {record['message']}"
    state = _report_state(report)
    callback = state.output_callback if state else _write_progress
    callback(message)


def _record_event_record(report: RunReport, record: dict[str, Any]) -> None:
    state = _report_state(report)
    if state and state.dry_run:
        state.events.append(record)
    else:
        _append_event(report, record)
        source = f" | {record['source_path']}" if record.get("source_path") is not None else ""
        _append_log(
            report,
            f"[{record['timestamp']}] {record['stage']}/{record['status']}{source} | {record['message']}",
        )
    _emit_progress(report, record)


def get_events(report: RunReport) -> list[dict[str, Any]]:
    """Return the recorded events, including in-memory dry-run events."""
    state = _report_state(report)
    if state and state.dry_run:
        return json.loads(json.dumps(state.events, ensure_ascii=False))
    return _read_events(report)

def _allocate_run_directory(reports_dir: Path, run_name: str, dry_run: bool) -> Path:
    for suffix in range(10_000):
        folder_name = run_name if suffix == 0 else f"{run_name}-{suffix:03d}"
        run_dir = reports_dir / folder_name
        if dry_run:
            if run_dir.exists() or run_dir.resolve() in _REPORT_STATES:
                continue
            return run_dir
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            continue
    raise FileExistsError("Could not allocate a unique run report directory")


def _create_run_report(run_dir: Path) -> RunReport:
    return RunReport(
        run_dir=run_dir,
        log_path=run_dir / "run.log",
        markdown_path=run_dir / "report.md",
    )


def _run_metadata(
    config: AppConfig,
    tool_versions: dict[str, str],
    argv: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "started_at": _timestamp(),
        "config": config,
        "tool_versions": tool_versions,
        "argv": argv,
    }


def _initialize_run_report(
    report: RunReport,
    metadata: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    report.log_path.touch()
    (report.run_dir / _EVENT_FILE).touch()
    _write_json(report.run_dir / _METADATA_FILE, metadata)
    report.markdown_path.write_text(
        "# One-Off-Proxyreparatur – Laufbericht\n\nStatus: läuft.\n",
        encoding="utf-8",
    )


def _started_run_event(
    metadata: dict[str, Any],
    tool_versions: dict[str, str],
    argv: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "timestamp": metadata["started_at"],
        "stage": "run",
        "status": "started",
        "source_path": None,
        "message": "Lauf gestartet",
        "details": {"tool_versions": tool_versions, "argv": list(argv)},
    }


def start_run(
    config: AppConfig,
    tool_versions: dict[str, str],
    argv: tuple[str, ...],
    *,
    dry_run: bool | None = None,
    output_callback: Callable[[str], None] | None = None,
) -> RunReport:
    if dry_run is None:
        dry_run = "--dry-run" in argv
    reports_dir = config.reports_dir
    if not dry_run:
        reports_dir.mkdir(parents=True, exist_ok=True)
    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = _allocate_run_directory(reports_dir, run_name, dry_run)
    report = _create_run_report(run_dir)
    callback = output_callback or _write_progress
    _REPORT_STATES[_state_key(report)] = _ReportState(
        dry_run=dry_run,
        output_callback=callback,
        events=[],
    )
    metadata = _run_metadata(config, tool_versions, argv)
    _initialize_run_report(report, metadata, dry_run)
    _record_event_record(
        report,
        _started_run_event(metadata, tool_versions, argv),
    )
    return report

def record_event(report: RunReport, event: ReportEvent) -> None:
    _assert_report_paths(report)
    record = {
        "timestamp": _timestamp(),
        **_jsonable(event),
    }
    _record_event_record(report, record)


def _verified_outcome(outcome: ClipOutcome) -> ClipOutcome:
    if outcome.status == "published" and not (outcome.verification and outcome.verification.passed):
        return replace(
            outcome,
            status="verification_error",
            reason="Output verification did not pass; the output is not reported as published.",
        )
    return outcome


def record_clip(report: RunReport, outcome: ClipOutcome) -> None:
    _assert_report_paths(report)
    outcome = _verified_outcome(outcome)
    state = _report_state(report)
    clip_path = None
    if not (state and state.dry_run):
        clips_dir = report.run_dir / _CLIPS_DIR
        clips_dir.mkdir(exist_ok=True)
        source_name = outcome.candidate.source_path.name or "clip"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", source_name).strip(".-")[:80] or "clip"
        index = 1
        while True:
            candidate_path = clips_dir / f"{index:06d}-{slug}.json"
            try:
                with candidate_path.open("x", encoding="utf-8") as clip_file:
                    json.dump(
                        {"recorded_at": _timestamp(), "outcome": _jsonable(outcome)},
                        clip_file,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    clip_file.write("\n")
                clip_path = candidate_path
                break
            except FileExistsError:
                index += 1

    reason = outcome.reason or outcome.status
    details = {"target_path": str(outcome.candidate.target_path)}
    if clip_path is not None:
        details["clip_record"] = str(clip_path.relative_to(report.run_dir))
    record_event(
        report,
        ReportEvent(
            stage="clip",
            status=outcome.status,
            source_path=outcome.candidate.source_path,
            message=reason,
            details=details,
        ),
    )


def process_candidates(
    report: RunReport,
    candidates: tuple[Candidate, ...],
    process_one: Callable[[Candidate], ClipOutcome],
) -> RunSummary:
    _assert_report_paths(report)
    record_event(
        report,
        ReportEvent(
            stage="selection",
            status="selected",
            source_path=None,
            message=f"{len(candidates)} Kandidat(en) ausgewählt",
            details={"candidate_count": len(candidates)},
        ),
    )

    counts: Counter[str] = Counter()
    for candidate in candidates:
        try:
            outcome = process_one(candidate)
        except Exception as error:
            outcome = ClipOutcome(
                candidate=candidate,
                clip=None,
                diagnosis=None,
                artifact=None,
                verification=None,
                status="analysis_error",
                reason=f"{type(error).__name__}: {error}",
            )
        outcome = _verified_outcome(outcome)
        record_clip(report, outcome)
        counts[outcome.status] += 1
    return RunSummary(counts=dict(counts))


def _read_events(report: RunReport) -> list[dict[str, Any]]:
    event_path = report.run_dir / _EVENT_FILE
    if not event_path.exists():
        return []
    events = []
    for line in event_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _read_clip_records(report: RunReport) -> list[tuple[Path, dict[str, Any]]]:
    clips_dir = report.run_dir / _CLIPS_DIR
    if not clips_dir.exists():
        return []
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(clips_dir.glob("*.json"))
    ]


def _reported_clip_status(outcome: dict[str, Any]) -> str:
    verification = outcome.get("verification")
    if outcome.get("status") == "published" and not (
        verification and verification.get("passed") is True
    ):
        return "verification_error"
    return str(outcome.get("status", "unknown"))


def _effective_summary(summary: RunSummary, clip_records: list[tuple[Path, dict[str, Any]]]) -> RunSummary:
    if clip_records:
        counts = Counter(_reported_clip_status(record.get("outcome", {})) for _, record in clip_records)
        return RunSummary(counts=dict(counts))
    counts = dict(summary.counts)
    counts["published"] = 0
    return RunSummary(counts=counts)


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _event_target_exists(event: dict[str, Any]) -> bool:
    stage = str(event.get("stage", "")).casefold()
    status = str(event.get("status", "")).casefold().replace("-", "_").replace(" ", "_")
    return status in {"existing", "already_exists", "already_present", "target_exists"} or (
        stage in {"selection", "inventory"} and status in {"skipped_existing", "target_present"}
    )


def _report_metadata(report: RunReport) -> dict[str, Any]:
    metadata_path = report.run_dir / _METADATA_FILE
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _render_clip_sections(
    report: RunReport, clip_records: list[tuple[Path, dict[str, Any]]]
) -> tuple[list[str], int, int]:
    sections = []
    passed_checks = 0
    verified_publications = 0
    for clip_path, data in clip_records:
        outcome = data.get("outcome", {})
        verification = outcome.get("verification")
        is_verified = bool(verification and verification.get("passed") is True)
        if is_verified:
            passed_checks += 1
        status = _reported_clip_status(outcome)
        if status == "published" and is_verified:
            verified_publications += 1
        source = outcome.get("candidate", {}).get("source_path", "")
        reason = outcome.get("reason") or "—"
        diagnosis = outcome.get("diagnosis") or {}
        diagnosis_text = diagnosis.get("kind", "—")
        sections.append(
            f"### {_markdown_cell(source)}\n\n"
            f"- Status: `{_markdown_cell(status)}`\n"
            f"- Befund: `{_markdown_cell(diagnosis_text)}`\n"
            f"- Entscheidung/Meldung: {_markdown_cell(reason)}\n"
            f"- Rohdaten und vollständige Ausgabeprüfung: `{_markdown_cell(clip_path.relative_to(report.run_dir))}`\n"

        )
    return sections, passed_checks, verified_publications


def _render_header(report: RunReport, metadata: dict[str, Any]) -> list[str]:
    return [
        "# One-Off-Proxyreparatur – Laufbericht",
        "",
        f"- Laufordner: `{report.run_dir.name}`",
        f"- Beginn (UTC): `{metadata.get('started_at', 'unbekannt')}`",
        f"- Konfiguration: `{json.dumps(metadata.get('config', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Werkzeugversionen: `{json.dumps(metadata.get('tool_versions', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Aufruf: `{json.dumps(metadata.get('argv', []), ensure_ascii=False)}`",
        "",
    ]


def _render_summary(
    summary: RunSummary, existing_targets: int, passed_checks: int, verified_publications: int
) -> list[str]:
    counts = summary.counts
    total = sum(counts.values())
    lines = [
        "## Übersicht",
        "",
        "| Kennzahl | Anzahl |",
        "|---|---:|",
        f"| Ausgewählte Kandidaten | {total} |",
        f"| Bereits vorhandene Ziele | {existing_targets} |",
        f"| Unauffällige Clips | {counts.get('clean', 0)} |",
        f"| Unklare Befunde | {counts.get('unclear', 0)} |",
        f"| Analysefehler | {counts.get('analysis_error', 0)} |",
        f"| Reparaturfehler | {counts.get('repair_error', 0)} |",
        f"| Ausgabeprüfungen bestanden | {passed_checks} |",
        f"| Erfolgreich veröffentlichte Ausgaben | {verified_publications} |",
        "",
        "## Statuszählung",
        "",
        "| Status | Anzahl |",
        "|---|---:|",
    ]
    for status in _STATUS_ORDER:
        count = verified_publications if status == "published" else counts.get(status, 0)
        lines.append(f"| `{status}` | {count} |")
    for status, count in sorted(counts.items()):
        if status not in _STATUS_ORDER:
            lines.append(f"| `{_markdown_cell(status)}` | {count} |")
    return lines


def _render_events(events: list[dict[str, Any]]) -> list[str]:
    lines = ["", "## Ereignisse", ""]
    if not events:
        lines.append("Keine Ereignisse protokolliert.")
        return lines
    lines.extend(["| Zeit (UTC) | Stufe | Status | Quelle | Meldung |", "|---|---|---|---|---|"])
    for event in events:
        lines.append(
            "| " + " | ".join(
                _markdown_cell(event.get(key, "—"))
                for key in ("timestamp", "stage", "status", "source_path", "message")
            ) + " |"
        )
    return lines


def _render_clip_results(clip_sections: list[str]) -> list[str]:
    return ["", "## Clip-Ergebnisse", "", *(clip_sections or ["Keine Clips verarbeitet."])]


def _render_acceptance_steps() -> list[str]:
    return [
        "## Abnahme und nächste Freigabe",
        "",
        "Vor der Freigabe sind die beiden Fehlerreferenzen als bekanntes Fehlerbild zu bestätigen und "
        "ein Vergleich ohne B-Frames darf nicht als diese Fehlerklasse gelten. Im Reparaturtest sind "
        "höchstens die zwei festgelegten Namen zu verarbeiten und Zielkonflikte zu respektieren.",
        "",
        "Der Serienmodus darf erst nach erfolgreichen Probe-Reparaturen beider Fehlerclips starten. "
        "Alle verbleibenden Clips sind zu analysieren; repariert werden ausschließlich bestätigte Fälle "
        "des bekannten Fehlerbilds. Nach ausdrücklicher Freigabe ist zuerst die praktische README zu "
        "erstellen und freigeben zu lassen. Tatsächliche Probe-Reparaturen sowie Player-/SMB-Abnahmen "
        "bleiben separat durchzuführen.",
        "",
    ]


def _render_markdown(report: RunReport, summary: RunSummary, events: list[dict[str, Any]]) -> str:
    metadata = _report_metadata(report)
    clip_records = _read_clip_records(report)
    clip_sections, passed_checks, verified_publications = _render_clip_sections(report, clip_records)
    existing_targets = sum(_event_target_exists(event) for event in events)
    lines = _render_header(report, metadata)
    lines.extend(_render_summary(summary, existing_targets, passed_checks, verified_publications))
    lines.extend(_render_events(events))
    lines.extend(_render_clip_results(clip_sections))
    lines.extend(_render_acceptance_steps())
    return "\n".join(lines)


def finish_run(report: RunReport, summary: RunSummary) -> Path:
    _assert_report_paths(report)
    state = _report_state(report)
    clip_records = [] if state and state.dry_run else _read_clip_records(report)
    effective_summary = _effective_summary(summary, clip_records)
    record_event(
        report,
        ReportEvent(
            stage="run",
            status="finished",
            source_path=None,
            message="Lauf abgeschlossen",
            details={"counts": effective_summary.counts},
        ),
    )
    if not (state and state.dry_run):
        content = _render_markdown(report, effective_summary, get_events(report))
        report.markdown_path.write_text(content, encoding="utf-8")
        _append_log(report, f"[{_timestamp()}] Laufbericht abgeschlossen")
    return report.markdown_path
