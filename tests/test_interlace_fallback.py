import pytest

from transcoder import _analysis
from transcoder._source import Source, StreamRef


def make_source(field_order="tt"):
    video_data = {} if field_order is None else {"field_order": field_order}
    return Source(
        files=(), probes=(), video=StreamRef(file=0, ordinal=0, data=video_data),
        channels=(), audio=(), fps=None, duration=0, start_tc="01:00:00:00", tc_origin="default",
    )


def test_unclear_analysis_uses_interlaced_metadata():
    values = _analysis.section("interlace", {"metadata_fallback": True})
    found = _analysis._rate_interlace(make_source("tt"), values, [(0, 0, 0, 0)])

    assert found["rating"] == "interlaced"
    assert found["trust"] == 1.0
    assert found["field_order"] == "tff"
    assert found["fallback"] == "metadata"
    assert found["measured_rating"] == "unclear"
    assert found["measured_trust"] == 0.0


def test_low_trust_analysis_uses_progressive_metadata():
    values = _analysis.section("interlace", {"metadata_fallback": True})
    found = _analysis._rate_interlace(make_source("progressive"), values, [(10, 0, 0, 0)])

    assert found["rating"] == "progressive"
    assert found["trust"] == 1.0
    assert found["field_order"] is None
    assert found["fallback"] == "metadata"
    assert found["measured_rating"] == "interlaced"
    assert found["measured_trust"] == 0.75


def test_fallback_is_off_by_default():
    values = _analysis.section("interlace")
    found = _analysis._rate_interlace(make_source("tt"), values, [(0, 0, 0, 0)])

    assert values["metadata_fallback"] is False
    assert found["rating"] == "unclear"
    assert found["trust"] == 0.0
    assert found["fallback"] is None


def test_missing_metadata_keeps_unclear():
    values = _analysis.section("interlace", {"metadata_fallback": True})
    found = _analysis._rate_interlace(make_source(None), values, [(0, 0, 0, 0)])

    assert found["rating"] == "unclear"
    assert found["fallback"] is None


def test_sure_analysis_ignores_metadata():
    values = _analysis.section("interlace", {"metadata_fallback": True})
    found = _analysis._rate_interlace(make_source("tt"), values, [(0, 0, 10, 0)] * 5)

    assert found["rating"] == "progressive"
    assert found["trust"] >= 0.9
    assert found["fallback"] is None


def test_metadata_fallback_must_be_bool():
    with pytest.raises(ValueError, match="interlace.metadata_fallback muss True oder False sein."):
        _analysis.section("interlace", {"metadata_fallback": "yes"})


def test_fix_interlace_accepts_fallback_report():
    values = _analysis.section("interlace", {"metadata_fallback": True})
    found = _analysis._rate_interlace(make_source("tt"), values, [(0, 0, 0, 0)])

    assert _analysis.fix_interlace(found, values, "progressive") == ({"deinterlace": True}, "deinterlaced")
