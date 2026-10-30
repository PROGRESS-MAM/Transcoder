#!/usr/bin/env python3
"""Show what the data streams of an MXF OP-Atom clip are. Read only, writes nothing."""

# --------- IMPORTS ---------
from collections import Counter
from pathlib import Path
import json
import shutil
import subprocess
import sys


# --------- CONFIG ---------
app_name = "Probe Data Streams"
app_version = "1.0.0"

clip_dir = Path(r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit\DEFA\.marathon\DEFA Dokumentation\transcode\eingang\722578")                      # folder with 1x video and 4x audio MXF (OP-Atom)
res_dir = Path(__file__).resolve().parent / "res"         # ffprobe is searched here before PATH
sample_packets = 3                                        # packets per data stream for hexdump and zero check
hex_lines = 4                                             # hexdump lines (16 bytes each) shown per sample packet


# --------- STATIC ---------
format_keys = ("operational_pattern_ul", "material_package_name", "material_package_umid")
data_keys = ("codec_name", "codec_long_name", "codec_tag_string", "time_base", "start_time", "duration", "nb_frames")


# --------- FUNC FFPROBE ---------
def _ffprobe() -> str:
    found = shutil.which("ffprobe", path=str(res_dir)) or shutil.which("ffprobe")
    if not found:
        raise SystemExit(f"ffprobe weder in {res_dir} noch im Systempfad gefunden.")
    return found


def _run(ffprobe: str, args: list[str]) -> str:
    process = subprocess.run([ffprobe, "-v", "error", *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or f"ffprobe endete mit Code {process.returncode}")
    return process.stdout


def probe(ffprobe: str, path: Path) -> dict:
    return json.loads(_run(ffprobe, ["-of", "json", "-show_format", "-show_streams", str(path)]) or "{}")


def packet_totals(ffprobe: str, path: Path) -> dict[int, tuple[int, int]]:
    """Packet count and byte sum per data stream index; reads the whole file once."""
    text = _run(ffprobe, ["-select_streams", "d", "-show_entries", "packet=stream_index,size", "-of", "csv=p=0",
                          str(path)])
    counts, sizes = Counter(), Counter()
    for line in text.splitlines():
        index, _, size = line.partition(",")
        if index.strip().isdigit():
            counts[int(index)] += 1
            sizes[int(index)] += int(size) if size.strip().isdigit() else 0
    return {index: (counts[index], sizes[index]) for index in counts}


def sample_dumps(ffprobe: str, path: Path, index: int) -> list[str]:
    """ffprobe hexdumps of the first packets of one stream."""
    text = _run(ffprobe, ["-of", "json", "-select_streams", str(index), "-read_intervals", f"%+#{sample_packets}",
                          "-show_packets", "-show_data", str(path)])
    return [packet.get("data", "") for packet in json.loads(text or "{}").get("packets", [])[:sample_packets]]


# --------- FUNC REPORT ---------
def _dump_bytes(dump: str) -> bytes:
    # ffprobe line format: "00000000: 0102 0304 ...  ascii"
    return bytes.fromhex("".join(line.partition(":")[2][:41].replace(" ", "") for line in dump.splitlines()))


def counts_like_transcoder(stream: dict) -> bool:
    # Same rule as transcoder/_render.py check_source_encoder
    return stream.get("codec_type") not in ("video", "audio") and stream.get("codec_tag_string") != "tmcd"


def report_data_stream(ffprobe: str, path: Path, stream: dict, totals: tuple[int, int]) -> None:
    index = stream["index"]
    print(f"\n  data-Stream #{index}")
    for key in data_keys:
        if key in stream:
            print(f"    {key:<17} {stream[key]}")
    print(f"    {'disposition':<17} {', '.join(k for k, v in stream.get('disposition', {}).items() if v) or '-'}")
    for key, value in stream.get("tags", {}).items():
        print(f"    tag {key:<13} {value}")
    count, size = totals
    print(f"    {'Pakete':<17} {count} ({size} Bytes)")
    dumps = sample_dumps(ffprobe, path, index) if count else []
    for number, dump in enumerate(dumps, 1):
        lines = dump.strip("\n").splitlines()
        print(f"    Paket {number}:")
        print("\n".join(f"      {line}" for line in lines[:hex_lines]))
        if len(lines) > hex_lines:
            print(f"      ... {len(lines) - hex_lines} weitere Zeilen")
    if not count or not size:
        print("    => leer (keine Pakete oder 0 Bytes)")
    elif dumps and not any(any(_dump_bytes(dump)) for dump in dumps):
        print(f"    => Stichprobe ({len(dumps)} Pakete) enthält nur Nullbytes")


def report_file(ffprobe: str, path: Path) -> tuple[int, int]:
    """Print one file; returns (data streams, streams the transcoder rejects)."""
    print(f"\n{'=' * 100}\n{path.name}")
    data = probe(ffprobe, path)
    form = data.get("format", {})
    print(f"  {'container':<22} {form.get('format_name')}")
    for key in format_keys:
        print(f"  {key:<22} {form.get('tags', {}).get(key, '-')}")
    streams = data.get("streams", [])
    print("\n  Streams:")
    for stream in streams:
        print(f"    #{stream.get('index'):<3} {stream.get('codec_type', '-'):<10} {stream.get('codec_name', '-'):<16}"
              f" {stream.get('codec_tag_string', '-')}")
    data_streams = [stream for stream in streams if stream.get("codec_type") == "data"]
    totals = packet_totals(ffprobe, path) if data_streams else {}
    for stream in data_streams:
        report_data_stream(ffprobe, path, stream, totals.get(stream["index"], (0, 0)))
    return len(data_streams), sum(map(counts_like_transcoder, streams))


# --------- MAIN ---------
def main() -> None:
    print(f"{app_name} {app_version}")
    if not clip_dir.is_dir():
        raise SystemExit(f"Clip-Ordner nicht gefunden: {clip_dir} (clip_dir im CONFIG-Bereich anpassen)")
    files = sorted(path for path in clip_dir.iterdir() if path.is_file() and path.suffix.casefold() == ".mxf")
    if not files:
        raise SystemExit(f"Keine MXF-Dateien in {clip_dir}")
    ffprobe = _ffprobe()
    print(f"ffprobe: {ffprobe}\nClip:    {clip_dir} ({len(files)} MXF)")
    summary = {}
    for path in files:
        try:
            summary[path.name] = report_file(ffprobe, path)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"  Fehler beim Lesen: {exc}")
            summary[path.name] = None
    print(f"\n{'=' * 100}\nZusammenfassung (data-Streams / laut Transcoder-Regel)")
    for name, result in summary.items():
        print(f"  {name:<60} {'Fehler' if result is None else f'{result[0]} / {result[1]}'}")
    valid = [result for result in summary.values() if result]
    print(f"  {'Gesamt':<60} {sum(r[0] for r in valid)} / {sum(r[1] for r in valid)}")


# --------- EXEC ---------
if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    main()
