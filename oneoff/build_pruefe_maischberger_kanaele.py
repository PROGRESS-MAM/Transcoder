# --------- IMPORTS ---------
import ast
import os
from pathlib import Path
import sys
import PyInstaller.__main__


# --------- CONFIG ---------
app_name = "Prüfe Maischberger-Kanäle Build"
app_version = "0.1.0"

here = Path(__file__).resolve().parent
project = here.parent
script = here / "pruefe_maischberger_kanaele.py"
dist_folder = here / "dist"
build_folder = project / "build" / "pruefe_maischberger_kanaele"
tools = ("ffmpeg", "ffprobe")


# --------- FUNC ---------
def _settings(path: Path, names: tuple[str, ...]) -> dict:
    """Read literal top-level settings without importing the script."""
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


# --------- MAIN ---------
def main() -> None:
    if not script.is_file():
        sys.exit(f"{script.name} fehlt in {here}.")
    if missing := [tool for tool in tools if not any((project / "res").glob(f"{tool}*"))]:
        sys.exit(f"{' und '.join(missing)} {'fehlt' if len(missing) == 1 else 'fehlen'} in {project / 'res'}: "
                 f"für die EXE dort ablegen (ffmpeg.exe, ffprobe.exe).")
    settings = _settings(script, ("app_name", "app_version"))
    exe_name = f"pruefe_maischberger_kanaele_{settings['app_version']}"
    PyInstaller.__main__.run([
        str(script), "--onefile", "--console", "--noconfirm", "--name", exe_name,
        "--paths", str(here), "--add-data", f"{project / 'res'}{os.pathsep}res",
        "--version-file", str(_version_file(settings["app_name"], settings["app_version"], exe_name)),
        "--distpath", str(dist_folder), "--workpath", str(build_folder), "--specpath", str(build_folder),
    ])
    print(f"Fertig: {dist_folder}\\{exe_name}.exe – der Bericht entsteht beim Lauf neben der EXE.")


# --------- EXEC ---------
if __name__ == "__main__":
    main()
