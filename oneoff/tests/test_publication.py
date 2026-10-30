from fractions import Fraction
from pathlib import Path

import pytest

from oneoff.contracts import (
    AudioFingerprint,
    Candidate,
    ClipAnalysis,
    CommandResult,
    Diagnosis,
    RepairArtifact,
    Verification,
)
from oneoff.publication import publish_repair, verify_repair


def _raw(*, source_path="/source/clip.mp4", frame_pts=None, b_frames=0, frame_count=3, audio_hash="same", tags=None):
    frame_pts = frame_pts if frame_pts is not None else ["0.000000", "0.040000", "0.080000"][:frame_count]
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": "1:1",
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "field_order": "progressive",
        "has_b_frames": b_frames,
        "time_base": "1/1000",
        "start_time": "0.000000",
        "duration": str(frame_count * 0.04),
    }
    audio = {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
        "sample_fmt": "fltp",
        "start_time": "0.000000",
        "tags": {"language": "eng"},
    }
    frames = [
        {"best_effort_timestamp_time": pts, "pict_type": "B" if b_frames and i == 1 else "P"}
        for i, pts in enumerate(frame_pts)
    ]
    return {
        "source": str(source_path),
        "status": "complete",
        "metadata": {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": tags or {"title": "sample"}},
            "streams": [video, audio],
            "chapters": [],
        },
        "decoder_frames": frames,
        "data_packets": None,
    }


def _fingerprint(path, payload="same"):
    return (
        AudioFingerprint(
            stream_index=1,
            properties={"codec": "aac", "sample_rate": 48000},
            packet_hashes=(payload,),
            packet_timestamps=((0, 0, 1024),),
        ),
    )


@pytest.fixture
def setup_files(tmp_path):
    source_path = tmp_path / "source" / "clip.mp4"
    target_path = tmp_path / "target" / "clip.mp4"
    part_path = target_path.with_name(target_path.name + ".part")
    source_path.parent.mkdir()
    target_path.parent.mkdir()
    source_path.write_bytes(b"original")
    part_path.write_bytes(b"encoded")
    candidate = Candidate(source_path, target_path)
    source = ClipAnalysis(candidate, _raw(source_path=source_path))
    diagnosis = Diagnosis("confirmed", "confirmed", {}, Fraction(25), 3, False)
    artifact = RepairArtifact(part_path, 3, CommandResult(("ffmpeg",), 0, ""))
    return candidate, source, diagnosis, artifact


def test_verify_passes_complete_output_and_audio_payload(setup_files):
    candidate, source, diagnosis, artifact = setup_files
    result = verify_repair(
        source, diagnosis, artifact, lambda path: _raw(source_path=path), _fingerprint
    )
    assert result.passed
    assert result.differences == ()
    assert result.accepted_terminal_discard is False
    assert result.output_raw["status"] == "complete"


def test_verify_rejects_failed_encode_and_missing_part(setup_files):
    candidate, source, diagnosis, artifact = setup_files
    failed = RepairArtifact(artifact.part_path, 3, CommandResult(("ffmpeg",), 1, "error"))
    artifact.part_path.unlink()
    result = verify_repair(source, diagnosis, failed, lambda path: _raw(source_path=path), _fingerprint)
    assert not result.passed
    assert any("encoding command" in item for item in result.differences)
    assert any("part file is missing" in item for item in result.differences)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"frame_pts": ["0", "0", "0.08"]}, "collide or move backwards"),
        ({"frame_pts": ["0", "0.05", "0.08"]}, "not constant"),
        ({"b_frames": 1}, "B-frames"),
        ({"audio_hash": "changed"}, "payload differs"),
        ({"tags": {"title": "changed"}}, "metadata tags differ"),
    ],
)
def test_verification_rejects_output_deviations(setup_files, kwargs, expected):
    candidate, source, diagnosis, artifact = setup_files
    output = _raw(
        source_path=artifact.part_path,
        **{key: value for key, value in kwargs.items() if key != "audio_hash"},
    )
    fingerprint = lambda path: _fingerprint(
        path, "same" if path == candidate.source_path else kwargs.get("audio_hash", "same")
    )
    result = verify_repair(source, diagnosis, artifact, lambda _: output, fingerprint)
    assert not result.passed
    assert any(expected in item for item in result.differences)


def test_verification_does_not_accept_more_or_fewer_frames(setup_files):
    candidate, source, diagnosis, artifact = setup_files
    artifact = RepairArtifact(artifact.part_path, 2, artifact.command_result)
    result = verify_repair(
        source, diagnosis, artifact, lambda path: _raw(source_path=path, frame_count=2), _fingerprint
    )
    assert not result.passed
    assert any("artifact frame count" in item for item in result.differences)
    assert any("output frame count" in item for item in result.differences)


def test_probe_exception_fails_closed(setup_files):
    candidate, source, diagnosis, artifact = setup_files

    def fail(_):
        raise OSError("probe unavailable")

    result = verify_repair(source, diagnosis, artifact, fail, _fingerprint)
    assert not result.passed
    assert any("output probe failed" in item for item in result.differences)


def test_publish_requires_verification_and_dry_run_does_not_publish(setup_files):
    candidate, _, _, artifact = setup_files
    failed = Verification(False, ("mismatch",), False, None)
    result = publish_repair(candidate, artifact, failed)
    assert not result.published
    assert not candidate.target_path.exists()
    assert artifact.part_path.exists()

    verified = Verification(
        True,
        (),
        False,
        _raw(source_path=artifact.part_path),
    )
    result = publish_repair(candidate, artifact, verified, dry_run=True)
    assert not result.published
    assert result.final_path is None
    assert not candidate.target_path.exists()
    assert artifact.part_path.exists()


def test_publish_atomically_moves_verified_part_and_preserves_source(setup_files):
    candidate, _, _, artifact = setup_files
    original = candidate.source_path.read_bytes()
    verified = Verification(True, (), False, _raw(source_path=artifact.part_path))
    result = publish_repair(candidate, artifact, verified)
    assert result.published
    assert result.final_path == candidate.target_path
    assert candidate.target_path.read_bytes() == b"encoded"
    assert not artifact.part_path.exists()
    assert candidate.source_path.read_bytes() == original


def test_publish_refuses_existing_target_and_keeps_part(setup_files):
    candidate, _, _, artifact = setup_files
    candidate.target_path.write_bytes(b"already here")
    verified = Verification(True, (), False, _raw(source_path=artifact.part_path))
    result = publish_repair(candidate, artifact, verified)
    assert not result.published
    assert candidate.target_path.read_bytes() == b"already here"
    assert artifact.part_path.exists()


def test_publish_refuses_target_that_appears_during_rename(setup_files):
    candidate, _, _, artifact = setup_files

    def race(part, target):
        target.write_bytes(b"concurrent")
        raise FileExistsError(target)

    verified = Verification(True, (), False, _raw(source_path=artifact.part_path))
    result = publish_repair(candidate, artifact, verified, race)
    assert not result.published
    assert candidate.target_path.read_bytes() == b"concurrent"
    assert artifact.part_path.exists()


def test_publish_reports_rename_failure_and_preserves_original_and_part(setup_files):
    candidate, _, _, artifact = setup_files
    original = candidate.source_path.read_bytes()

    def fail(part, target):
        raise OSError("share unavailable")

    verified = Verification(True, (), False, _raw(source_path=artifact.part_path))
    result = publish_repair(candidate, artifact, verified, fail)
    assert not result.published
    assert "share unavailable" in result.reason
    assert not candidate.target_path.exists()
    assert artifact.part_path.read_bytes() == b"encoded"
    assert candidate.source_path.read_bytes() == original


def test_publish_rejects_unverifiable_success_claim(setup_files):
    candidate, _, _, artifact = setup_files
    result = publish_repair(candidate, artifact, Verification(True, (), False, None))
    assert not result.published
    assert not candidate.target_path.exists()
    assert artifact.part_path.exists()


def test_publish_rejects_mismatched_source_filename(setup_files):
    candidate, _, _, artifact = setup_files
    wrong = Candidate(candidate.source_path, candidate.target_path.with_name("renamed.mp4"))
    result = publish_repair(
        wrong, artifact, Verification(True, (), False, _raw(source_path=artifact.part_path))
    )
    assert not result.published
    assert not wrong.target_path.exists()
    assert artifact.part_path.exists()


def test_data_stream_without_payload_evidence_fails_verification(setup_files):
    candidate, source, diagnosis, artifact = setup_files
    source_raw = dict(source.raw)
    source_meta = dict(source_raw["metadata"])
    source_meta["streams"] = list(source_meta["streams"]) + [
        {"index": 2, "codec_type": "data", "codec_name": "tmcd", "tags": {"timecode": "00:00:00:00"}}
    ]
    source_raw["metadata"] = source_meta
    source = ClipAnalysis(candidate, source_raw)
    output = _raw(source_path=artifact.part_path)
    output["metadata"]["streams"].append(
        {"index": 2, "codec_type": "data", "codec_name": "tmcd", "tags": {"timecode": "00:00:00:00"}}
    )
    result = verify_repair(source, diagnosis, artifact, lambda _: output, _fingerprint)
    assert not result.passed
    assert any("data-stream payload evidence is unavailable" in item for item in result.differences)


def test_terminal_discard_is_explicit_in_verification(setup_files):
    candidate, source, diagnosis, artifact = setup_files
    diagnosis = Diagnosis("confirmed", "accepted final discard", {}, Fraction(25), 3, True)
    result = verify_repair(
        source, diagnosis, artifact, lambda path: _raw(source_path=path), _fingerprint
    )
    assert result.passed
    assert result.accepted_terminal_discard
