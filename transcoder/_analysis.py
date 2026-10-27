"""Settings, analysis checks and fix rules of the transcoder."""
# --------- IMPORTS ---------
from collections.abc import Callable, Iterable, Iterator
from itertools import combinations, zip_longest
from statistics import median
import copy
import json
import math
import re
import numpy as np
from . import _media
from ._source import Source


# --------- CONFIG ---------
defaults = {
    "samples": {"per_minute": 6, "seconds": 2, "full": False},
    "interlace": {"source": "analyse", "trust": 0.9},
    "black_bars": {"extra_crop": 0, "trust": 0.9},
    "audio": {"silence_threshold": "-60dB", "silence_min_active": "1%", "silence_window": "1s",
              "duplicate_min": 0.95, "max_offset_ms": 40, "trust": 0.9},
}
bars_fps = 4                    # frames per second read from a sample for black_bars
bars_full_fps = 1               # the same with samples.full
black_tolerance = 6.0           # 8-bit steps above the black level that still count as black (noise, grain)
full_block_seconds = 10         # audio with samples.full is compared in blocks of this length
compare_rate = 8000             # audio is compared at about this sample rate
sample_bytes = 4                # audio is decoded as 32 bit float


# --------- STATIC ---------
checks = ("streams", "interlace", "black_bars", "audio")
interlace_sources = ("analyse", "metadata")
idet_pattern = re.compile(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)"
                          r"\s*Undetermined:\s*(\d+)")
op_atom_marker = "0d010201.10"  # OP-Atom in the operational pattern UL
Progress = Callable[[str, float], None]
FrameLines = tuple[np.ndarray, np.ndarray]
FieldCounts = tuple[int, int, int, int]


class FixError(Exception):
    """A fix cannot change the output safely; the text goes into the error report."""


# --------- FUNC SETTINGS ---------
def _merged(base: dict, values: dict | None, name: str) -> dict:
    if values is None:
        return copy.deepcopy(base)
    if not isinstance(values, dict):
        raise ValueError(f"Einstellungen {name}: erwartet ein Dict, erhalten {values!r}.")
    if unknown := sorted(set(values) - set(base)):
        raise ValueError(f"Einstellungen {name}: unbekannte Einträge {', '.join(unknown)}; "
                         f"erlaubt: {', '.join(base)}.")
    result = copy.deepcopy(base)
    for key, value in values.items():
        result[key] = _merged(base[key], value, f"{name}.{key}") if isinstance(base[key], dict) else value
    return result


def section(check: str, values: dict | None = None, samples: dict | None = None) -> dict:
    """Complete settings of one check: defaults, then samples, then the section values (may hold own samples)."""
    if check not in checks:
        raise ValueError(f"Unbekannte Prüfung {check!r}; erlaubt: {', '.join(checks)}.")
    base = {**defaults.get(check, {}), "samples": _merged(defaults["samples"], samples, "samples")}
    result = _merged(base, values, check)
    _validate(check, result)
    return result


def split_settings(settings: dict | None) -> dict[str, dict]:
    """Whole settings dict of analyse() as complete section per check."""
    settings = settings or {}
    if not isinstance(settings, dict):
        raise ValueError(f"settings: erwartet ein Dict, erhalten {settings!r}.")
    if unknown := sorted(set(settings) - set(defaults)):
        raise ValueError(f"settings: unbekannte Abschnitte {', '.join(unknown)}; erlaubt: {', '.join(defaults)}.")
    return {check: section(check, settings.get(check), settings.get("samples")) for check in checks}


def _validate(check: str, values: dict) -> None:
    samples = values["samples"]
    if not isinstance(samples["full"], bool):
        raise ValueError("samples.full muss True oder False sein.")
    for key in ("per_minute", "seconds"):
        if not isinstance(samples[key], (int, float)) or isinstance(samples[key], bool) or samples[key] <= 0:
            raise ValueError(f"samples.{key} muss eine Zahl größer 0 sein, erhalten {samples[key]!r}.")
    if "trust" in values and not (isinstance(values["trust"], (int, float)) and 0 <= values["trust"] <= 1):
        raise ValueError(f"{check}.trust muss zwischen 0 und 1 liegen, erhalten {values['trust']!r}.")
    if check == "interlace" and values["source"] not in interlace_sources:
        raise ValueError(f"interlace.source muss {' oder '.join(interlace_sources)} sein.")
    if check == "black_bars" and (not isinstance(values["extra_crop"], int) or values["extra_crop"] < 0):
        raise ValueError(f"black_bars.extra_crop muss eine ganze Zahl ab 0 sein, erhalten {values['extra_crop']!r}.")
    if check == "audio":
        _media.decibel(values["silence_threshold"], "audio.silence_threshold")
        _media.percent(values["silence_min_active"], "audio.silence_min_active")
        if _media.seconds(values["silence_window"], "audio.silence_window") <= 0:
            raise ValueError("audio.silence_window muss größer 0 sein.")
        if not 0 < values["duplicate_min"] <= 1 or values["max_offset_ms"] < 0:
            raise ValueError("audio.duplicate_min muss in (0, 1] liegen, audio.max_offset_ms ab 0.")


def key(check: str, values: dict) -> tuple[str, str]:
    return check, json.dumps(values, sort_keys=True)


# --------- FUNC STATISTICS ---------
def _binomial_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n or p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * log_p + (n - j) * log_q
             for j in range(k + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(term - top) for term in terms))


def share_above(hits: int, total: int, limit: float) -> float:
    """Probability that the true share is at least limit, after hits of total (uniform prior, Beta posterior)."""
    # For Beta(k+1, n-k+1): P(share <= x) = P(Binomial(n+1, x) >= k+1).
    return _binomial_cdf(hits, total + 1, limit)


def _rated(hits: int, total: int, limit: float, full: bool) -> tuple[bool, float]:
    """(share >= limit, trust of that rating); exact with full=True."""
    above = total > 0 and hits / total >= limit
    if full:
        return above, 1.0
    chance = share_above(hits, total, limit)
    return above, round(chance if above else 1 - chance, 4)


# --------- FUNC SAMPLES ---------
def spans(duration: float, samples: dict) -> list[tuple[float, float]]:
    """(start, length) of all samples; one span over the full length with samples.full."""
    if samples["full"] or duration <= 0:
        return [(0.0, duration)]
    length = min(float(samples["seconds"]), duration)
    count = max(1, round(duration / 60 * samples["per_minute"]))
    return [(min(max(0.0, (index + 0.5) * duration / count - length / 2), duration - length), length)
            for index in range(count)]


def _seek(start: float, length: float, full: bool) -> list[str]:
    return [] if full else ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]


# --------- FUNC STREAMS ---------
def _stream_text(data: dict) -> dict:
    keep = ("codec_name", "profile", "width", "height", "sample_aspect_ratio", "display_aspect_ratio", "avg_frame_rate",
            "field_order", "pix_fmt", "bit_rate", "channels", "channel_layout", "sample_rate", "sample_fmt",
            "bits_per_raw_sample", "duration")
    return {name: data[name] for name in keep if name in data}


def check_streams(source: Source, values: dict, progress: Progress) -> dict:
    files = []
    for path, data in zip(source.files, source.probes):
        tags = data.get("format", {}).get("tags", {})
        pattern = tags.get("operational_pattern_ul")
        files.append({
            "file": str(path), "container": data.get("format", {}).get("format_name"),
            "duration": _media.number(data.get("format", {}).get("duration")),
            "bit_rate": _media.number(data.get("format", {}).get("bit_rate")),
            "operational_pattern": pattern, "op_atom": bool(pattern and op_atom_marker in pattern.casefold()),
            "material_package_umid": tags.get("material_package_umid"),
            "material_package_name": tags.get("material_package_name"),
            "encoder": tags.get("encoder"), "x264": _media.x264_options(path),
            "streams": [{"type": stream.get("codec_type"), **_stream_text(stream)} for stream in data.get("streams", [])],
        })
    return {
        "files": files, "duration": source.duration, "start_tc": source.start_tc, "tc_origin": source.tc_origin,
        "video": {**_stream_text(source.video.data), "fps": str(source.fps)} if source.video else None,
        "field_order": source.field_order,
        "audio": [_stream_text(ref.data) for ref in source.audio], "channels": len(source.channels),
    }


# --------- FUNC INTERLACE ---------
def _idet(source: Source, start: float, length: float, full: bool) -> tuple[int, int, int, int]:
    ref = source.video
    _, errors = _media.run_text("ffmpeg", ["-nostdin", "-nostats", *_seek(start, length, full), "-i",
                                           str(source.files[ref.file]), "-map", f"0:v:{ref.ordinal}", "-vf", "idet",
                                           "-an", "-f", "null", "-"])
    found = idet_pattern.findall(errors)
    return tuple(map(int, found[-1])) if found else (0, 0, 0, 0)


def _interlaced(tff: int, bff: int, prog: int) -> bool:
    """Interlaced only with one clear field order; both orders at once is detector noise on progressive pictures."""
    field = max(tff, bff)
    return field > prog and field >= 2 * min(tff, bff)


def _rate_interlace(source: Source, values: dict, measurements: Iterable[FieldCounts]) -> dict:
    metadata = source.field_order
    meta_rating = {"progressive": "progressive", None: None}.get(metadata, "interlaced")
    report = {"metadata": metadata, "metadata_rating": meta_rating}
    if values["source"] == "metadata":
        return {**report, "rating": meta_rating or "unclear", "trust": 1.0 if meta_rating else 0.0}
    full = values["samples"]["full"]
    totals, votes, voters = [0, 0, 0, 0], 0, 0
    for tff, bff, prog, undetermined in measurements:
        totals = [a + b for a, b in zip(totals, (tff, bff, prog, undetermined))]
        if tff + bff + prog:
            voters += 1
            votes += _interlaced(tff, bff, prog)
    if full:
        votes, voters = (sum(totals[:3]), sum(totals[:3])) if _interlaced(*totals[:3]) else (0, sum(totals[:3]))
    interlaced, trust = _rated(votes, voters, 0.5, False) if voters else (False, 0.0)
    rating = "unclear" if not voters or votes * 2 == voters else "interlaced" if interlaced else "progressive"
    order = "tff" if totals[0] >= totals[1] else "bff"
    if rating == "interlaced" and max(totals[:2]) < 2 * min(totals[:2]):
        rating, trust = "unclear", 0.0      # samples disagree on the field order
    return {**report, "rating": rating, "trust": trust if voters else 0.0,
            "field_order": order if rating == "interlaced" else None,
            "frames": dict(zip(("tff", "bff", "progressive", "undetermined"), totals)), "votes": votes,
            "voters": voters, "agrees": meta_rating == rating if meta_rating and rating != "unclear" else None}


def check_interlace(source: Source, values: dict, progress: Progress) -> dict:
    """Rate the scan mode from metadata or sampled idet measurements."""
    if not source.video:
        raise ValueError("Source hat kein Video.")
    if values["source"] == "metadata":
        return _rate_interlace(source, values, [])
    parts = spans(source.duration, values["samples"])
    measurements = []
    for number, (start, length) in enumerate(parts, 1):
        progress(f"interlace: Stichprobe {number}/{len(parts)}", number / len(parts))
        measurements.append(_idet(source, start, length, values["samples"]["full"]))
    return _rate_interlace(source, values, measurements)

# --------- FUNC BLACK BARS ---------
def _gray_lines(chunk: bytes, width: int, height: int) -> FrameLines:
    frame = np.frombuffer(chunk, np.uint8).reshape(height, width)
    return frame.mean(axis=1, dtype=np.float32), frame.mean(axis=0, dtype=np.float32)


def _frames_lines(source: Source, start: float, length: float, full: bool) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    ref, width, height = source.video, source.width, source.height
    args = [*_seek(start, length, full), "-i", str(source.files[ref.file]), "-map", f"0:v:{ref.ordinal}", "-an",
            "-vf", f"fps={bars_full_fps if full else bars_fps},format=gray", "-f", "rawvideo", "pipe:1"]
    for chunk in _media.read_chunks(args, width * height):
        if len(chunk) == width * height:
            yield _gray_lines(chunk, width, height)


def _edge(lines: np.ndarray, limit: float) -> int:
    """Number of lines from the start of lines that stay at or below limit."""
    above = np.flatnonzero(lines > limit)
    return int(above[0]) if above.size else len(lines)


def _rate_black_bars(source: Source, values: dict, samples: list[list[FrameLines]]) -> dict:
    full = values["samples"]["full"]
    lines = [line for frames in samples for frame in frames for line in frame]
    if not lines:
        return {"rating": "unclear", "trust": 0.0, "reason": "keine Bilder lesbar"}
    # Black is the lowest level found: the mean of the darkest line, i.e. the bars if there are any.
    black = float(min(line.min() for line in lines))
    limit = black + black_tolerance
    sides = []
    for frames in samples:
        valid = [(rows, cols) for rows, cols in frames if rows.max() > limit]
        if valid:
            sides.append([(_edge(rows, limit), _edge(rows[::-1], limit), _edge(cols, limit), _edge(cols[::-1], limit))
                          for rows, cols in valid])
    if not sides:
        return {"rating": "unclear", "trust": 0.0, "black_level": round(black, 1), "reason": "nur schwarze Bilder"}
    # Taking the minimum per side keeps every picture area seen in any sample, so mixed formats stay complete;
    # dark scenes and fades only show wider bars and cannot shrink the picture.
    per_sample = [tuple(min(values) for values in zip(*frames)) for frames in sides]
    top, bottom, left, right = (min(values) for values in zip(*per_sample))
    bars = {"top": top, "bottom": bottom, "left": left, "right": right}
    active = (left, top, source.width - left - right, source.height - top - bottom)
    voters = sum(map(len, sides)) if full else len(sides)
    rating = "bars" if any(bars.values()) else "none"
    # Rule of succession: chance that an unseen sample keeps its picture inside the same area.
    trust = 1.0 if rating == "none" else round((voters + 1) / (voters + 2), 4)
    return {"rating": rating, "trust": trust, "black_level": round(black, 1), "bars": bars, "active": active,
            "size": (source.width, source.height), "samples": len(samples), "valid_samples": len(sides),
            "black_samples": len(samples) - len(sides)}


def check_black_bars(source: Source, values: dict, progress: Progress) -> dict:
    """Rate black borders using the unchanged row and column measurements."""
    if not source.video or not source.width or not source.height:
        raise ValueError("Source hat kein lesbares Video.")
    parts = spans(source.duration, values["samples"])
    samples = []
    for number, (start, length) in enumerate(parts, 1):
        progress(f"black_bars: Stichprobe {number}/{len(parts)}", number / len(parts))
        samples.append(list(_frames_lines(source, start, length, values["samples"]["full"])))
    return _rate_black_bars(source, values, samples)

# --------- FUNC AUDIO ---------
def _audio_args(source: Source, number: int, start: float, length: float, full: bool, rate: int) -> list[str]:
    refs = [ref for ref in source.audio if ref.file == number]
    # amerge cannot negotiate overlapping mono layouts unless the format is set after it.
    merge = f"amerge=inputs={len(refs)}," if len(refs) > 1 else ""
    graph = "".join(f"[0:a:{ref.ordinal}]" for ref in refs) + f"{merge}aresample={rate},aformat=sample_fmts=flt[a]"
    return [*_seek(start, length, full), "-i", str(source.files[number]), "-filter_complex", graph, "-map", "[a]",
            "-vn", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


def _audio_blocks(source: Source, start: float, length: float, full: bool, rate: int) -> Iterator[np.ndarray]:
    """Blocks (frames, channels) of all channels of the source in channel order."""
    block_frames = round((full_block_seconds if full else max(length, 0.1)) * rate)
    files = sorted({ref.file for ref in source.audio})
    widths = [sum(int(ref.data.get("channels") or 0) for ref in source.audio if ref.file == number) for number in files]
    readers = [_media.read_chunks(_audio_args(source, number, start, length, full, rate), block_frames * width * sample_bytes)
               for number, width in zip(files, widths)]
    for chunks in zip_longest(*readers, fillvalue=b""):
        arrays = [np.frombuffer(chunk[:len(chunk) // (width * sample_bytes) * width * sample_bytes], "<f4")
                  .reshape(-1, width) for chunk, width in zip(chunks, widths)]
        frames = min(len(array) for array in arrays)
        if frames:
            yield np.hstack([array[:frames] for array in arrays])


def _similarity(spectra: list[np.ndarray], norms: np.ndarray, a: int, b: int, size: int, lag: int) -> tuple[float, int]:
    correlation = np.fft.irfft(spectra[a] * np.conj(spectra[b]), size)
    lags = np.concatenate([correlation[size - lag:], correlation[:lag + 1]]) if lag else correlation[:1]
    best = int(np.argmax(lags))
    return float(lags[best] / (norms[a] * norms[b])), best - lag


def check_audio(source: Source, values: dict, progress: Progress) -> dict:
    count = len(source.channels)
    if not count:
        raise ValueError("Source hat keinen Ton.")
    rate, full = source.sample_rate, values["samples"]["full"]
    level = 10 ** (_media.decibel(values["silence_threshold"], "audio.silence_threshold") / 20)
    min_active = _media.percent(values["silence_min_active"], "audio.silence_min_active") / 100
    window = max(1, round(_media.seconds(values["silence_window"], "audio.silence_window") * rate))
    factor = max(1, rate // compare_rate)
    lag = round(values["max_offset_ms"] / 1000 * rate / factor)
    active, total, hits, windows = np.zeros(count), np.zeros(count), np.zeros(count, int), np.zeros(count, int)
    peak = np.zeros(count)
    pairs = {pair: [] for pair in combinations(range(count), 2)}
    found_spans = spans(source.duration, values["samples"])
    for number, (start, length) in enumerate(found_spans, 1):
        progress(f"audio: Stichprobe {number}/{len(found_spans)}", number / len(found_spans))
        for block in _audio_blocks(source, start, length, full, rate):
            frames = len(block)
            cut = frames // window * window
            squares = np.square(block.astype(np.float64))
            # Same measure as AQC digital_silence: RMS per window, the last shorter window counts with its length.
            parts = [(squares[:cut].reshape(-1, window, count).mean(axis=1), window)] if cut else []
            if frames > cut:
                parts.append((squares[cut:].mean(axis=0, keepdims=True), frames - cut))
            for means, length_samples in parts:
                loud = means >= level ** 2
                active += loud.sum(axis=0) * length_samples
                hits += loud.sum(axis=0)
                windows += len(means)
            total += frames
            peak = np.maximum(peak, np.abs(block).max(axis=0))
            # Channels are compared only where both carry sound in this block.
            reduced = block[:frames // factor * factor].reshape(-1, factor, count).mean(axis=1)
            reduced = reduced - reduced.mean(axis=0)
            loud_block = np.sqrt(np.square(block).mean(axis=0)) >= level
            size = 1 << (len(reduced) + lag).bit_length()
            spectra = [np.fft.rfft(reduced[:, channel], size) for channel in range(count)]
            norms = np.sqrt(np.square(reduced).sum(axis=0)) + 1e-12
            rms = np.sqrt(np.square(block).mean(axis=0)) + 1e-12
            for a, b in pairs:
                if loud_block[a] and loud_block[b]:
                    similarity, offset = _similarity(spectra, norms, a, b, size, lag)
                    pairs[(a, b)].append((similarity, offset, 20 * math.log10(rms[b] / rms[a])))
    channels = []
    for index, ref in enumerate(source.channels):
        audible = bool(total[index]) and active[index] / total[index] >= min_active
        chance = share_above(int(hits[index]), int(windows[index]), min_active)
        trust = 1.0 if full else round(chance if audible else 1 - chance, 4)
        channels.append({"channel": ref.number, "file": source.files[ref.stream.file].name, "stream": ref.stream.ordinal,
                         "audible": audible, "active": round(float(active[index] / max(total[index], 1)) * 100, 2),
                         "peak_db": round(20 * math.log10(peak[index]), 1) if peak[index] > 0 else None,
                         "trust": trust})
    results = []
    for (a, b), found in pairs.items():
        if not (channels[a]["audible"] and channels[b]["audible"]):
            continue
        votes = sum(similarity >= values["duplicate_min"] for similarity, _, _ in found)
        duplicate, trust = _rated(votes, len(found), 0.5, False) if found else (False, 0.0)
        results.append({"channels": [a + 1, b + 1], "similarity": round(median(s for s, _, _ in found), 3) if found else None,
                        "offset_ms": round(median(o for _, o, _ in found) * factor / rate * 1000, 1) if found else None,
                        "level_db": round(median(d for _, _, d in found), 1) if found else None,
                        "duplicate": bool(found) and duplicate, "trust": trust if found else 0.0, "blocks": len(found)})
    return {"trust": min(channel["trust"] for channel in channels), "rate": rate, "channels": channels,
            "audible": [channel["channel"] for channel in channels if channel["audible"]],
            "pairs": results, "duplicates": [pair["channels"] for pair in results if pair["duplicate"]]}


# --------- FUNC SHARED VIDEO ---------
def _can_combine(source: Source, names: list[str], sections: dict[str, dict]) -> bool:
    pair = ("black_bars", "interlace")
    return (all(name in names and key(name, sections[name]) not in source.reports for name in pair)
            and sections["interlace"]["source"] == "analyse"
            and sections["black_bars"]["samples"] == sections["interlace"]["samples"])


def _video_sample(source: Source, start: float, length: float, full: bool) -> tuple[list[FrameLines], FieldCounts]:
    ref, width, height = source.video, source.width, source.height
    # Pin idet precision so gray conversion cannot propagate before split.
    pixel_format = source.video.data.get("pix_fmt")
    scan_format = f"format={pixel_format}," if pixel_format else ""
    graph = (f"[0:v:{ref.ordinal}]split=2[scan_in][bars_in];[scan_in]{scan_format}idet[scan];"
             f"[bars_in]fps={bars_full_fps if full else bars_fps},format=gray[bars]")
    args = [*_seek(start, length, full), "-i", str(source.files[ref.file]), "-filter_complex", graph,
            "-map", "[scan]", "-an", "-f", "null", "-", "-map", "[bars]", "-an", "-f", "rawvideo", "pipe:1"]
    errors = []
    frames = [_gray_lines(chunk, width, height) for chunk in _media.read_chunks(args, width * height, errors)
              if len(chunk) == width * height]
    found = idet_pattern.findall("\n".join(errors))
    return frames, tuple(map(int, found[-1])) if found else (0, 0, 0, 0)


def _video_reports(source: Source, sections: dict[str, dict], progress: Progress) -> dict[str, dict]:
    if not source.video or not source.width or not source.height:
        raise ValueError("Source hat kein lesbares Video.")
    values = sections["black_bars"]["samples"]
    parts, samples, measurements = spans(source.duration, values), [], []
    for number, (start, length) in enumerate(parts, 1):
        progress(f"black_bars+interlace: Stichprobe {number}/{len(parts)}", number / len(parts))
        frames, fields = _video_sample(source, start, length, values["full"])
        samples.append(frames)
        measurements.append(fields)
    reports = {"black_bars": _rate_black_bars(source, sections["black_bars"], samples),
               "interlace": _rate_interlace(source, sections["interlace"], measurements)}
    for name, found in reports.items():
        source.reports[key(name, sections[name])] = found
    return reports


runners = {"streams": check_streams, "interlace": check_interlace, "black_bars": check_black_bars, "audio": check_audio}


def report(source: Source, check: str, values: dict, progress: Progress) -> dict:
    """Report of check with complete section values; measured once per source and values."""
    cache = key(check, values)
    if cache not in source.reports:
        source.reports[cache] = runners[check](source, values, progress)
    return source.reports[cache]


# --------- FUNC FIXES ---------
def _sure(found: dict, need: float, what: str) -> None:
    if found.get("rating") == "unclear" or found.get("trust", 0) < need:
        raise FixError(f"{what} unsicher: {found.get('rating', '?')}, trust {found.get('trust', 0):.2f} < {need:.2f}"
                       + (f" ({found['reason']})" if found.get("reason") else ""))


def fix_interlace(found: dict, values: dict, scan: str) -> tuple[dict, str | None]:
    _sure(found, values["trust"], "Interlacing")
    if found["rating"] == "interlaced" and scan == "progressive":
        return {"deinterlace": True}, "deinterlaced"
    return {}, None


def fix_black_bars(found: dict, values: dict, width: int, height: int) -> tuple[dict, str | None]:
    _sure(found, values["trust"], "Schwarze Ränder")
    if found["rating"] == "none":
        return {}, None
    bars, extra = found["bars"], values["extra_crop"]
    left = bars["left"] + (extra if bars["left"] else 0)
    top = bars["top"] + (extra if bars["top"] else 0)
    right = width - bars["right"] - (extra if bars["right"] else 0)
    bottom = height - bars["bottom"] - (extra if bars["bottom"] else 0)
    # 4:2:0 needs even values; rounding goes inwards.
    left, top, right, bottom = left + left % 2, top + top % 2, right - right % 2, bottom - bottom % 2
    if right - left < 2 or bottom - top < 2:
        raise FixError(f"Schwarze Ränder: keine Bildfläche übrig ({bars}).")
    box = (left, top, right - left, bottom - top)
    return {"crop": box}, f"beschnitten auf {box[2]}×{box[3]} bei {box[0]},{box[1]}"


def fix_audio(found: dict, values: dict, target: dict, count: int) -> tuple[dict, str | None]:
    need = values["trust"]
    if unsure := [channel for channel in found["channels"] if channel["trust"] < need]:
        raise FixError("Ton unsicher: " + ", ".join(f"Kanal {channel['channel']} trust {channel['trust']:.2f}"
                                                     for channel in unsure) + f" < {need:.2f}")
    audible = found["audible"]
    if len(audible) == 1:
        return {"audio_map": [audible[0], audible[0]]}, f"Kanal {audible[0]} auf [{audible[0]}, {audible[0]}]"
    if not {1, 2} <= set(audible):
        raise FixError(f"Ton: hörbar {', '.join(map(str, audible)) or 'keiner'}; erwartet Kanal 1 und 2 oder genau "
                       f"1 hörbaren Kanal")
    if "min_audible" in target:
        return {}, None
    pairs = {tuple(pair["channels"]): pair for pair in found["pairs"]}
    for channel in (number for number in audible if number > 2):
        matches = [pairs.get((first, channel)) for first in (1, 2)]
        if not any(pair and pair["duplicate"] for pair in matches):
            best = max((pair["similarity"] or 0 for pair in matches if pair), default=0)
            raise FixError(f"Ton: Kanal {channel} hörbar und nicht doppelt zu Kanal 1/2 (Ähnlichkeit {best:.2f})")
        if any(pair and pair["duplicate"] and pair["trust"] < need for pair in matches):
            raise FixError(f"Ton: Dublette mit Kanal {channel} unsicher (trust < {need:.2f})")
    dropped = list(range(3, count + 1))
    return {"audio_map": [1, 2]}, f"Kanäle 1, 2 ({', '.join(map(str, dropped))} verworfen)" if dropped else None
