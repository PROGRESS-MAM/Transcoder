import importlib.util
import os
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def build_context(tmp_path, monkeypatch):
    calls = []
    pyinstaller = types.ModuleType("PyInstaller")
    pyinstaller_main = types.ModuleType("PyInstaller.__main__")
    pyinstaller_main.run = lambda arguments: calls.append(arguments)
    pyinstaller.__main__ = pyinstaller_main
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", pyinstaller_main)

    module_path = REPO_ROOT / "oneoff" / "build_repariere_balken.py"
    spec = importlib.util.spec_from_file_location("build_repariere_balken_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    project = tmp_path
    here = project / "oneoff"
    here.mkdir()
    script = here / "repariere_balken.py"
    script.write_text('app_name = "Repariere Balken"\napp_version = "0.1.0"\n', encoding="utf-8")
    helper = here / "pruefe_balken.py"
    helper.write_text("", encoding="utf-8")
    res = project / "res"
    res.mkdir()
    (res / "ffmpeg.exe").touch()
    (res / "ffprobe.exe").touch()
    dist_folder = here / "dist"
    build_folder = project / "build" / "repariere_balken"

    for name, value in {
        "project": project,
        "here": here,
        "script": script,
        "helper": helper,
        "dist_folder": dist_folder,
        "build_folder": build_folder,
    }.items():
        monkeypatch.setattr(module, name, value)

    return SimpleNamespace(
        build=module,
        calls=calls,
        project=project,
        here=here,
        script=script,
        helper=helper,
        dist_folder=dist_folder,
        build_folder=build_folder,
    )


def test_build_succeeds_with_expected_arguments(build_context):
    context = build_context
    exe_name = "repariere_balken_0.1.0"
    version_file = context.build_folder / f"{exe_name}_version.txt"
    assert context.build.main() is None
    assert context.calls == [[
        str(context.script), "--onefile", "--console", "--noconfirm", "--name", exe_name,
        "--paths", str(context.here), "--add-data", f"{context.project / 'res'}{os.pathsep}res",
        "--version-file", str(version_file), "--distpath", str(context.dist_folder),
        "--workpath", str(context.build_folder), "--specpath", str(context.build_folder),
    ]]
    version_text = version_file.read_text(encoding="utf-8")
    assert "filevers=(0, 1, 0, 0)" in version_text
    assert "prodvers=(0, 1, 0, 0)" in version_text
    assert "StringStruct('FileVersion', '0.1.0')" in version_text


def test_missing_ffprobe_exits_without_running_pyinstaller(build_context):
    context = build_context
    (context.project / "res" / "ffprobe.exe").unlink()
    with pytest.raises(SystemExit, match="ffprobe"):
        context.build.main()
    assert context.calls == []


def test_missing_helper_exits_with_filename(build_context):
    context = build_context
    context.helper.unlink()
    with pytest.raises(SystemExit) as error:
        context.build.main()
    assert "pruefe_balken.py" in str(error.value)
    assert context.calls == []
