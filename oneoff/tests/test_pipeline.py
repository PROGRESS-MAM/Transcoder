from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from oneoff import pipeline
from oneoff.contracts import (
    AppConfig,
    Candidate,
    CommandResult,
    Diagnosis,
    Verification,
)
from oneoff.tool_resolution import ToolResolution
from oneoff.run_report import get_events


TEST_NAMES = ("58238241.mp4", "58238527.mp4")


def _config(root: Path) -> AppConfig:
    exe = root / "app"
    source = root / "source"
    target = root / "target"
    reports = exe / "reports"
    for directory in (exe, source, target):
        directory.mkdir(parents=True, exist_ok=True)
    return AppConfig(exe, source, target, reports, repair_test=True)


def _tools() -> ToolResolution:
    return ToolResolution(Path("/mock/ffmpeg"), Path("/mock/ffprobe"), "mock ffmpeg", "mock ffprobe", ())


def _raw() -> dict[str, object]:
    return {
        "status": "complete",
        "metadata": {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "bit_rate": "400000",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                }
            ],
            "chapters": [],
        },
        "decoder_frames": [{"media_type": "video", "pict_type": "P", "pts": 0}],
        "packets": [],
    }


def _diagnosis(clip, kind: str = "confirmed") -> Diagnosis:
    return Diagnosis(
        kind=kind,
        reason=f"test {kind}",
        evidence={"test": True},
        frame_rate=Fraction(25, 1) if kind == "confirmed" else None,
        decoder_frame_count=1 if kind == "confirmed" else None,
        accepted_terminal_discard=False,
    )


def _install_mocks(monkeypatch, *, runner=None) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(pipeline, "resolve_tools", lambda config: _tools())
    monkeypatch.setattr(pipeline, "collect_clip", lambda path: _raw())
    monkeypatch.setattr(
        pipeline,
        "classify",
        lambda clip: _diagnosis(clip, "confirmed" if clip.candidate.source_path.name == TEST_NAMES[0] else "clean"),
    )

    def default_runner(argv, progress_callback=None):
        commands.append(argv)
        Path(argv[-1]).write_bytes(b"mock repair")
        return CommandResult(argv, 0, "")

    monkeypatch.setattr(
        pipeline,
        "verify_repair",
        lambda clip, diagnosis, artifact, **kwargs: Verification(
            True,
            (),
            False,
            {
                "source": str(artifact.part_path),
                "status": "complete",
                "decoder_frames": [{}],
            },
        ),
    )
    if runner is None:
        runner = default_runner
    return commands, runner


def _write_sources(config: AppConfig) -> dict[Path, bytes]:
    snapshots = {}
    for name in TEST_NAMES:
        path = config.source_dir / name
        content = f"original:{name}".encode()
        path.write_bytes(content)
        snapshots[path] = content
    return snapshots


def _tree_snapshot(root: Path) -> dict[Path, tuple[str, bytes | None]]:
    return {
        path.relative_to(root): ("directory", None)
        if path.is_dir()
        else ("file", path.read_bytes())
        for path in root.rglob("*")
    }


def test_dry_run_leaves_target_tree_unchanged_and_records_decisions_in_memory(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_sources(config)
    before = _tree_snapshot(tmp_path)
    commands, runner = _install_mocks(monkeypatch)
    progress = []

    result = pipeline.run_pipeline(
        config,
        dry_run=True,
        argv=("--dry-run",),
        ffmpeg_runner=runner,
        audio_fingerprinter=lambda path: (),
        output_callback=progress.append,
    )

    assert commands == []
    assert _tree_snapshot(tmp_path) == before
    assert not result.report.run_dir.exists()
    events = get_events(result.report)
    assert any(event["stage"] == "diagnosis" and event["status"] == "confirmed" for event in events)
    assert any(event["stage"] == "repair" and event["status"] == "dry_run" for event in events)
    assert any("repair/dry_run" in message for message in progress)
    assert result.summary.counts == {"dry_run": 1, "clean": 1}


def test_real_run_publishes_repair_without_overwriting_source(tmp_path, monkeypatch):
    config = _config(tmp_path)
    source_files = _write_sources(config)
    commands, runner = _install_mocks(monkeypatch)

    result = pipeline.run_pipeline(
        config,
        ffmpeg_runner=runner,
        audio_fingerprinter=lambda path: (),
    )

    assert len(commands) == 1
    assert commands[0][0] == "/mock/ffmpeg"
    assert all(path.read_bytes() == content for path, content in source_files.items())
    assert (config.target_dir / TEST_NAMES[0]).read_bytes() == b"mock repair"
    assert not (config.target_dir / TEST_NAMES[1]).exists()
    assert result.summary.counts == {"published": 1, "clean": 1}


def test_repair_error_is_recorded_without_changing_source_or_target(tmp_path, monkeypatch):
    config = _config(tmp_path)
    source_files = _write_sources(config)
    _commands, runner = _install_mocks(monkeypatch)

    def failing_runner(argv, progress_callback=None):
        return CommandResult(argv, 1, "mock failure")

    result = pipeline.run_pipeline(
        config,
        dry_run=False,
        ffmpeg_runner=failing_runner,
        audio_fingerprinter=lambda path: (),
    )

    assert all(path.read_bytes() == content for path, content in source_files.items())
    assert not list(config.target_dir.iterdir())
    outcomes = [
        json.loads(path.read_text(encoding="utf-8"))["outcome"]
        for path in sorted((result.report.run_dir / "clips").glob("*.json"))
    ]
    assert [item["status"] for item in outcomes] == ["repair_error", "clean"]
    assert "FFmpeg repair failed" in outcomes[0]["reason"]


def test_tool_resolution_failure_finishes_report_before_aborting(tmp_path):
    config = _config(tmp_path)
    failed = ToolResolution(None, None, None, None, ("ffmpeg missing", "ffprobe missing"))

    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run_pipeline(config, tool_resolver=lambda config: failed)

    assert error.value.report is not None
    report = error.value.report
    assert report.markdown_path.is_file()
    text = report.markdown_path.read_text(encoding="utf-8")
    assert "ffmpeg missing" in text
    assert not (report.run_dir / "clips").exists()


def test_tool_resolution_exception_is_recorded(tmp_path):
    config = _config(tmp_path)

    def fail_resolution(config):
        raise OSError("mock tool check failure")

    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run_pipeline(config, tool_resolver=fail_resolution)

    assert error.value.report is not None
    report = error.value.report
    assert report.markdown_path.is_file()
    assert "mock tool check failure" in report.markdown_path.read_text(encoding="utf-8")
