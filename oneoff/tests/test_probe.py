import json
import shutil
import subprocess
from pathlib import Path

import pytest

from oneoff.analysis import ProbeError, collect_clip


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required for generated clips"
)


def _make_clip(path: Path, b_frames: int) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "12",
            "-bf",
            str(b_frames),
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.parametrize("b_frames, has_b_frames", [(2, True), (0, False)])
def test_collects_complete_clip_data(tmp_path: Path, b_frames: int, has_b_frames: bool) -> None:
    clip = tmp_path / f"clip-b{b_frames}.mp4"
    _make_clip(clip, b_frames)
    original = clip.read_bytes()

    original_result = collect_clip(clip)
    updates = []
    result = collect_clip(clip, progress=lambda *update: updates.append(update))

    assert {key: value for key, value in result.items() if key != "analysed_at"} == {
        key: value for key, value in original_result.items() if key != "analysed_at"
    }
    assert {update[0] for update in updates} == {"packets", "frames"}
    assert all(len(update) == 4 for update in updates)
    final_counts = {phase: current for phase, current, _, _ in updates}
    assert final_counts["packets"] == len(result["packets"])
    assert final_counts["frames"] == len(result["frames"])
    assert set(result) == {
        "file",
        "analysed_at",
        "tools",
        "tool_paths",
        "commands",
        "diagnostics",
        "inventory",
        "packets",
        "frames",
    }
    assert result["file"] == {
        "name": clip.name,
        "abs_path": str(clip.resolve()),
        "size": len(original),
    }
    assert result["tools"]["ffprobe"].startswith("ffprobe version")
    assert result["tools"]["ffmpeg"].startswith("ffmpeg version")
    assert result["tool_paths"] == {"ffprobe": FFPROBE, "ffmpeg": FFMPEG}
    assert len(result["packets"]) == 12
    assert len(result["frames"]) == 12
    assert result["inventory"]["frame_counts"]["decoded_video_total"] == 12
    assert any(frame["codec_type"] == "audio" for frame in result["inventory"]["streams"])
    assert any(frame["pict_type"] == "B" for frame in result["frames"]) is has_b_frames
    assert all("time_base" in packet for packet in result["packets"])
    assert all("best_effort_timestamp" in frame for frame in result["frames"])
    assert set(result["commands"]) == set(result["diagnostics"])
    json.dumps(result)
    assert clip.read_bytes() == original


def test_unreadable_path_has_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mp4"
    with pytest.raises(ProbeError, match="Cannot read media file"):
        collect_clip(missing)
