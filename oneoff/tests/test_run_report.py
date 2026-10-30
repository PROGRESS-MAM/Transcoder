import json
from pathlib import Path

from oneoff.contracts import (
    AppConfig,
    Candidate,
    ClipOutcome,
    ClipAnalysis,
    Diagnosis,
    ReportEvent,
    RunSummary,
    Verification,
)
from oneoff.run_report import finish_run, get_events, process_candidates, record_clip, record_event, start_run


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        exe_dir=tmp_path / "app",
        source_dir=tmp_path / "source",
        target_dir=tmp_path / "target",
        reports_dir=tmp_path / "reports",
        repair_test=True,
    )


def make_candidate(tmp_path: Path, name: str) -> Candidate:
    return Candidate(tmp_path / "source" / name, tmp_path / "target" / name)


def make_outcome(candidate: Candidate, status: str = "clean", *, passed: bool | None = None) -> ClipOutcome:
    clip = ClipAnalysis(candidate=candidate, raw={"format": {"duration": "1.0"}, "frames": [{"pts": 1}]})
    diagnosis = Diagnosis(
        kind="clean" if status == "clean" else "confirmed",
        reason="fixture finding",
        evidence={"frame": 1},
        frame_rate=None,
        decoder_frame_count=1,
        accepted_terminal_discard=False,
    )
    verification = None if passed is None else Verification(
        passed=passed,
        differences=(),
        accepted_terminal_discard=False,
        output_raw={"streams": []},
    )
    return ClipOutcome(
        candidate=candidate,
        clip=clip,
        diagnosis=diagnosis,
        artifact=None,
        verification=verification,
        status=status,
        reason="fixture result",
    )


def test_start_run_creates_unique_persistent_run_directories(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = start_run(config, {"ffmpeg": "7.0"}, ("repair.py",))
    first_metadata = json.loads((first.run_dir / "run.json").read_text(encoding="utf-8"))
    second = start_run(config, {}, ())

    assert first.run_dir.parent == config.reports_dir
    assert first.run_dir != second.run_dir
    assert first_metadata["config"]["repair_test"] is True
    assert first_metadata["tool_versions"] == {"ffmpeg": "7.0"}
    assert first_metadata["argv"] == ["repair.py"]
    assert first.log_path.exists()
    assert first.markdown_path.exists()
    assert first.run_dir.exists()


def test_dry_run_keeps_events_in_memory_without_creating_files(tmp_path: Path) -> None:
    report = start_run(make_config(tmp_path), {}, ("repair.py", "--dry-run"))
    record_clip(report, make_outcome(make_candidate(tmp_path, "clip.mov")))
    finish_run(report, RunSummary(counts={"clean": 1}))

    events = get_events(report)
    assert [event["status"] for event in events] == ["started", "clean", "finished"]
    assert list(tmp_path.iterdir()) == []


def test_progress_is_sent_to_injected_output_callback(tmp_path: Path) -> None:
    progress: list[str] = []
    report = start_run(make_config(tmp_path), {}, (), output_callback=progress.append)
    record_event(
        report,
        ReportEvent(
            stage="inventory",
            status="already_exists",
            source_path=None,
            message="Target already exists",
            details={},
        ),
    )

    assert any("run/started" in line for line in progress)
    assert any("inventory/already_exists" in line and "Target already exists" in line for line in progress)


def test_record_event_is_structured_and_does_not_print(tmp_path: Path, capsys) -> None:
    report = start_run(make_config(tmp_path), {}, ())
    record_event(
        report,
        ReportEvent(
            stage="inventory",
            status="already_exists",
            source_path=tmp_path / "source" / "clip.mov",
            message="Target already exists",
            details={"target": tmp_path / "target" / "clip.mov", "attempt": 2},
        ),
    )

    event_lines = (report.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(event_lines[-1])
    assert event["source_path"].endswith("clip.mov")
    assert event["details"]["target"].endswith("clip.mov")
    assert "already_exists" in report.log_path.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""
    saved_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert saved_files
    assert all(configured_path == tmp_path / "reports" or (tmp_path / "reports") in configured_path.parents
               for configured_path in saved_files)


def test_record_clip_saves_full_analysis_decision_and_verification(tmp_path: Path) -> None:
    report = start_run(make_config(tmp_path), {}, ())
    candidate = make_candidate(tmp_path, "clip.mov")
    outcome = make_outcome(candidate, "published", passed=True)
    record_clip(report, outcome)

    clip_file = next((report.run_dir / "clips").glob("*.json"))
    saved = json.loads(clip_file.read_text(encoding="utf-8"))["outcome"]
    assert saved["clip"]["raw"]["frames"] == [{"pts": 1}]
    assert saved["diagnosis"]["evidence"] == {"frame": 1}
    assert saved["verification"]["passed"] is True
    assert saved["candidate"]["target_path"].endswith("clip.mov")
    assert not (tmp_path / "source" / "clip.mov").exists()


def test_process_candidates_logs_errors_and_continues(tmp_path: Path) -> None:
    report = start_run(make_config(tmp_path), {}, ())
    candidates = (make_candidate(tmp_path, "bad.mov"), make_candidate(tmp_path, "good.mov"))
    called = []

    def process_one(candidate: Candidate) -> ClipOutcome:
        called.append(candidate)
        if candidate == candidates[0]:
            raise RuntimeError("probe failed")
        return make_outcome(candidate, "published", passed=True)

    summary = process_candidates(report, candidates, process_one)

    assert called == list(candidates)
    assert summary.counts == {"analysis_error": 1, "published": 1}
    saved = [json.loads(path.read_text(encoding="utf-8"))["outcome"] for path in (report.run_dir / "clips").glob("*.json")]
    assert {item["status"] for item in saved} == {"analysis_error", "published"}
    assert any("probe failed" in item["reason"] for item in saved)


def test_finish_run_reports_statuses_targets_and_verified_outputs(tmp_path: Path) -> None:
    report = start_run(make_config(tmp_path), {"ffprobe": "7.0"}, ("run",))
    record_event(
        report,
        ReportEvent(
            stage="inventory",
            status="already_exists",
            source_path=tmp_path / "source" / "old.mov",
            message="Target already exists",
            details={},
        ),
    )
    statuses = [
        "clean",
        "unclear",
        "unclear",
        "analysis_error",
        "analysis_error",
        "analysis_error",
        "repair_error",
        "repair_error",
        "repair_error",
        "repair_error",
        "verification_error",
        "published",
    ]
    candidates = tuple(make_candidate(tmp_path, f"clip-{index}.mov") for index in range(len(statuses)))
    next_status = iter(statuses)

    def process_one(candidate: Candidate) -> ClipOutcome:
        status = next(next_status)
        return make_outcome(candidate, status, passed=True if status == "published" else None)

    summary = process_candidates(report, candidates, process_one)
    assert sum(summary.counts.values()) == len(candidates)

    path = finish_run(report, summary)
    markdown = path.read_text(encoding="utf-8")

    assert path == report.markdown_path
    assert "Ausgewählte Kandidaten | 12" in markdown
    assert "Bereits vorhandene Ziele | 1" in markdown
    assert "Unklare Befunde | 2" in markdown
    assert "Analysefehler | 3" in markdown
    assert "Reparaturfehler | 4" in markdown
    assert "Ausgabeprüfungen bestanden | 1" in markdown
    assert "ffprobe" in markdown
    assert "nächste Freigabe" in markdown
    assert "Probe-Reparaturen" in markdown
    assert "Player-/SMB-Abnahmen" in markdown
    assert "ausdrücklicher Freigabe" in markdown
    assert "praktische README" in markdown
    assert "freigeben zu lassen" in markdown


def test_unverified_output_is_never_reported_as_published(tmp_path: Path) -> None:
    report = start_run(make_config(tmp_path), {}, ())
    candidate = make_candidate(tmp_path, "unverified.mov")

    summary = process_candidates(
        report,
        (candidate,),
        lambda selected: make_outcome(selected, "published"),
    )
    markdown = finish_run(report, RunSummary(counts={"published": 1})).read_text(encoding="utf-8")
    saved = json.loads(next((report.run_dir / "clips").glob("*.json")).read_text(encoding="utf-8"))

    assert summary.counts == {"verification_error": 1}
    assert saved["outcome"]["status"] == "verification_error"
    assert "Erfolgreich veröffentlichte Ausgaben | 0" in markdown
    assert "`verification_error` | 1" in markdown
    assert "`published` | 0" in markdown
