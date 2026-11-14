import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))

import repariere_balken as r


def test_crop_enough_side_bars():
    box = r.crop_box(1920, 1080, 60, 60, 120, 120)
    assert box == (107, 60, 1706, 960)
    assert r.picture_size(1706, 960, 1920, 1080) == (1920, 1080)
    assert r.video_filter(box, 1920, 1080) == (
        "crop=1706:960:107:60:exact=1,scale=1920:1080:flags=lanczos:"
        "in_color_matrix=auto:out_color_matrix=bt709,setsar=1/1,format=yuv420p"
    )


def test_crop_side_bars_too_narrow():
    box = r.crop_box(1920, 1080, 100, 100, 40, 40)
    assert box == (40, 100, 1840, 880)
    assert r.picture_size(1840, 880, 1920, 1080) == (1920, 918)
    assert "pad=1920:1080:0:80:color=black" in r.video_filter(box, 1920, 1080)


def test_crop_asymmetric():
    box = r.crop_box(1920, 1080, 60, 62, 100, 110)
    assert box == (100, 60, 1710, 958)
    assert r.picture_size(1710, 958, 1920, 1080) == (1920, 1076)


def test_crop_never_cuts_picture():
    for top in range(4, 200, 13):
        for bottom in range(4, 200, 13):
            for left in range(4, 400, 17):
                for right in range(4, 400, 17):
                    x, y, width, height = r.crop_box(1920, 1080, top, bottom, left, right)
                    assert y == top
                    assert height == 1080 - top - bottom
                    assert x <= left
                    assert x + width >= 1920 - right
                    assert 0 <= x
                    assert x + width <= 1920


def test_encode_command():
    command = r.encode_command(
        "ffmpeg", Path("a.mp4"), Path("a.mp4.part"), (107, 60, 1706, 960), 1920, 1080, "10:00:00:00"
    )
    assert command[:8] == ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", "a.mp4"]
    assert command[-3:] == ["-f", "mp4", "a.mp4.part"]
    assert command.index("-c:a") < command.index("copy")
    assert command[command.index("-timecode") + 1] == "10:00:00:00"
    assert "h264_nvenc" in command
    assert "passthrough" in command


def test_encode_command_without_timecode():
    command = r.encode_command(
        "ffmpeg", Path("a.mp4"), Path("a.mp4.part"), (107, 60, 1706, 960), 1920, 1080, None
    )
    assert "-timecode" not in command
    assert "-write_tmcd" in command


def test_encode_timeout():
    assert r.encode_timeout(100.0) == 500.0
