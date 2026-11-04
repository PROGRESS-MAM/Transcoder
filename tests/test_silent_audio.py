import json
import shutil
import subprocess
from pathlib import Path

import pytest

import transcoder
from transcoder import _render


def _preset_values(layout="stereo"):
    return {
        "container": "mp4",
        "video": {
            "codec": "mpeg4",
            "frame": "source",
            "scan": "source",
            "pix_fmt": "yuv420p",
        },
        "audio": {"codec": "aac", "layout": layout, "sample_rate": 48000},
    }


def _silent_video(path: Path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg und ffprobe werden benötigt")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=25:d=1",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return transcoder.source(path)


def _render_output(source, destination, monkeypatch, layout="stereo", **options):
    monkeypatch.setattr(_render._presets, "load", lambda _name: ("test", _preset_values(layout)))
    return transcoder.render(source, transcoder.output("test", destination, **options))[0]


def test_silent_audio_creates_full_length_aac_stereo(tmp_path, monkeypatch):
    source = _silent_video(tmp_path / "source.mp4")
    output = tmp_path / "proxy.mp4"

    result = _render_output(source, output, monkeypatch, silent_audio=True)

    assert result.state == "ok"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(probe.stdout)
    audio = [stream for stream in metadata["streams"] if stream["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] == "aac"
    assert audio[0]["channels"] == 2
    assert audio[0]["channel_layout"] == "stereo"
    assert audio[0]["sample_rate"] == "48000"

    silence = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    assert silence.stdout
    assert not any(silence.stdout)

    source_duration = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(tmp_path / "source.mp4")],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    output_duration = float(metadata["format"]["duration"])
    assert abs(output_duration - source_duration) <= 1 / 25


def test_missing_audio_keeps_legacy_error(tmp_path, monkeypatch):
    source = _silent_video(tmp_path / "source.mp4")

    result = _render_output(source, tmp_path / "proxy.mp4", monkeypatch)

    assert result.state == "error"
    assert "Source hat keinen Ton" in result.summary


def test_silent_audio_rejects_non_stereo_layout(tmp_path, monkeypatch):
    source = _silent_video(tmp_path / "source.mp4")

    result = _render_output(source, tmp_path / "proxy.mp4", monkeypatch, layout="mono", silent_audio=True)

    assert result.state == "error"
    assert "Source hat keinen Ton" in result.summary
