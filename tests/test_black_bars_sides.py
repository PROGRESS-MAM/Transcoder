from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcoder import _analysis as a, _render as r


def _samples(width=1920, height=1080, top=0, bottom=0, left=0, right=0, side_levels=None,
             side_level=40, n=10, seed=0):
    rng = np.random.default_rng(seed)
    samples = []
    for i in range(n):
        image = rng.integers(40, 220, (height, width))
        if top:
            image[:top] = 0
        if bottom:
            image[height - bottom:] = 0
        level = side_levels[i] if side_levels else side_level
        if left:
            image[top:height - bottom, :left] = level + rng.integers(-1, 2, (height - top - bottom, left))
        if right:
            image[top:height - bottom, width - right:] = level + rng.integers(-1, 2,
                                                                               (height - top - bottom, right))
        samples.append([a._gray_lines(image.astype(np.uint8).tobytes(), width, height)])
    return samples


def src(width, height, sar):
    return SimpleNamespace(width=width, height=height, sar=sar)


on = a.section("black_bars", {"sides": True})
off = a.section("black_bars", {})
F = (1920, 1080, Fraction(1))


def test_gray_side_bars_detected():
    found = a._rate_black_bars(src(1920, 1080, 1), on,
                               _samples(top=60, bottom=60, left=120, right=120, side_level=40))
    assert found["sides"]["left"] == 120 == found["sides"]["right"]
    assert 38 <= found["sides"]["level_left"] <= 42
    assert found["bars"] == {"top": 60, "bottom": 60, "left": 0, "right": 0}


def test_bright_sides_ignored():
    found = a._rate_black_bars(src(1920, 1080, 1), on, _samples(left=120, right=120, side_level=100))
    assert found["sides"] is None


def test_asymmetric_sides_ignored():
    found = a._rate_black_bars(src(1920, 1080, 1), on, _samples(left=120, right=60))
    assert found["sides"] is None


def test_changing_sides_ignored():
    found = a._rate_black_bars(src(1920, 1080, 1), on,
                               _samples(left=120, right=120, side_levels=[10 * i for i in range(10)]))
    assert found["sides"] is None


def test_windowbox_fills_frame():
    found = a._rate_black_bars(src(1920, 1080, 1), on,
                               _samples(top=60, bottom=60, left=120, right=120, side_level=40))
    crop = a.fix_black_bars(found, on, 1920, 1080, Fraction(1), F)[0]["crop"]
    assert crop == (106, 60, 1708, 960)
    assert r.fit(1708, 960, Fraction(1), F) == (1920, 1080)


def test_side_bars_too_narrow():
    found = a._rate_black_bars(src(1920, 1080, 1), on,
                               _samples(top=100, bottom=100, left=40, right=40))
    assert a.fix_black_bars(found, on, 1920, 1080, Fraction(1), F)[0]["crop"] == (40, 100, 1840, 880)


def test_wide_source_without_letterbox():
    found = a._rate_black_bars(src(2048, 1080, 1), on, _samples(width=2048, left=128, right=128))
    assert a.fix_black_bars(found, on, 2048, 1080, Fraction(1), F)[0]["crop"] == (64, 0, 1920, 1080)


def test_pillarbox_only_unchanged():
    found = a._rate_black_bars(src(1920, 1080, 1), on, _samples(left=240, right=240))
    assert a.fix_black_bars(found, on, 1920, 1080, Fraction(1), F) == ({}, None)


def test_sides_off_or_no_frame_unchanged():
    found = a._rate_black_bars(src(1920, 1080, 1), on,
                               _samples(top=60, bottom=60, left=120, right=120, side_level=40))
    assert a.fix_black_bars(found, off, 1920, 1080, Fraction(1), F)[0]["crop"] == (0, 60, 1920, 960)
    assert a.fix_black_bars(found, on, 1920, 1080, Fraction(1), None)[0]["crop"] == (0, 60, 1920, 960)


def test_anamorphic():
    assert a.side_crop((0, 72, 720, 432), {"left": 40, "right": 40}, 720, 576,
                       Fraction(64, 45), F) == (40, 72, 640, 432)


def test_side_crop_keeps_picture():
    for top in range(0, 200, 23):
        for bottom in range(0, 200, 23):
            box = None if top == bottom == 0 else (0, top, 1920, 1080 - top - bottom)
            for left in range(4, 300, 19):
                for right in range(4, 300, 19):
                    crop = a.side_crop(box, {"left": left, "right": right}, 1920, 1080, Fraction(1), F)
                    if crop is not None:
                        x, y, width, height = crop
                        assert y == (box[1] if box else 0)
                        assert height == (box[3] if box else 1080)
                        assert x <= left and x + width >= 1920 - right
                        assert x % 2 == width % 2 == 0


def test_sides_must_be_bool():
    try:
        a.section("black_bars", {"sides": "ja"})
    except ValueError:
        return
    raise AssertionError("black_bars.sides must be validated as bool")
