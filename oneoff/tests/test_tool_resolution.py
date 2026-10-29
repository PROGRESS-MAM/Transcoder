import pytest

from oneoff.analysis import probe


@pytest.mark.parametrize("tool_name", ["ffmpeg", "ffprobe"])
def test_bundled_tool_is_preferred_over_path(tmp_path, monkeypatch, tool_name):
    bundled = tmp_path / "res" / f"{tool_name}.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"local tool")
    monkeypatch.setattr(probe.shutil, "which", lambda name: f"/path/{name}")

    assert probe._resolve_tool(tool_name, tmp_path) == str(bundled.resolve())


@pytest.mark.parametrize("tool_name", ["ffmpeg", "ffprobe"])
def test_tool_resolution_falls_back_to_path(tmp_path, monkeypatch, tool_name):
    path_tool = f"/path/{tool_name}"
    monkeypatch.setattr(probe.shutil, "which", lambda name: path_tool)

    assert probe._resolve_tool(tool_name, tmp_path) == path_tool


def test_missing_tool_has_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.shutil, "which", lambda name: None)

    with pytest.raises(probe.ProbeError, match="ffmpeg.*not found in res/ or on PATH"):
        probe._resolve_tool("ffmpeg", tmp_path)
