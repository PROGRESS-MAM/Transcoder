import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))
import pruefe_balken as p


def _frames(
    top=30,
    bottom=30,
    left=40,
    right=40,
    top_level=0,
    bottom_level=0,
    left_level=20,
    right_level=20,
    n=10,
    h=270,
    w=480,
    seed=0,
):
    rng = np.random.default_rng(seed)
    images = []
    for _ in range(n):
        frame = rng.integers(40, 220, (h, w))
        frame[:top] = top_level
        if bottom > 0:
            frame[h - bottom:] = bottom_level
        if left > 0:
            frame[top:h - bottom, :left] = left_level + rng.integers(-1, 2, (h - top - bottom, left))
        if right > 0:
            frame[top:h - bottom, w - right:] = right_level + rng.integers(-1, 2, (h - top - bottom, right))
        images.append(frame.astype(np.uint8))
    return np.stack(images)


def test_four_sides():
    result = p.rate_frames(_frames())
    assert result["state"] == "betroffen"
    assert result["top_bottom"] == {
        "first": 30,
        "second": 30,
        "level_first": 0.0,
        "level_second": 0.0,
    }
    assert result["left_right"]["first"] == 40
    assert result["left_right"]["second"] == 40
    assert result["valid"] == 10


def test_dark_not_black():
    result = p.rate_frames(_frames(top_level=25, bottom_level=25, left_level=18, right_level=18))
    assert result["state"] == "betroffen"


def test_only_top_bottom():
    result = p.rate_frames(_frames(left=0, right=0))
    assert result["state"] == "nur oben/unten"
    assert result["left_right"] is None


def test_only_left_right():
    result = p.rate_frames(_frames(top=0, bottom=0))
    assert result["state"] == "nur links/rechts"


def test_no_bars():
    result = p.rate_frames(_frames(top=0, bottom=0, left=0, right=0))
    assert result["state"] == "keine Balken"


def test_asymmetric_width():
    result = p.rate_frames(_frames(left=40, right=20))
    assert result["left_right"] is None


def test_width_tolerance():
    within = p.rate_frames(_frames(top=30, bottom=33))
    outside = p.rate_frames(_frames(top=30, bottom=36))
    assert within["top_bottom"] is not None
    assert outside["top_bottom"] is None


def test_different_levels():
    result = p.rate_frames(_frames(left_level=20, right_level=40))
    assert result["left_right"] is None


def test_changing_edge_is_no_bar():
    frames = np.stack([
        _frames(top_level=10 * i, bottom_level=10 * i, n=1, seed=i)[0]
        for i in range(10)
    ])
    assert p.rate_frames(frames)["top_bottom"] is None


def test_flat_frames():
    result = p.rate_frames(np.zeros((10, 270, 480), dtype=np.uint8))
    assert result == {"state": "nicht prüfbar", "valid": 0}


def test_too_few_valid():
    result = p.rate_frames(_frames(n=4))
    assert result["state"] == "nicht prüfbar"
    assert result["valid"] == 4
