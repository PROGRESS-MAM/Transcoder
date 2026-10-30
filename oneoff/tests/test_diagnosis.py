from fractions import Fraction
from pathlib import Path

from oneoff.contracts import Candidate, ClipAnalysis, Diagnosis
from oneoff.diagnosis import classify


def _clip(
    output_pts=(0, 200, 100, 300, 300),
    *,
    output_types=("I", "B", "B", "B", "P"),
    output_packet_indices=(0, 4, 2, 3, 1),
    dts=(0, 100, 200, 300, 400),
    avg_rate="24/1",
    r_rate="24/1",
    time_base="1/2400",
    status="complete",
    packet_pts=(0, 300, 100, 300, 200),
    include_anchors=True,
    streams=None,
):
    packets = [
        {"stream_index": 0, "pos": i * 10, "dts": tick, "pts": pts}
        for i, (tick, pts) in enumerate(zip(dts, packet_pts))
    ]
    frames = []
    for output_index, packet_index in enumerate(output_packet_indices):
        packet = packets[packet_index]
        frame = {
            "stream_index": 0,
            "pict_type": output_types[output_index],
            "pts": output_pts[output_index],
        }
        if include_anchors:
            frame["pkt_pos"] = packet["pos"]
            frame["pkt_dts"] = packet["dts"]
        frames.append(frame)
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "avg_frame_rate": avg_rate,
        "r_frame_rate": r_rate,
        "time_base": time_base,
    }
    raw = {
        "schema_version": 1,
        "status": status,
        "metadata": {"streams": streams if streams is not None else [video]},
        "packets": packets,
        "decoder_frames": frames,
        "diagnostics": [],
    }
    candidate = Candidate(source_path=Path("source.mp4"), target_path=Path("target.mp4"))
    return ClipAnalysis(candidate=candidate, raw=raw)


def test_confirms_full_documented_pattern_in_decoder_order():
    result = classify(_clip())

    assert result.kind == "confirmed"
    assert result.frame_rate == Fraction(24, 1)
    assert result.decoder_frame_count == 5
    assert result.evidence["decoder_order_regressions"][0]["pts"] == 100
    assert result.evidence["decoder_order_equal_pts"][0]["pts"] == 300
    assert result.evidence["repeated_pts_across_b_and_p"][0]["picture_types"] == ["B", "P"]
    assert result.evidence["b_frame_pts_equals_dts_count"] == 1


def test_clean_clip_has_explicit_not_affected_result():
    result = classify(_clip(
        output_pts=(0, 100, 200, 300, 400),
        output_types=("I", "P", "P", "P", "P"),
        output_packet_indices=(0, 1, 2, 3, 4),
        packet_pts=(0, 100, 200, 300, 400),
    ))

    assert result.kind == "clean"
    assert result.reason.startswith("Nicht betroffen:")
    assert not result.evidence["decoder_order_regression_count"]
    assert not result.evidence["decoder_order_equal_pts_count"]


def test_partial_signature_is_not_misclassified_as_confirmed_or_clean():
    result = classify(_clip(
        output_pts=(0, 100, 200, 300, 300),
        packet_pts=(0, 300, 200, 300, 100),
    ))

    assert result.kind == "unsupported"
    assert result.evidence["repeated_pts_across_b_and_p_count"] == 1
    assert not result.evidence["decoder_order_regression_count"]


def test_incomplete_probe_is_unclear():
    result = classify(_clip(status="partial"))

    assert result.kind == "unclear"
    assert "nicht vollständig" in result.reason


def test_frames_without_unique_packet_anchors_are_unclear():
    result = classify(_clip(include_anchors=False))

    assert result.kind == "unclear"
    assert result.evidence["frame_packet_mapping"] == "ambiguous_or_incomplete"


def test_conflicting_frame_rates_are_unsupported():
    result = classify(_clip(avg_rate="25/1", r_rate="24/1"))

    assert result.kind == "unsupported"
    assert "Bildraten" in result.reason


def test_irregular_dts_is_not_treated_as_the_known_case():
    result = classify(_clip(dts=(0, 100, 190, 300, 400)))

    assert result.kind == "unsupported"
    assert result.evidence["irregular_dts_step_count"] == 2


def test_multiple_video_streams_are_unsupported():
    stream = {
        "index": 0, "codec_type": "video", "codec_name": "h264",
        "avg_frame_rate": "24/1", "r_frame_rate": "24/1", "time_base": "1/2400",
    }
    result = classify(_clip(streams=[stream, {**stream, "index": 1}]))

    assert result.kind == "unsupported"
    assert result.evidence["video_stream_count"] == 2


def test_rational_rate_uses_exact_integer_ticks():
    clip = _clip(
        output_pts=(0, 1001, 2002, 3003, 4004),
        output_types=("I", "P", "P", "P", "P"),
        output_packet_indices=(0, 1, 2, 3, 4),
        packet_pts=(0, 1001, 2002, 3003, 4004),
        dts=(0, 1001, 2002, 3003, 4004),
        avg_rate="24000/1001",
        r_rate="24000/1001",
        time_base="1/24000",
    )

    result = classify(clip)

    assert result.kind == "clean"
    assert result.frame_rate == Fraction(24000, 1001)
    assert result.evidence["expected_frame_ticks"] == 1001
