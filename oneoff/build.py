# --------- IMPORTS ---------
from importlib.util import find_spec
from pathlib import Path
import ast
import os
import sys
import PyInstaller.__main__


# --------- CONFIG ---------
app_name = "One-off Transcode Build"
app_version = "0.1.0"

here = Path(__file__).resolve().parent                  # oneoff/, the exe is written here next to the lists
project = here.parent
script = here / "one-off_transcode.py"
build_folder = project / "build" / "one-off"
tools = ("ffmpeg", "ffprobe")                           # must lie in res/ of the project, the exe has no other ffmpeg
modules = ("transcoder", "toolbox", "numpy")            # must be importable in this Python


# --------- FUNC ---------
def _settings(path: Path, names: tuple[str, ...]) -> dict:
    """Literal values of top level settings; the script is not imported because it writes its log on import."""
    found = {node.targets[0].id: ast.literal_eval(node.value) for node in ast.parse(path.read_text(encoding="utf-8")).body
             if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
             and node.targets[0].id in names}
    if missing := [name for name in names if name not in found]:
        sys.exit(f"{', '.join(missing)} fehlt in {path}.")
    return found


def _version_file(name: str, version: str, exe_name: str) -> Path:
    numbers = (tuple(int(part) for part in version.split(".")) + (0, 0, 0, 0))[:4]
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040704B0', [
          StringStruct('FileDescription', '{name}'), StringStruct('FileVersion', '{version}'),
          StringStruct('ProductName', '{name}'), StringStruct('ProductVersion', '{version}'),
          StringStruct('OriginalFilename', '{exe_name}.exe')])]),
        VarFileInfo([VarStruct('Translation', [1031, 1200])])])
"""
    path = build_folder / f"{exe_name}_version.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _data(source: Path, target: str) -> list[str]:
    return ["--add-data", f"{source}{os.pathsep}{target}"]


# --------- MAIN ---------
def main() -> None:
    if not script.is_file():
        sys.exit(f"{script.name} fehlt in {here}.")
    if missing := [tool for tool in tools if not any((project / "res").glob(f"{tool}*"))]:
        sys.exit(f"{' und '.join(missing)} {'fehlt' if len(missing) == 1 else 'fehlen'} in {project / 'res'}: "
                 f"für die EXE dort ablegen (ffmpeg.exe, ffprobe.exe).")
    sys.path.insert(0, str(project))
    if missing := [name for name in modules if not find_spec(name)]:
        sys.exit(f"{', '.join(missing)} nicht installiert: im Projektordner pip install -e \".[build]\" ausführen.")
    settings = _settings(script, ("app_name", "app_version", "qc_list", "done_lists"))
    lists = [here / name for name in (settings["qc_list"], *settings["done_lists"])]
    if missing := [path.name for path in lists if not path.is_file()]:
        sys.exit(f"Liste fehlt in {here}: {', '.join(missing)}")
    exe_name = f"one-off_transcode_{settings['app_version']}"
    PyInstaller.__main__.run([
        str(script), "--onefile", "--console", "--noconfirm", "--name", exe_name, "--paths", str(project),
        *_data(project / "transcoder" / "presets", "transcoder/presets"), *_data(project / "res", "res"),
        *(option for path in lists for option in _data(path, ".")),
        "--version-file", str(_version_file(settings["app_name"], settings["app_version"], exe_name)),
        "--distpath", str(here), "--workpath", str(build_folder), "--specpath", str(build_folder),
    ])


# --------- EXEC ---------
if __name__ == "__main__":
    main()
