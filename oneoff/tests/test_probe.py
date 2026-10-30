from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from oneoff.analysis import collect_clip
from oneoff.analysis import probe


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"fixture")
    return path


def _completed(stdout: str = "{}", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_collects_metadata_packets_and_decoded_frames(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: "/tools/ffprobe")
    responses = [
        _completed("ffprobe version 7.0\n"),
        _completed(json.dumps({"format": {"duration": "1"}, "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}], "chapters": []})),
        _completed(json.dumps({"packets": [{"pts": 10, "dts": 9, "pos": "100", "flags": "K_"}]})),
        _completed(json.dumps({"frames": [{"pts": 10, "best_effort_timestamp": 11, "pict_type": "I"}]})),
    ]
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return responses.pop(0)

    monkeypatch.setattr(probe.subprocess, "run", run)
    raw = collect_clip(clip)

    assert raw["status"] == "complete"
    assert raw["metadata"]["streams"][0]["r_frame_rate"] == "25/1"
    assert raw["packets"][0]["pts"] == 10
    assert raw["decoder_frames"][0]["best_effort_timestamp"] == 11
    assert raw["tool_version"] == "ffprobe version 7.0"
    assert len(calls) == 4
    json.dumps(raw)


def test_missing_ffprobe_returns_clear_error(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: None)

    raw = collect_clip(clip)

    assert raw["status"] == "error"
    assert raw["diagnostics"][0]["stage"] == "resolve"
    assert "not found" in raw["diagnostics"][0]["message"]


def test_invalid_file_reported_without_raising(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: "/tools/ffprobe")
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: _completed("", 1, "Invalid data found when processing input"))

    raw = collect_clip(clip)

    assert raw["status"] == "error"
    assert raw["diagnostics"][-1]["stage"] == "metadata"
    assert "Invalid data" in raw["diagnostics"][-1]["stderr"]


def test_failed_optional_scan_returns_partial_result(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: "/tools/ffprobe")
    responses = [
        _completed("ffprobe version 7.0\n"),
        _completed(json.dumps({"format": {}, "streams": [], "chapters": []})),
        _completed("not json"),
        _completed(json.dumps({"frames": []})),
    ]
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: responses.pop(0))

    raw = collect_clip(clip)

    assert raw["status"] == "partial"
    assert raw["packets"] == []
    assert raw["decoder_frames"] == []
    assert raw["diagnostics"][-1]["stage"] == "packets"



def test_missing_metadata_sections_are_reported_as_error(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: "/tools/ffprobe")
    responses = [_completed("ffprobe version 7.0\n"), _completed(json.dumps({}))]
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: responses.pop(0))

    raw = collect_clip(clip)

    assert raw["status"] == "error"
    assert raw["diagnostics"][-1]["stage"] == "metadata"
    assert "missing required sections" in raw["diagnostics"][-1]["message"]


def test_missing_packet_or_frame_sections_are_partial(monkeypatch: pytest.MonkeyPatch, clip: Path) -> None:
    monkeypatch.setattr(probe, "_resolve_ffprobe", lambda: "/tools/ffprobe")
    responses = [
        _completed("ffprobe version 7.0\n"),
        _completed(json.dumps({"format": {}, "streams": [], "chapters": []})),
        _completed(json.dumps({})),
        _completed(json.dumps({})),
    ]
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs: responses.pop(0))

    raw = collect_clip(clip)

    assert raw["status"] == "partial"
    assert any("required packets section" in item["message"] for item in raw["diagnostics"])
    assert any("required frames section" in item["message"] for item in raw["diagnostics"])

def test_inventory_wraps_raw_data_in_clip_analysis(clip: Path) -> None:
    from oneoff.contracts import Candidate, ClipAnalysis
    from oneoff.inventory import collect_candidate

    candidate = Candidate(source_path=clip, target_path=clip.with_name("output.mp4"))
    result = collect_candidate(candidate, collector=lambda path: {"status": "error", "source": str(path)})

    assert isinstance(result, ClipAnalysis)
    assert result.candidate == candidate
    assert result.raw["status"] == "error"
