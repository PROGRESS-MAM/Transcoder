from pathlib import Path
import subprocess

from oneoff.contracts import AppConfig
from oneoff.tool_resolution import resolve_tools


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        exe_dir=tmp_path,
        source_dir=tmp_path / "source",
        target_dir=tmp_path / "target",
        reports_dir=tmp_path / "reports",
        repair_test=True,
    )


def completed(stdout: str = "ffmpeg version test", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_prefers_bundled_tools_and_checks_versions(tmp_path, monkeypatch):
    res_dir = tmp_path / "res"
    res_dir.mkdir()
    ffmpeg = res_dir / "ffmpeg"
    ffprobe = res_dir / "ffprobe"
    ffmpeg.touch()
    ffprobe.touch()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return completed(stdout=f"{Path(argv[0]).name} version 7")

    monkeypatch.setattr("oneoff.tool_resolution.shutil.which", lambda name: f"/path/{name}")
    monkeypatch.setattr("oneoff.tool_resolution.subprocess.run", run)

    result = resolve_tools(make_config(tmp_path))

    assert result.ok
    assert result.ffmpeg_path == ffmpeg
    assert result.ffprobe_path == ffprobe
    assert result.ffmpeg_version == "ffmpeg version 7"
    assert result.ffprobe_version == "ffprobe version 7"
    assert calls == [[str(ffmpeg), "-version"], [str(ffprobe), "-version"]]


def test_uses_path_when_bundle_is_missing(tmp_path, monkeypatch):
    found = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}
    monkeypatch.setattr("oneoff.tool_resolution.shutil.which", lambda name: found[name])
    monkeypatch.setattr("oneoff.tool_resolution.subprocess.run", lambda *args, **kwargs: completed())

    result = resolve_tools(make_config(tmp_path))

    assert result.ok
    assert result.ffmpeg_path == Path(found["ffmpeg"])
    assert result.ffprobe_path == Path(found["ffprobe"])


def test_falls_back_to_path_when_bundled_tool_cannot_run(tmp_path, monkeypatch):
    res_dir = tmp_path / "res"
    res_dir.mkdir()
    bundled = res_dir / "ffmpeg"
    bundled.touch()
    monkeypatch.setattr(
        "oneoff.tool_resolution.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == str(bundled):
            raise PermissionError("not executable")
        return completed()

    monkeypatch.setattr("oneoff.tool_resolution.subprocess.run", run)

    result = resolve_tools(make_config(tmp_path))

    assert result.ffmpeg_path == Path("/usr/bin/ffmpeg")
    assert result.ok
    assert calls[:2] == [str(bundled), "/usr/bin/ffmpeg"]


def test_missing_tools_return_clear_errors_without_running_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr("oneoff.tool_resolution.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "oneoff.tool_resolution.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected subprocess")),
    )

    result = resolve_tools(make_config(tmp_path))

    assert not result.ok
    assert result.ffmpeg_path is None
    assert result.ffprobe_path is None
    assert len(result.errors) == 2
    assert all("not found" in error and "PATH" in error for error in result.errors)


def test_nonzero_exit_and_timeout_are_reported(tmp_path, monkeypatch):
    res_dir = tmp_path / "res"
    res_dir.mkdir()
    (res_dir / "ffmpeg").touch()
    (res_dir / "ffprobe").touch()
    monkeypatch.setattr("oneoff.tool_resolution.shutil.which", lambda name: None)
    outcomes = iter([
        completed(stdout="bad ffmpeg", returncode=2),
        subprocess.TimeoutExpired(cmd="ffprobe", timeout=10),
    ])

    def run(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("oneoff.tool_resolution.subprocess.run", run)

    result = resolve_tools(make_config(tmp_path))

    assert not result.ok
    assert result.ffmpeg_path is None
    assert result.ffprobe_path is None
    assert any("status 2" in error and "bad ffmpeg" in error for error in result.errors)
    assert any("ffprobe was found but could not be run" in error and "timed out" in error for error in result.errors)
