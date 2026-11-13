import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))
import pruefe_balken as p


def test_read_config_ok(tmp_path):
    folder = tmp_path / "media"
    folder.mkdir()
    config = tmp_path / "pruefe_balken.ini"
    config.write_text(f"[paths]\npruef_path = {folder}\n", encoding="utf-8-sig")
    assert p.read_config(config) == folder


def test_read_config_missing_file(tmp_path):
    with pytest.raises(ValueError, match="pruefe_balken.ini"):
        p.read_config(tmp_path / "pruefe_balken.ini")


def test_read_config_missing_key(tmp_path):
    config = tmp_path / "pruefe_balken.ini"
    config.write_text("[paths]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pruef_path"):
        p.read_config(config)


def test_read_config_missing_folder(tmp_path):
    config = tmp_path / "pruefe_balken.ini"
    config.write_text(f"[paths]\npruef_path = {tmp_path / 'missing'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Prüfpfad nicht gefunden"):
        p.read_config(config)


def test_mp4_files_non_recursive(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.mp4", "B.MP4", "c.mov", "sub/d.mp4"):
        (tmp_path / name).touch()
    assert [path.name for path in p.mp4_files(tmp_path)] == ["a.mp4", "B.MP4"]


def test_sample_times():
    assert p.sample_times(100.0, 4) == [12.5, 37.5, 62.5, 87.5]


def test_read_frame_size(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_find_tool", lambda name: name)
    monkeypatch.setattr(p, "_run", lambda command, timeout: b"x" * 10)
    assert p.read_frame(tmp_path / "x.mp4", 0.5, 4, 3) is None
    monkeypatch.setattr(p, "_run", lambda command, timeout: bytes(range(12)))
    frame = p.read_frame(tmp_path / "x.mp4", 0.5, 4, 3)
    assert frame is not None and frame.shape == (3, 4)


def test_run_timeout():
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="Zeitlimit"):
        p._run([sys.executable, "-c", "import time; time.sleep(5)"], 0.5)
    assert time.monotonic() - start < 3


def test_run_error_exit():
    with pytest.raises(RuntimeError, match="Exit-Code 3.*kaputt"):
        p._run([sys.executable, "-c", "import sys; sys.stderr.write('kaputt'); sys.exit(3)"], 2)


def test_check_file_error_state(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "probe", lambda path: (_ for _ in ()).throw(RuntimeError("kein Videostream")))
    assert p.check_file(tmp_path / "x.mp4") == {"state": "Fehler", "reason": "kein Videostream"}


def test_check_file_rates_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "probe", lambda path: (480, 270, 100.0))
    rng = np.random.default_rng(0)

    def read_frame(path, sample_time, width, height):
        frame = rng.integers(40, 220, (height, width), dtype=np.uint8)
        frame[:30] = 0
        frame[-30:] = 0
        frame[:, :40] = 20
        frame[:, -40:] = 20
        return frame

    monkeypatch.setattr(p, "read_frame", read_frame)
    assert p.check_file(tmp_path / "x.mp4")["state"] == "betroffen"


def test_check_file_no_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "probe", lambda path: (480, 270, 100.0))
    monkeypatch.setattr(p, "read_frame", lambda *args: None)
    assert p.check_file(tmp_path / "x.mp4") == {"state": "nicht prüfbar", "valid": 0}


def test_main_summary(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "x.mp4").touch()
    (folder / "y.mp4").touch()
    (tmp_path / "pruefe_balken.ini").write_text(f"[paths]\npruef_path = {folder}\n", encoding="utf-8")
    monkeypatch.setattr(p, "base_dir", tmp_path)
    monkeypatch.setattr(p, "check_file", lambda path: (
        {"state": "betroffen", "valid": 10, "top_bottom": None, "left_right": None}
        if path.name == "x.mp4"
        else {"state": "keine Balken", "valid": 10, "top_bottom": None, "left_right": None}
    ))
    assert p.main() == 0
    output = capsys.readouterr().out
    assert "Geprüft: 2 MP4-Dateien" in output
    assert "Betroffen (Balken an allen vier Seiten): 1" in output


def test_main_missing_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(p, "base_dir", tmp_path)
    assert p.main() == 1
    assert "pruefe_balken.ini" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe fehlen")
def test_real_ffmpeg_windowbox(tmp_path):
    path = tmp_path / "windowbox.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1600x800:rate=25", "-t", "6",
            "-vf", "pad=1760:800:80:0:color=0x202020,pad=1920:1080:80:140:color=black",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert p.check_file(path)["state"] == "betroffen"


def test_read_config_without_section(tmp_path):
    config = tmp_path / "pruefe_balken.ini"
    config.write_text("pruef_path = x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pruefe_balken.ini"):
        p.read_config(config)


def test_check_file_missing_width(monkeypatch, tmp_path):
    monkeypatch.setattr(p, "_find_tool", lambda name: name)
    monkeypatch.setattr(p, "_run", lambda command, timeout: b'{"streams": [{}], "format": {"duration": "10"}}')
    assert p.check_file(tmp_path / "x.mp4")["state"] == "Fehler"


def test_run_unexpected_error(monkeypatch, capsys):
    def fail():
        raise RuntimeError("x")

    monkeypatch.setattr(p, "main", fail)
    assert p.run() == 1
    assert "Unerwarteter Fehler: x" in capsys.readouterr().out
