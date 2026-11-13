import importlib.util
import os
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CONTENT = r"""[paths]
; Ordner mit den MP4-Dateien, nicht rekursiv
pruef_path = \\server\freigabe\ordner
""" + "\n"


@pytest.fixture
def build_context(tmp_path, monkeypatch):
    calls = []
    pyinstaller = types.ModuleType("PyInstaller")
    pyinstaller_main = types.ModuleType("PyInstaller.__main__")
    pyinstaller_main.run = lambda arguments: calls.append(arguments)
    pyinstaller.__main__ = pyinstaller_main
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", pyinstaller_main)

    module_path = REPO_ROOT / "oneoff" / "build_pruefe_balken.py"
    spec = importlib.util.spec_from_file_location("build_pruefe_balken_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    project = tmp_path
    here = project / "oneoff"
    here.mkdir()
    script = here / "pruefe_balken.py"
    script.write_text('app_name = "Prüfe Balken"\napp_version = "0.1.0"\n', encoding="utf-8")
    config_file = here / "pruefe_balken.ini"
    config_file.write_text(CONFIG_CONTENT, encoding="utf-8", newline="\n")
    res = project / "res"
    res.mkdir()
    (res / "ffmpeg.exe").touch()
    (res / "ffprobe.exe").touch()
    dist_folder = here / "dist"
    build_folder = project / "build" / "pruefe_balken"

    for name, value in {
        "project": project,
        "here": here,
        "script": script,
        "config_file": config_file,
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
        config_file=config_file,
        dist_folder=dist_folder,
        build_folder=build_folder,
        config_content=CONFIG_CONTENT,
    )


def test_build_succeeds_with_expected_arguments(build_context):
    context = build_context
    exe_name = "pruefe_balken_0.1.0"
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
    target_config = context.dist_folder / "pruefe_balken.ini"
    assert target_config.read_text(encoding="utf-8") == context.config_content


def test_existing_config_is_kept(build_context):
    context = build_context
    context.dist_folder.mkdir(parents=True)
    target_config = context.dist_folder / "pruefe_balken.ini"
    target_config.write_text("x", encoding="utf-8")
    context.build.main()
    assert target_config.read_text(encoding="utf-8") == "x"


def test_missing_ffprobe_exits_without_running_pyinstaller(build_context):
    context = build_context
    (context.project / "res" / "ffprobe.exe").unlink()
    with pytest.raises(SystemExit, match="ffprobe"):
        context.build.main()
    assert context.calls == []


def test_missing_config_exits_with_filename(build_context):
    context = build_context
    context.config_file.unlink()
    with pytest.raises(SystemExit) as error:
        context.build.main()
    assert "pruefe_balken.ini" in str(error.value)
    assert context.calls == []
