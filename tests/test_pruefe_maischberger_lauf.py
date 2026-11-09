from datetime import datetime
from pathlib import Path
import sys
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "oneoff"))

import pruefe_maischberger_kanaele as p


def make_signal() -> np.ndarray:
    rate = 48000
    rng = np.random.default_rng(20250308)
    t = np.arange(rate, dtype=np.float64) / rate
    frequency = 110.0 + 20.0 * np.sin(2.0 * np.pi * 0.7 * t)
    phase = 2.0 * np.pi * np.cumsum(frequency) / rate
    harmonics = sum(np.sin(k * phase) / k for k in range(1, 25))
    envelope = np.clip(np.sin(2.0 * np.pi * 3.0 * t) + rng.normal(0.0, 0.03, t.size), 0.0, None)
    values = harmonics * envelope + rng.normal(0.0, 0.02, t.size)
    return values * (0.25 / np.max(np.abs(values)))


def test_run_times_out_quickly() -> None:
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="Zeitlimit"):
        p._run([sys.executable, "-c", "import time; time.sleep(10)"], 0.5)
    assert time.monotonic() - started < 5


def test_run_includes_stderr_on_failure() -> None:
    with pytest.raises(RuntimeError, match="kaputt"):
        p._run([sys.executable, "-c", "import sys; sys.stderr.write('kaputt'); sys.exit(3)"], 10)


def test_media_files_filters_and_requires_results(tmp_path: Path) -> None:
    (tmp_path / "a.MXF").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.wav").write_bytes(b"")
    (tmp_path / "c.xml").write_bytes(b"")
    assert p._media_files(tmp_path) == [tmp_path / "a.MXF", tmp_path / "sub" / "b.wav"]
    with pytest.raises(FileNotFoundError):
        p._media_files(tmp_path / "missing")
    only_xml = tmp_path / "xml"
    only_xml.mkdir()
    (only_xml / "c.xml").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        p._media_files(only_xml)


def test_measure_clip_classifies_all_pairs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "clip.mov"
    signal = make_signal()
    streams = [{"codec_type": "audio", "channels": 1, "sample_rate": "48000"} for _ in range(4)]
    monkeypatch.setattr(p, "_media_files", lambda folder: [media])
    monkeypatch.setattr(p, "_probe", lambda path: {"format": {"duration": "600"}, "streams": streams})
    monkeypatch.setattr(
        p,
        "_decode",
        lambda path, stream_count, width, start, length, rate: np.column_stack(
            (signal, 0.9 * signal, -2.0 * signal, -1.8 * signal)
        ).astype(np.float32),
    )

    result = p.measure_clip(tmp_path)
    pairs = {item["channels"]: item for item in result["pairs"]}
    assert len(pairs) == 6
    assert pairs[(1, 2)]["verdict"] == "identisch"
    assert pairs[(3, 4)]["verdict"] == "identisch"
    for pair in ((1, 3), (1, 4), (2, 3), (2, 4)):
        assert pairs[pair]["verdict"] == "verpolt"
    assert pairs[(1, 3)]["level_db"] == pytest.approx(6.0, abs=0.1)
    assert result["samples"] == 20


def test_layout_without_audio_and_channel_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first, second = tmp_path / "first.mov", tmp_path / "second.wav"
    probes = {
        first: {"format": {"duration": "3"}, "streams": [{"codec_type": "audio", "channels": 2, "sample_rate": "44100"}]},
        second: {"format": {"duration": "5"}, "streams": [{"codec_type": "audio", "channels": 1, "sample_rate": "48000"}]},
    }
    monkeypatch.setattr(p, "_probe", lambda path: probes[path])
    layout, where, rate, duration = p._layout([first, second])
    assert len(layout) == 2
    assert where == ["first.mov a:0 k1", "first.mov a:0 k2", "second.wav a:0 k1"]
    assert (rate, duration) == (44100, 5.0)
    monkeypatch.setattr(p, "_probe", lambda path: {"streams": [{"codec_type": "video"}]})
    with pytest.raises(ValueError, match="Source hat keinen Ton\\."):
        p._layout([first])


def test_main_records_clip_error_and_interrupts_without_partial_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(p, "root_path", tmp_path)
    monkeypatch.setattr(p, "base_dir", tmp_path)
    monkeypatch.setattr(
        p,
        "clips",
        (("1", "CLIP_ONE", "One", "reason one"), ("2", "CLIP_TWO", "Two", "reason two"), ("3", "CLIP_THREE", "Three", "reason three")),
    )
    result = {"files": ["two.wav"], "samples": 1, "blocks": 1, "channels": [], "pairs": []}

    def measure(folder: Path) -> dict:
        clip_id = folder.name
        if clip_id == "1":
            raise RuntimeError("Zeitlimit 180 s überschritten: ffmpeg")
        if clip_id == "3":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(p, "measure_clip", measure)
    assert p.main() == 0
    reports = list(tmp_path.glob("maischberger_kanaele_*.txt"))
    assert len(reports) == 1
    assert not list(tmp_path.glob("*.tmp"))
    report = reports[0].read_text(encoding="utf-8")
    assert "ABGEBROCHEN" in report
    assert "Clips 2/3" in report
    assert "Zeitlimit 180 s überschritten: ffmpeg" in report
    assert "CLIP_TWO" in report
    assert "CLIP_THREE" not in report


def test_main_handles_interrupt_during_interim_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(p, "root_path", tmp_path)
    monkeypatch.setattr(p, "base_dir", tmp_path)
    monkeypatch.setattr(p, "clips", (("1", "CLIP_ONE", "One", "reason"), ("2", "CLIP_TWO", "Two", "reason")))
    monkeypatch.setattr(p, "measure_clip", lambda folder: {"files": [], "samples": 0, "blocks": 0, "channels": [], "pairs": []})
    write_report = p._write_report
    calls = 0

    def interrupt_once(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        write_report(path, text)

    monkeypatch.setattr(p, "_write_report", interrupt_once)
    assert p.main() == 0
    reports = list(tmp_path.glob("maischberger_kanaele_*.txt"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "ABGEBROCHEN" in report
    assert "Clips 1/2" in report
    assert "CLIP_ONE" in report
    assert "CLIP_TWO" not in report


def test_main_missing_root_returns_error_without_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(p, "root_path", tmp_path / "missing")
    monkeypatch.setattr(p, "base_dir", tmp_path)
    assert p.main() == 1
    assert not list(tmp_path.glob("maischberger_kanaele_*.txt"))


def test_write_report_ignores_missing_parent(tmp_path: Path) -> None:
    p._write_report(tmp_path / "missing" / "report.txt", "report")


def test_main_uses_clip_id_as_folder_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    input_path = tmp_path / "eingang"
    input_path.mkdir()
    monkeypatch.setattr(p, "root_path", root)
    monkeypatch.setattr(p, "eingang_path", input_path)
    monkeypatch.setattr(p, "base_dir", tmp_path)
    clip = ("595000", "PROB0616", "T", "G")
    monkeypatch.setattr(p, "clips", (clip,))
    measured: list[Path] = []
    result = {"files": [], "samples": 0, "blocks": 0, "channels": [], "pairs": []}

    def measure(folder: Path) -> dict:
        measured.append(folder)
        return result

    monkeypatch.setattr(p, "measure_clip", measure)
    assert p.main() == 0
    assert measured == [input_path / "595000"]


def test_report_handles_non_comparable_pair_without_metrics() -> None:
    clip = ("595000", "PROB0616", "T", "G")
    result = {
        "files": [],
        "samples": 1,
        "blocks": 0,
        "channels": [],
        "pairs": [{"channels": (1, 2), **p.rate_pair([])}],
    }
    text = p._report_text([(clip, result, None)], datetime(2026, 1, 1), 1, None, False)
    pair_line = next(line for line in text.splitlines() if line.startswith("  1–2 "))
    assert "nicht vergleichbar" in pair_line
    assert pair_line.count("—") == 6
