from pathlib import Path

import pytest

from oneoff.analysis.probe import collect_clip
from oneoff.contracts import Candidate, ClipAnalysis
from oneoff.diagnosis import classify


REFERENCE_CASES = (
    ("58238241.mp4", "confirmed", True),
    ("58238527.mp4", "confirmed", True),
    ("53287289.mp4", "clean", False),
)


def _find_clip(filename: str) -> Path | None:
    repo_root = Path(__file__).resolve().parents[2]
    for path in repo_root.rglob(filename):
        if ".git" not in path.parts:
            return path
    return None


def _reference_raw(filename: str, affected: bool) -> dict:
    if affected:
        output_pts = (0, 200, 100, 300, 300)
        output_types = ("I", "B", "B", "B", "P")
        output_packet_indices = (0, 4, 2, 3, 1)
        packet_pts = (0, 300, 100, 300, 200)
    else:
        output_pts = (0, 100, 200, 300, 400)
        output_types = ("I", "P", "P", "P", "P")
        output_packet_indices = (0, 1, 2, 3, 4)
        packet_pts = (0, 100, 200, 300, 400)

    dts = (0, 100, 200, 300, 400)
    packets = [
        {"stream_index": 0, "pos": i * 10, "dts": tick, "pts": pts}
        for i, (tick, pts) in enumerate(zip(dts, packet_pts))
    ]
    frames = []
    for output_index, packet_index in enumerate(output_packet_indices):
        packet = packets[packet_index]
        frames.append(
            {
                "stream_index": 0,
                "pict_type": output_types[output_index],
                "pts": output_pts[output_index],
                "pkt_pos": packet["pos"],
                "pkt_dts": packet["dts"],
            }
        )

    return {
        "schema_version": 1,
        "status": "complete",
        "metadata": {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "avg_frame_rate": "24/1",
                    "r_frame_rate": "24/1",
                    "time_base": "1/2400",
                }
            ]
        },
        "packets": packets,
        "decoder_frames": frames,
        "diagnostics": [],
        "source": filename,
    }


@pytest.mark.parametrize(
    ("filename", "expected_kind", "affected"), REFERENCE_CASES,
    ids=[case[0] for case in REFERENCE_CASES],
)
def test_reference_clip_probe_and_classification(filename, expected_kind, affected):
    clip_path = _find_clip(filename)
    if clip_path is None:
        pytest.skip(f"{filename} is not present in the repository checkout; media clip unavailable")

    raw = collect_clip(clip_path)
    candidate = Candidate(source_path=clip_path, target_path=clip_path)
    result = classify(ClipAnalysis(candidate=candidate, raw=raw))

    assert result.kind == expected_kind


@pytest.fixture(params=REFERENCE_CASES, ids=[case[0] for case in REFERENCE_CASES])
def reference_clip_analysis(request):
    filename, expected_kind, affected = request.param
    candidate = Candidate(
        source_path=Path(filename),
        target_path=Path("fixed") / filename,
    )
    raw = _reference_raw(filename, affected)
    return ClipAnalysis(candidate=candidate, raw=raw), expected_kind


def test_spec_derived_reference_fixture_classification(reference_clip_analysis):
    clip, expected_kind = reference_clip_analysis

    result = classify(clip)

    assert result.kind == expected_kind
