from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT))

if "toolbox" not in sys.modules:
    from types import ModuleType
    toolbox = ModuleType("toolbox")
    toolbox.tb_write_log = lambda *args: None
    sys.modules["toolbox"] = toolbox

import transcoder
from marathon import transcode_marathon as marathon


def test_marathon_settings_enable_sides():
    assert marathon.settings["black_bars"] == {"extra_crop": 0, "sides": True}


def test_analysis_text_shows_sides():
    reports = {
        "streams": {"video": {"width": 1920, "height": 1080, "fps": 25}},
        "interlace": {"rating": "progressive"},
        "audio": {"audible": [1, 2], "channels": [1, 2]},
        "black_bars": {
            "rating": "bars",
            "sides": {"left": 120, "right": 120, "level_left": 40.0, "level_right": 40.0},
        },
    }
    assert "Ränder bars, Seitenbalken 120/120 px" in marathon._analysis_text(reports)
    del reports["black_bars"]["sides"]
    assert "Ränder bars, hörbar" in marathon._analysis_text(reports)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg und ffprobe werden benötigt")
def test_real_render_windowbox(tmp_path):
    source_file = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1400x900:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-vf", "pad=1920:900:260:0:color=0x282828,pad=1920:1080:0:90:color=black",
        "-ac", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "10",
        str(source_file),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

    on = transcoder.render(
        transcoder.source(source_file),
        [transcoder.output("h264_10mbit_1080p", tmp_path / "on.mp4", [
            transcoder.fix("black_bars", settings={"sides": True, "samples": {"full": True}})
        ])],
    )[0]
    assert on.state == "ok"
    assert on.fixes[0]["detail"] == "beschnitten auf 1600×900 bei 160,90"

    off = transcoder.render(
        transcoder.source(source_file),
        [transcoder.output("h264_10mbit_1080p", tmp_path / "off.mp4", [
            transcoder.fix("black_bars", settings={"samples": {"full": True}})
        ])],
    )[0]
    assert off.state == "ok"
    assert off.fixes[0]["detail"] == "beschnitten auf 1920×900 bei 0,90"
