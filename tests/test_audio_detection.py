from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from transcoder import _analysis as analysis


def make_source(levels):
    rate = 10
    samples = np.repeat(np.asarray(levels, dtype=np.float64).T, rate, axis=0).astype(np.float32)
    channels = [
        SimpleNamespace(number=index + 1, stream=SimpleNamespace(file=0, ordinal=index))
        for index in range(len(levels))
    ]
    source = SimpleNamespace(
        channels=channels,
        files=[SimpleNamespace(name="synthetic.wav")],
        sample_rate=rate,
        duration=len(levels[0]),
    )
    return source, samples


def check_levels(levels, *, full=False):
    source, samples = make_source(levels)
    values = analysis.section(
        "audio",
        {"samples": {"per_minute": 0.1, "seconds": 10, "full": full}},
    )
    with patch.object(analysis, "_audio_blocks", return_value=iter([samples])):
        return analysis.check_audio(source, values, lambda *_: None)


def test_detects_four_channels_with_zero_and_minus_80_dbfs():
    result = check_levels([
        [0.1] * 10,
        [0.1] * 10,
        [0.0] * 10,
        [0.0001] * 10,
    ])

    assert result["audible"] == [1, 2]
    assert [(channel["points"], channel["votes"]) for channel in result["channels"]] == [
        (10, 10), (10, 10), (10, 0), (10, 0)
    ]
    assert result["channels"][2]["peak_db"] is None
    assert result["channels"][3]["peak_db"] == -80.0


def test_keeps_a_quiet_but_real_track_audible():
    result = check_levels([
        [0.0017782794] * 10,  # -55 dBFS
        [0.1] * 10,
    ])

    assert result["audible"] == [1, 2]
    assert result["channels"][0]["votes"] == 10
    assert result["channels"][0]["points"] == 10


def test_track_present_for_only_thirty_percent_is_not_audible():
    result = check_levels([
        [0.1, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.1] * 10,
    ])

    channel = result["channels"][0]
    assert result["audible"] == [2]
    assert channel["points"] == 10
    assert channel["votes"] == 3
    assert channel["audible"] is False
    assert channel["active"] == 30.0
    assert channel["peak_db"] == -20.0
    assert channel["trust"] == analysis._rated(3, 10, 0.5, False)[1]


def test_all_levels_below_threshold_are_silent_and_count_windows():
    result = check_levels([
        [0.0] * 10,
        [0.0001] * 10,
        [0.00001] * 10,
        [0.0001] * 10,
    ])

    assert result["audible"] == []
    assert all(channel["points"] == 10 for channel in result["channels"])
    assert all(channel["votes"] == 0 for channel in result["channels"])
    assert all(channel["audible"] is False for channel in result["channels"])


def test_audio_defaults_replace_minimum_active_with_positive_gap():
    values = analysis.section("audio")
    assert values["silence_threshold"] == "-60dB"
    assert values["silence_window"] == "1s"
    assert values["silence_gap"] == "20dB"
    assert "silence_min_active" not in values
    with pytest.raises(ValueError, match="silence_gap"):
        analysis.section("audio", {"silence_gap": "0dB"})


def test_unmeasurable_windows_do_not_count_when_other_windows_are_valid():
    result = check_levels([
        [0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0] * 10,
    ])

    assert result["audible"] == [1]
    assert result["channels"][0]["points"] == 2
    assert result["channels"][0]["votes"] == 2
    assert result["channels"][1]["points"] == 2
    assert result["channels"][1]["votes"] == 0


def test_full_samples_report_perfect_trust():
    result = check_levels([[0.1] * 10, [0.0] * 10], full=True)

    assert all(channel["trust"] == 1.0 for channel in result["channels"])
