import os
import sys
import types
from pathlib import Path

calls = []
pyinstaller_stub = types.ModuleType("PyInstaller")
pyinstaller_stub.__path__ = []
pyinstaller_main_stub = types.ModuleType("PyInstaller.__main__")


def record_run(arguments):
    calls.append(arguments)


pyinstaller_main_stub.run = record_run
pyinstaller_stub.__main__ = pyinstaller_main_stub
sys.modules["PyInstaller"] = pyinstaller_stub
sys.modules["PyInstaller.__main__"] = pyinstaller_main_stub

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "oneoff"))
import build_pruefe_maischberger_kanaele as b


def _configure_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    here = project / "oneoff"
    here.mkdir(parents=True)
    script = here / "pruefe_maischberger_kanaele.py"
    script.write_text(
        'app_name = "Prüfe Maischberger-Kanäle"\napp_version = "0.1.0"\n',
        encoding="utf-8",
    )
    resources = project / "res"
    resources.mkdir()
    (resources / "ffmpeg.exe").touch()
    (resources / "ffprobe.exe").touch()
    monkeypatch.setattr(b, "project", project)
    monkeypatch.setattr(b, "here", here)
    monkeypatch.setattr(b, "script", script)
    monkeypatch.setattr(b, "dist_folder", here / "dist")
    monkeypatch.setattr(b, "build_folder", project / "build" / "pruefe_maischberger_kanaele")
    calls.clear()
    return project, here, script


def test_build_succeeds_with_expected_arguments(tmp_path, monkeypatch):
    project, here, script = _configure_project(tmp_path, monkeypatch)

    b.main()

    assert len(calls) == 1
    arguments = calls[0]
    assert str(script) in arguments
    for flag in ("--onefile", "--console", "--noconfirm", "--paths", "--add-data", "--version-file",
                 "--distpath", "--workpath", "--specpath"):
        assert flag in arguments
    assert arguments[arguments.index("--name") + 1] == "pruefe_maischberger_kanaele_0.1.0"
    assert arguments[arguments.index("--paths") + 1] == str(here)
    assert arguments[arguments.index("--add-data") + 1] == f"{project / 'res'}{os.pathsep}res"
    assert arguments[arguments.index("--distpath") + 1] == str(b.dist_folder)
    assert arguments[arguments.index("--workpath") + 1] == str(b.build_folder)
    assert arguments[arguments.index("--specpath") + 1] == str(b.build_folder)
    version_file = Path(arguments[arguments.index("--version-file") + 1])
    assert version_file.is_file()
    assert "0.1.0" in version_file.read_text(encoding="utf-8")


def test_missing_ffprobe_exits_without_running_pyinstaller(tmp_path, monkeypatch):
    project, _, _ = _configure_project(tmp_path, monkeypatch)
    (project / "res" / "ffprobe.exe").unlink()

    try:
        b.main()
    except SystemExit as error:
        assert "ffprobe" in str(error)
    else:
        raise AssertionError("Expected SystemExit when ffprobe is missing")
    assert calls == []


def test_missing_script_exits_with_filename(tmp_path, monkeypatch):
    _, _, script = _configure_project(tmp_path, monkeypatch)
    script.unlink()

    try:
        b.main()
    except SystemExit as error:
        assert script.name in str(error)
    else:
        raise AssertionError("Expected SystemExit when the script is missing")
