"""One-off analysis of channel pairs in Maischberger clips."""

# --------- IMPORTS ---------
from datetime import datetime
import itertools
from pathlib import Path
from statistics import median
import json
import math
import os
import shutil
import subprocess
import sys

import numpy as np

from maischberger_clips import clips


# --------- CONFIG ---------
app_name = "Prüfe Maischberger-Kanäle"
app_version = "0.1.0"
root_path = Path(r"\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit")
eingang_path = root_path / "LYNXarchive" / ".marathon" / "transcode" / "eingang"
media_suffixes = (".mov", ".mxf", ".mp4", ".wav")
sample_count = 20
sample_seconds = 10.0
probe_timeout = 60
decode_timeout = 180
report_prefix = "maischberger_kanaele"
base_dir = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
bundle_dir = Path(getattr(sys, "_MEIPASS", base_dir))
_tool_cache: dict[str, str] = {}
compare_rate = 8000
silence_db = -60.0
near_lag_ms = 40
wide_lag_ms = 250
duplicate_min = 0.95
same_lag_ms = 1.0


# --------- STATIC ---------
verdicts = ("identisch", "identisch, versetzt", "verpolt", "verpolt, versetzt", "verschieden", "nicht vergleichbar")


# --------- FUNC MEASURE ---------
def channel_levels(block: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return RMS and peak levels in dBFS for each channel."""
    values = block.astype(np.float64, copy=False)
    rms = np.sqrt(np.mean(np.square(values), axis=0, dtype=np.float64))
    peak = np.max(np.abs(values), axis=0)
    floor = 10 ** (-120.0 / 20.0)
    rms_db = np.maximum(20.0 * np.log10(np.maximum(rms, floor)), -120.0).astype(np.float64)
    peak_db = np.maximum(20.0 * np.log10(np.maximum(peak, floor)), -120.0).astype(np.float64)
    return rms_db, peak_db


def pair_values(a: np.ndarray, b: np.ndarray, rate: int) -> dict[str, float]:
    """Measure signed correlation, lag, and level difference for one block."""
    factor = max(1, rate // compare_rate)

    def reduce(values: np.ndarray) -> np.ndarray:
        cut = len(values) // factor * factor
        reduced = values[:cut].astype(np.float64, copy=False).reshape(-1, factor).mean(axis=1)
        return reduced - reduced.mean()

    original_a = a.astype(np.float64, copy=False)
    original_b = b.astype(np.float64, copy=False)
    x, y = reduce(original_a), reduce(original_b)
    near = round(near_lag_ms / 1000 * rate / factor)
    wide = round(wide_lag_ms / 1000 * rate / factor)
    size = 1 << (len(x) + wide).bit_length()
    correlation = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(y, size)), size)
    norms = (math.sqrt(float(np.sum(x * x))) + 1e-12) * (math.sqrt(float(np.sum(y * y))) + 1e-12)
    correlation /= norms
    lags = np.concatenate([correlation[size - wide:], correlation[:wide + 1]])

    near_start = wide - near
    near_index = int(np.argmax(lags[near_start:wide + near + 1]))
    peak_index = int(np.argmax(np.abs(lags)))
    near_lag = near_index - near
    peak_lag = peak_index - wide
    to_ms = factor / rate * 1000.0
    rms_a = math.sqrt(float(np.mean(original_a * original_a)))
    rms_b = math.sqrt(float(np.mean(original_b * original_b)))
    return {
        "r0": float(lags[wide]),
        "near_max": float(lags[near_start + near_index]),
        "near_lag_ms": float(near_lag * to_ms),
        "peak": float(lags[peak_index]),
        "peak_lag_ms": float(peak_lag * to_ms),
        "level_db": float(20.0 * math.log10((rms_b + 1e-12) / (rms_a + 1e-12))),
    }


def rate_pair(values: list[dict]) -> dict:
    """Summarize block measurements and classify the channel pair."""
    if not values:
        return {
            "blocks": 0,
            "r0": None,
            "near_max": None,
            "peak": None,
            "near_lag_ms": None,
            "peak_lag_ms": None,
            "level_db": None,
            "inverted_share": 0.0,
            "verdict": "nicht vergleichbar",
        }

    medians = {key: median(value[key] for value in values)
               for key in ("r0", "near_max", "peak", "near_lag_ms", "peak_lag_ms", "level_db")}
    peak = medians["peak"]
    lag = medians["peak_lag_ms"]
    if peak >= duplicate_min:
        verdict = "identisch, versetzt" if abs(lag) > same_lag_ms else "identisch"
    elif peak <= -duplicate_min:
        verdict = "verpolt, versetzt" if abs(lag) > same_lag_ms else "verpolt"
    else:
        verdict = "verschieden"

    return {
        "blocks": len(values),
        "r0": round(medians["r0"], 3),
        "near_max": round(medians["near_max"], 3),
        "peak": round(peak, 3),
        "near_lag_ms": round(medians["near_lag_ms"], 1),
        "peak_lag_ms": round(lag, 1),
        "level_db": round(medians["level_db"], 1),
        "inverted_share": round(sum(value["peak"] <= -duplicate_min for value in values) / len(values), 3),
        "verdict": verdict,
    }


# --------- FUNC TOOLS ---------
def _find_tool(name: str) -> str:
    """Resolve a media tool lazily from the app bundle or PATH."""
    cached = _tool_cache.get(name)
    if cached:
        return cached

    for folder in (base_dir / "res", bundle_dir / "res"):
        candidate = folder / name
        if candidate.is_file():
            _tool_cache[name] = str(candidate)
            return _tool_cache[name]
        if os.name == "nt":
            windows_candidate = candidate.with_suffix(".exe")
            if windows_candidate.is_file():
                _tool_cache[name] = str(windows_candidate)
                return _tool_cache[name]

    found = shutil.which(name)
    if found:
        _tool_cache[name] = found
        return found
    raise RuntimeError(f"{name} nicht gefunden. Lege das Programm in res ab oder installiere es im PATH.")


def _run(command: list[str], timeout: float) -> bytes:
    """Run a bounded subprocess and return its standard output."""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Zeitlimit {timeout:g} s überschritten: {Path(command[0]).stem}") from error

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        stderr_lines = [line for line in stderr.splitlines() if line.strip()]
        detail = " | ".join(stderr_lines[-5:]) or "keine stderr-Ausgabe"
        raise RuntimeError(f"{Path(command[0]).stem} fehlgeschlagen (Exit-Code {result.returncode}): {detail}")
    return result.stdout


# --------- FUNC SOURCE ---------
def _media_files(folder: Path) -> list[Path]:
    """Return supported media files in stable case-insensitive order."""
    if not folder.is_dir():
        raise FileNotFoundError(f"Ordner fehlt: {folder}")
    files = sorted(
        (path for path in folder.rglob("*") if path.is_file() and path.suffix.casefold() in media_suffixes),
        key=lambda path: str(path).casefold(),
    )
    if not files:
        raise FileNotFoundError(f"keine Mediendatei in {folder}")
    return files


def _probe(path: Path) -> dict:
    """Read media stream metadata with ffprobe."""
    output = _run(
        [_find_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        probe_timeout,
    )
    return json.loads(output or b"{}")


def _layout(files: list[Path]) -> tuple[list[tuple[Path, int, int]], list[str], int, float]:
    """Describe audio streams and channels in stable source order."""
    layout: list[tuple[Path, int, int]] = []
    where: list[str] = []
    rate = 48000
    has_rate = False
    duration = 0.0

    for path in files:
        metadata = _probe(path)
        streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"]
        if streams:
            channels = 0
            for ordinal, stream in enumerate(streams):
                stream_channels = int(stream.get("channels") or 0)
                channels += stream_channels
                where.extend(f"{path.name} a:{ordinal} k{index + 1}" for index in range(stream_channels))
                if not has_rate:
                    rate = int(stream.get("sample_rate") or 48000)
                    has_rate = True
            layout.append((path, len(streams), channels))
        raw_duration = metadata.get("format", {}).get("duration")
        if raw_duration:
            duration = max(duration, float(raw_duration))

    if not where:
        raise ValueError("Source hat keinen Ton.")
    return layout, where, rate, duration


def _spans(duration: float) -> list[tuple[float, float]]:
    """Return evenly distributed sample windows across the source duration."""
    length = min(sample_seconds, duration)
    if duration <= 0:
        return [(0.0, sample_seconds)]
    return [
        (min(max(0.0, (index + 0.5) * duration / sample_count - length / 2), duration - length), length)
        for index in range(sample_count)
    ]


def _decode(path: Path, streams: int, width: int, start: float, length: float, rate: int) -> np.ndarray:
    """Decode selected audio streams to interleaved float32 channel frames."""
    graph = "".join(f"[0:a:{index}]" for index in range(streams))
    if streams > 1:
        graph += f"amerge=inputs={streams},"
    graph += f"aresample={rate},aformat=sample_fmts=flt[a]"
    output = _run(
        [
            _find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
            "-filter_complex", graph, "-map", "[a]", "-vn", "-c:a", "pcm_f32le",
            "-f", "f32le", "pipe:1",
        ],
        decode_timeout,
    )
    values = np.frombuffer(output, dtype=np.float32)
    values = values[:len(values) // width * width]
    return values.reshape(-1, width)


def _block(layout: list[tuple[Path, int, int]], start: float, length: float, rate: int) -> np.ndarray | None:
    """Decode and align every source file into one channel block."""
    decoded = [_decode(path, streams, width, start, length, rate) for path, streams, width in layout]
    frames = min((len(values) for values in decoded), default=0)
    if frames == 0:
        return None
    return np.hstack([values[:frames] for values in decoded])


# --------- FUNC CLIP ---------
def measure_clip(folder: Path) -> dict:
    """Measure channel levels and pair relationships for one clip folder."""
    files = _media_files(folder)
    layout, where, rate, duration = _layout(files)
    spans = _spans(duration)
    channel_count = len(where)
    square_sums = np.zeros(channel_count, dtype=np.float64)
    peaks = np.zeros(channel_count, dtype=np.float64)
    pair_indices = list(itertools.combinations(range(channel_count), 2))
    pair_measurements = {pair: [] for pair in pair_indices}
    frame_count = 0
    blocks = 0

    for start, length in spans:
        block = _block(layout, start, length, rate)
        if block is None:
            continue
        values = block.astype(np.float64, copy=False)
        square_sums += np.sum(values * values, axis=0)
        peaks = np.maximum(peaks, np.max(np.abs(values), axis=0))
        frame_count += len(values)
        blocks += 1
        block_rms_db, _ = channel_levels(block)
        for first, second in pair_indices:
            if block_rms_db[first] >= silence_db and block_rms_db[second] >= silence_db:
                pair_measurements[(first, second)].append(pair_values(block[:, first], block[:, second], rate))

    floor = 10 ** (-120.0 / 20.0)
    rms = np.sqrt(square_sums / max(frame_count, 1))
    rms_db = np.maximum(20.0 * np.log10(np.maximum(rms, floor)), -120.0)
    peak_db = np.maximum(20.0 * np.log10(np.maximum(peaks, floor)), -120.0)
    channels = [
        {"channel": index + 1, "where": where[index], "rms_db": round(float(rms_db[index]), 1), "peak_db": round(float(peak_db[index]), 1)}
        for index in range(channel_count)
    ]
    pairs = [
        {"channels": (first + 1, second + 1), **rate_pair(pair_measurements[(first, second)])}
        for first, second in pair_indices
    ]
    return {
        "files": [path.name for path, _, _ in layout],
        "samples": len(spans),
        "blocks": blocks,
        "channels": channels,
        "pairs": pairs,
    }


# --------- FUNC REPORT ---------
def _report_text(
    results: list[tuple[tuple[str, str, str, str], dict | None, str | None]],
    started: datetime,
    total: int,
    finished: datetime | None,
    aborted: bool,
) -> str:
    """Build the complete plain-text progress or final report."""
    end_text = "ABGEBROCHEN" if aborted else (finished.strftime("%Y-%m-%d %H:%M:%S") if finished else "läuft")
    lines = [
        f"{app_name} {app_version}",
        f"Start: {started:%Y-%m-%d %H:%M:%S}",
        f"Ende: {end_text}",
        f"Clips {len(results)}/{total}",
        (
            f"Einstellungen: sample_count={sample_count}, sample_seconds={sample_seconds:g}, "
            f"compare_rate={compare_rate}, near_lag_ms={near_lag_ms}, wide_lag_ms={wide_lag_ms}, "
            f"duplicate_min={duplicate_min}, silence_db={silence_db:g}"
        ),
        "",
        "Zusammenfassung:",
    ]
    verdict_counts: dict[tuple[int, int], dict[str, int]] = {}
    for _, result, _ in results:
        if result is None:
            continue
        for pair in result.get("pairs", []):
            key = tuple(pair["channels"])
            counts = verdict_counts.setdefault(key, {})
            verdict = pair["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
    for pair in sorted(verdict_counts):
        counts = verdict_counts[pair]
        summary = ", ".join(f"{verdict} {counts[verdict]}" for verdict in verdicts if counts.get(verdict, 0) > 0)
        if summary:
            lines.append(f"  {pair[0]}–{pair[1]}: {summary}")
    errors = sum(error is not None for _, _, error in results)
    lines.extend((f"Fehler: {errors}", "", "Clips:"))

    def metric(value: float | None, digits: int = 3, signed: bool = False) -> str:
        if value is None:
            return "—"
        return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"

    for clip, result, error in results:
        clip_id, identifier, title, reason = clip
        lines.extend((f"{clip_id} · {identifier} {title}", f"  Bericht: {reason}"))
        if result is None:
            lines.append("  Dateien: —")
        else:
            lines.append(f"  Dateien: {', '.join(result.get('files', [])) or '—'}")
            for channel in result.get("channels", []):
                lines.append(
                    f"  K{channel['channel']} {channel['where']:<28} RMS {channel['rms_db']:6.1f} dBFS  "
                    f"Peak {channel['peak_db']:6.1f} dBFS"
                )
            for pair in result.get("pairs", []):
                first, second = pair["channels"]
                lines.append(
                    f"  {first}–{second} Blöcke {pair['blocks']}, r0 {metric(pair['r0'])}, "
                    f"±40 ms {metric(pair['near_max'])} ({metric(pair['near_lag_ms'], 1, True)} ms), "
                    f"stärkste {metric(pair['peak'])} ({metric(pair['peak_lag_ms'], 1, True)} ms), "
                    f"Pegel {metric(pair['level_db'], 1, True)} dB, "
                    f"verpolt {pair['inverted_share']:.1%} → {pair['verdict']}"
                )
        if error is not None:
            lines.append(f"  Fehler: {error}")
    return "\n".join(lines) + "\n"


def _write_report(path: Path, text: str) -> None:
    """Replace a report atomically; report failures never stop analysis."""
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        print(f"Warnung: Bericht konnte nicht geschrieben werden: {error}", file=sys.stderr)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# --------- MAIN ---------
def main() -> int:
    """Run the clip analysis and keep a complete progress report."""
    if not root_path.is_dir():
        print(f"Quellordner fehlt: {root_path}. Prüfe die Netzwerkverbindung.", file=sys.stderr)
        return 1

    started = datetime.now()
    report_path = base_dir / f"{report_prefix}_{started:%Y%m%d_%H%M%S}.txt"
    results: list[tuple[tuple[str, str, str, str], dict | None, str | None]] = []
    total = len(clips)
    aborted = False

    for index, clip in enumerate(clips, 1):
        clip_id, identifier, _, _ = clip
        try:
            print(f"[{index}/{total}] {identifier} …", flush=True)
            try:
                result = measure_clip(eingang_path / clip_id)
                results.append((clip, result, None))
                findings = ", ".join(
                    f"{pair['channels'][0]}–{pair['channels'][1]} {pair['verdict']}"
                    for pair in result.get("pairs", [])
                ) or "Keine Kanalpaare"
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                results.append((clip, None, str(error)))
                findings = f"Fehler: {error}"
            print(findings, flush=True)
            _write_report(report_path, _report_text(results, started, total, None, False))
        except KeyboardInterrupt:
            aborted = True
            break

    finished = datetime.now()
    final_text = _report_text(results, started, total, finished, aborted)
    try:
        _write_report(report_path, final_text)
    except KeyboardInterrupt:
        aborted = True
        try:
            _write_report(report_path, _report_text(results, started, total, datetime.now(), True))
        except KeyboardInterrupt:
            pass
    print(f"Bericht: {report_path}")
    return 0


# --------- EXEC ---------
if __name__ == "__main__":
    code = main()
    if getattr(sys, "frozen", False):
        try:
            input("Enter zum Beenden …")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
