import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))

import repariere_balken as r


def test_stream_info_parses(monkeypatch, tmp_path):
    payload = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "data", "codec_name": "tmcd", "tags": {"timecode": "10:00:00:00"}},
        ],
        "format": {"duration": "12.5"},
    }
    monkeypatch.setattr(r.pb, "_run", lambda command, timeout: json.dumps(payload).encode())

    assert r.stream_info(tmp_path / "a.mp4") == {
        "video": "h264", "width": 1920, "height": 1080, "audio": 2,
        "duration": 12.5, "timecode": "10:00:00:00",
    }


def test_repair_skips_unaffected(monkeypatch, tmp_path):
    monkeypatch.setattr(r.pb, "check_file", lambda path: {"state": "nur oben/unten"})
    monkeypatch.setattr(r.pb, "_run", lambda *args: (_ for _ in ()).throw(AssertionError("unexpected encode")))

    assert r.repair_file(tmp_path / "a.mp4") == {"state": "übersprungen", "detail": "nur oben/unten"}


def test_repair_replaces_after_check(monkeypatch, tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"old")
    monkeypatch.setattr(r.pb, "check_file", lambda item: {
        "state": "betroffen", "top_bottom": {"first": 60, "second": 60},
        "left_right": {"first": 120, "second": 120},
    })
    monkeypatch.setattr(r, "stream_info", lambda item: {
        "video": "h264", "width": 1920, "height": 1080, "audio": 1,
        "duration": 10.0, "timecode": None,
    })

    def fake_run(command, timeout):
        Path(command[-1]).write_bytes(b"new")
        return b""

    monkeypatch.setattr(r.pb, "_run", fake_run)
    result = r.repair_file(path)

    assert result["state"] == "repariert"
    assert "1706×960 bei 107,60 → 1920×1080" in result["detail"]
    assert path.read_bytes() == b"new"
    assert not Path(str(path) + r.part_suffix).exists()


def test_repair_keeps_original_on_failed_check(monkeypatch, tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"old")
    monkeypatch.setattr(r.pb, "check_file", lambda item: {
        "state": "betroffen", "top_bottom": {"first": 60, "second": 60},
        "left_right": {"first": 120, "second": 120},
    })

    def fake_stream_info(item):
        if str(item).endswith(r.part_suffix):
            return {"video": "h264", "width": 1918, "height": 1080, "audio": 1, "duration": 10.0, "timecode": "10:00:00:01"}
        return {"video": "h264", "width": 1920, "height": 1080, "audio": 1, "duration": 10.0, "timecode": "10:00:00:00"}

    monkeypatch.setattr(r, "stream_info", fake_stream_info)
    monkeypatch.setattr(r.pb, "_run", lambda command, timeout: Path(command[-1]).write_bytes(b"new"))

    result = r.repair_file(path)

    assert result["state"] == "Fehler"
    assert "Größe" in result["detail"]
    assert "Timecode" in result["detail"]
    assert path.read_bytes() == b"old"
    assert not Path(str(path) + r.part_suffix).exists()


def test_repair_keeps_original_on_encode_error(monkeypatch, tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"old")
    monkeypatch.setattr(r.pb, "check_file", lambda item: {
        "state": "betroffen", "top_bottom": {"first": 60, "second": 60},
        "left_right": {"first": 120, "second": 120},
    })
    monkeypatch.setattr(r, "stream_info", lambda item: {
        "video": "h264", "width": 1920, "height": 1080, "audio": 1,
        "duration": 10.0, "timecode": None,
    })
    original_run = r.pb._run

    def fail_after_write(command, timeout):
        Path(command[-1]).write_bytes(b"partial")
        raise RuntimeError("ffmpeg fehlgeschlagen")

    monkeypatch.setattr(r.pb, "_run", fail_after_write)
    result = r.repair_file(path)
    assert result == {"state": "Fehler", "detail": "ffmpeg fehlgeschlagen"}
    assert path.read_bytes() == b"old"
    assert not Path(str(path) + r.part_suffix).exists()

    def timeout_after_write(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise r.pb.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(r.pb, "_run", original_run)
    monkeypatch.setattr(r.pb.subprocess, "run", timeout_after_write)
    result = r.repair_file(path)
    assert result["state"] == "Fehler"
    assert "Zeitlimit" in result["detail"]
    assert path.read_bytes() == b"old"
    assert not Path(str(path) + r.part_suffix).exists()


def test_repair_removes_part_on_interrupt(monkeypatch, tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"old")
    monkeypatch.setattr(r.pb, "check_file", lambda item: {
        "state": "betroffen", "top_bottom": {"first": 60, "second": 60},
        "left_right": {"first": 120, "second": 120},
    })
    monkeypatch.setattr(r, "stream_info", lambda item: {
        "video": "h264", "width": 1920, "height": 1080, "audio": 1,
        "duration": 10.0, "timecode": None,
    })

    def interrupt_after_write(command, timeout):
        Path(command[-1]).write_bytes(b"partial")
        raise KeyboardInterrupt()

    monkeypatch.setattr(r.pb, "_run", interrupt_after_write)
    with pytest.raises(KeyboardInterrupt):
        r.repair_file(path)

    assert path.read_bytes() == b"old"
    assert not Path(str(path) + r.part_suffix).exists()


def test_encode_gets_timeout(monkeypatch, tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(b"old")
    monkeypatch.setattr(r.pb, "check_file", lambda item: {
        "state": "betroffen", "top_bottom": {"first": 60, "second": 60},
        "left_right": {"first": 120, "second": 120},
    })
    monkeypatch.setattr(r, "stream_info", lambda item: {
        "video": "h264", "width": 1920, "height": 1080, "audio": 1,
        "duration": 10.0, "timecode": None,
    })
    observed = []

    def fake_run(command, timeout):
        observed.append(timeout)
        Path(command[-1]).write_bytes(b"new")

    monkeypatch.setattr(r.pb, "_run", fake_run)
    r.repair_file(path)

    assert observed == [r.encode_timeout(10.0)]


def test_main_stops_without_nvenc(monkeypatch):
    events = []

    def fail_nvenc():
        events.append("nvenc")
        raise RuntimeError("kein Gerät")

    monkeypatch.setattr(r, "check_nvenc", fail_nvenc)
    monkeypatch.setattr(r, "log", lambda text: events.append("log"))
    monkeypatch.setattr(r, "repair_file", lambda path: (_ for _ in ()).throw(AssertionError("unexpected repair")))

    assert r.main() == 1
    assert events == ["nvenc", "log", "log"]


def test_main_summary(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "a"
    folder.mkdir()
    for name in ("x.mp4", "y.mp4", "z.mov"):
        (folder / name).write_bytes(b"")
    monkeypatch.setattr(r, "source_folders", (folder, tmp_path / "fehlt"))
    monkeypatch.setattr(r, "check_nvenc", lambda: None)
    monkeypatch.setattr(r, "repair_file", lambda path: {
        "state": "repariert" if path.name == "x.mp4" else "übersprungen", "detail": "ok",
    })
    log_path = tmp_path / "run.log"
    monkeypatch.setattr(r, "log_path", log_path)

    assert r.main() == 0
    output = capsys.readouterr().out
    assert "Geprüft: 2 MP4-Dateien" in output
    assert "Repariert: 1" in output
    assert "Übersprungen: 1" in output
    assert "Ordner nicht erreichbar" in output
    assert log_path.exists()
    assert "Repariert: 1" in log_path.read_text(encoding="utf-8")


def _create_source(path: Path, width: int, height: int) -> None:
    pad_filter = f"pad=1920:{height}:(1920-{width})/2:0:color=0x202020,pad=1920:1080:0:(1080-{height})/2:color=black"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", "6",
        "-vf", pad_filter, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", "-timecode", "10:00:00:00", "-write_tmcd", "1", str(path),
    ], check=True, capture_output=True, timeout=60)


def _real_repair(path: Path, width: int, height: int, monkeypatch):
    monkeypatch.setattr(r, "encoder_args", (
        "-c:v", "libx264", "-profile:v", "high", "-b:v", "10M", "-fps_mode:v", "passthrough",
    ))
    _create_source(path, width, height)
    result = r.repair_file(path)
    assert result["state"] == "repariert"
    info = r.stream_info(path)
    assert info["audio"] == 1
    assert info["timecode"] == "10:00:00:00"
    assert info["width"] == 1920
    assert info["height"] == 1080
    assert not Path(str(path) + r.part_suffix).exists()
    return result


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe not in PATH")
def test_real_repair_enough_side_bars(monkeypatch, tmp_path):
    result = _real_repair(tmp_path / "a.mp4", 1400, 900, monkeypatch)

    assert "(oben/unten bleiben Balken)" not in result["detail"]
    checked = r.pb.check_file(tmp_path / "a.mp4")
    assert checked["top_bottom"] is None
    assert checked["state"] != "betroffen"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe not in PATH")
def test_real_repair_side_bars_too_narrow(monkeypatch, tmp_path):
    result = _real_repair(tmp_path / "a.mp4", 1600, 800, monkeypatch)

    assert "(oben/unten bleiben Balken)" in result["detail"]
    assert r.pb.check_file(tmp_path / "a.mp4")["left_right"] is None
