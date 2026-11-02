# ---------- IMPORTS ----------
import json
import subprocess
import sys
from pathlib import Path

# ---------- CONFIG ----------
app_name = "check_colors"
app_version = "0.1.0"

# Path to the video file of each clip (for OP-Atom: the video MXF)
clips = {
    "DEFA04372 Der Augenzeuge 1949/16 (329617)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Der Augenzeuge\transcode\eingang\329617",
    "DEFA05481 Der Augenzeuge 1966/40 (391361)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Der Augenzeuge\transcode\eingang\391361",
    "DEFA15362_1 Susanne Albrecht (392157)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Videoarchive Cintec\transcode\eingang\392157",
    "DEFA14229 Sabine Wulff (439074)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Spielfilme\transcode\eingang\439074",
    "DEFA02162 Berlin heute (491813)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Dokumentation\transcode\eingang\491813",
    "DEFA06896 Die Streichholzballade (622941)": r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Animation\transcode\eingang\622941",
}

fields = ("codec_name", "color_transfer", "color_primaries", "color_space", "color_range", "chroma_location")
res_dir = Path(__file__).resolve().parent / "res"


# ---------- FUNC ----------
def run(tool: str, args: list[str]) -> str:
    result = subprocess.run([str(res_dir / tool), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{tool} exit code {result.returncode}")
    return result.stdout


def tool_version(tool: str) -> str:
    return run(tool, ["-hide_banner", "-version"]).splitlines()[0]


def video_streams(path: Path) -> list[dict]:
    text = run("ffprobe", ["-v", "error", "-select_streams", "v", "-show_entries",
                           f"stream=index,{','.join(fields)}", "-of", "json", str(path)])
    return json.loads(text).get("streams", [])


def report(name: str, file: str) -> None:
    print(f"\n{name}\n  {file or '(kein Pfad eingetragen)'}")
    if not file:
        return
    path = Path(file)
    if not path.is_file():
        print("  FEHLER: Datei nicht gefunden")
        return
    try:
        streams = video_streams(path)
    except RuntimeError as exc:
        print(f"  FEHLER: {exc}")
        return
    if not streams:
        print("  kein Videostream")
    for stream in streams:
        print(f"  Stream {stream.get('index')}: " + ", ".join(f"{key}={stream.get(key, '-')}" for key in fields))


# ---------- MAIN ----------
def main() -> int:
    print(f"{app_name} {app_version}")
    try:
        print(tool_version("ffprobe"))
        print(tool_version("ffmpeg"))
    except (OSError, RuntimeError) as exc:
        print(f"FEHLER: ffprobe/ffmpeg in {res_dir} nicht nutzbar: {exc}")
        return 1
    for name, file in clips.items():
        report(name, file)
    return 0


# ---------- EXEC ----------
if __name__ == "__main__":
    sys.exit(main())
