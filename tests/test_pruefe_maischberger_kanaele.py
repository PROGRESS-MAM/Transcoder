from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "oneoff"))

import pruefe_maischberger_kanaele as p
from transcoder import _analysis


@pytest.fixture(scope="module")
def signal() -> np.ndarray:
    rate = 48000
    rng = np.random.default_rng(20250308)
    t = np.arange(rate * 10, dtype=np.float64) / rate
    frequency = 110.0 + 20.0 * np.sin(2.0 * np.pi * 0.7 * t)
    phase = 2.0 * np.pi * np.cumsum(frequency) / rate
    harmonics = sum(np.sin(k * phase) / k for k in range(1, 25))
    envelope = np.clip(np.sin(2.0 * np.pi * 3.0 * t) + rng.normal(0.0, 0.03, t.size), 0.0, None)
    values = harmonics * envelope + rng.normal(0.0, 0.02, t.size)
    return values * (0.25 / np.max(np.abs(values)))


def test_identical_pair_and_verdict(signal: np.ndarray) -> None:
    values = p.pair_values(signal, 2.0 * signal, 48000)
    assert values["near_max"] >= 0.99
    assert values["peak"] >= 0.99
    assert abs(values["peak_lag_ms"]) <= 0.2
    assert values["level_db"] == pytest.approx(6.02, abs=0.05)
    assert p.rate_pair([values] * 5)["verdict"] == "identisch"


def test_inverted_pair_and_verdict(signal: np.ndarray) -> None:
    values = p.pair_values(signal, -2.0 * signal, 48000)
    assert values["peak"] <= -0.99
    assert abs(values["peak_lag_ms"]) <= 0.2
    assert values["r0"] <= -0.99
    assert values["near_max"] < 0.9
    summary = p.rate_pair([values])
    assert summary["verdict"] == "verpolt"
    assert summary["inverted_share"] == 1.0


def test_inverted_similarity_matches_absolute_peak(signal: np.ndarray) -> None:
    rate = 48000
    factor = max(1, rate // 8000)
    a = signal.astype(np.float64)
    b = -2.0 * a
    reduced_a = a[:len(a) // factor * factor].reshape(-1, factor).mean(axis=1)
    reduced_b = b[:len(b) // factor * factor].reshape(-1, factor).mean(axis=1)
    reduced_a -= reduced_a.mean()
    reduced_b -= reduced_b.mean()
    lag = round(40 / 1000 * rate / factor)
    size = 1 << (len(reduced_a) + lag).bit_length()
    norms = np.array([np.sqrt(np.sum(reduced_a ** 2)) + 1e-12,
                      np.sqrt(np.sum(reduced_b ** 2)) + 1e-12])
    spectra = [np.fft.rfft(reduced_a, size), np.fft.rfft(reduced_b, size)]
    similarity, offset, inverted = _analysis._similarity(spectra, norms, 0, 1, size, lag)
    values = p.pair_values(a, b, rate)
    assert similarity == pytest.approx(abs(values["peak"]), abs=1e-6)
    assert inverted is True
    assert offset == 0


def test_shifted_pair_and_verdict(signal: np.ndarray) -> None:
    values = p.pair_values(signal, np.roll(signal, 3840), 48000)
    assert values["peak"] >= 0.95
    assert abs(abs(values["peak_lag_ms"]) - 80.0) <= 0.5
    assert values["near_max"] < 0.5
    assert p.rate_pair([values])["verdict"] == "identisch, versetzt"


def test_different_pair_and_verdict() -> None:
    rng = np.random.default_rng(8741)
    a = rng.normal(size=48000)
    b = np.random.default_rng(9917).normal(size=48000)
    values = p.pair_values(a, b, 48000)
    assert abs(values["peak"]) < 0.2
    assert p.rate_pair([values])["verdict"] == "verschieden"


def test_empty_summary() -> None:
    summary = p.rate_pair([])
    assert summary["blocks"] == 0
    assert summary["verdict"] == "nicht vergleichbar"
    assert summary["inverted_share"] == 0.0
    for key in ("r0", "near_max", "peak", "near_lag_ms", "peak_lag_ms", "level_db"):
        assert summary[key] is None


def test_channel_levels() -> None:
    rate = 48000
    t = np.arange(rate, dtype=np.float64) / rate
    block = np.stack([0.5 * np.sin(2.0 * np.pi * 1000.0 * t), np.zeros(rate)], axis=1)
    rms_db, peak_db = p.channel_levels(block)
    assert rms_db.dtype == np.float64
    assert peak_db.dtype == np.float64
    assert rms_db[0] == pytest.approx(-9.03, abs=0.05)
    assert peak_db[0] == pytest.approx(-6.02, abs=0.05)
    assert rms_db[1] == -120.0
    assert peak_db[1] == -120.0
