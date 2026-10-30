"""Conservatively classify the documented H.264 timestamp failure pattern."""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from typing import Any

from oneoff.contracts import ClipAnalysis, Diagnosis

_SCHEMA_VERSION = 1


def _fraction(value: Any) -> Fraction | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError, TypeError, OverflowError):
        return None
    return result


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.isdigit() or (text[0] in "+-" and text[1:].isdigit())):
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _diagnosis(
    kind: str,
    reason: str,
    evidence: dict[str, Any],
    frame_rate: Fraction | None = None,
    frame_count: int | None = None,
) -> Diagnosis:
    return Diagnosis(
        kind=kind,  # type: ignore[arg-type]
        reason=reason,
        evidence=evidence,
        frame_rate=frame_rate,
        decoder_frame_count=frame_count,
        accepted_terminal_discard=False,
    )


def _unsupported(reason: str, evidence: dict[str, Any], frame_rate: Fraction | None = None,
                 frame_count: int | None = None) -> Diagnosis:
    return _diagnosis("unsupported", reason, evidence, frame_rate, frame_count)


def _unclear(reason: str, evidence: dict[str, Any], frame_rate: Fraction | None = None,
             frame_count: int | None = None) -> Diagnosis:
    return _diagnosis("unclear", reason, evidence, frame_rate, frame_count)


def _stream_index(item: dict[str, Any]) -> int | None:
    value = item.get("index")
    return _integer(value) if value is not None else None


def _same_stream(frame: dict[str, Any], packet: dict[str, Any], index: int | None) -> bool:
    frame_stream = _integer(frame.get("stream_index"))
    packet_stream = _integer(packet.get("stream_index"))
    if frame_stream is not None and packet_stream is not None:
        return frame_stream == packet_stream
    if index is None:
        return True
    return frame_stream in (None, index) and packet_stream in (None, index)


def _packet_indexes(
    packets: list[dict[str, Any]],
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    positions: dict[int, list[int]] = defaultdict(list)
    decode_timestamps: dict[int, list[int]] = defaultdict(list)
    for packet_index, packet in enumerate(packets):
        pos = _integer(packet.get("pos"))
        dts = _integer(packet.get("dts"))
        if pos is not None:
            positions[pos].append(packet_index)
        if dts is not None:
            decode_timestamps[dts].append(packet_index)
    return positions, decode_timestamps


def _candidate_packet_index(
    frame: dict[str, Any],
    positions: dict[int, list[int]],
    decode_timestamps: dict[int, list[int]],
    packets: list[dict[str, Any]],
    stream_index: int | None,
) -> int | None:
    frame_pos = _integer(frame.get("pkt_pos"))
    frame_dts = _integer(frame.get("pkt_dts"))
    possible: set[int] = set()
    if frame_pos is not None:
        candidates = positions.get(frame_pos, [])
        if len(candidates) != 1:
            return None
        possible.update(candidates)
    if frame_dts is not None:
        candidates = decode_timestamps.get(frame_dts, [])
        candidates = [i for i in candidates if _same_stream(frame, packets[i], stream_index)]
        if len(candidates) != 1 or (possible and candidates[0] not in possible):
            return None
        possible.update(candidates)
    if len(possible) != 1:
        return None
    return next(iter(possible))


def _associated_frame(
    frame: dict[str, Any],
    packet: dict[str, Any],
    packet_index: int,
    stream_index: int | None,
) -> dict[str, Any] | None:
    if not _same_stream(frame, packet, stream_index):
        return None
    frame_dts = _integer(frame.get("pkt_dts"))
    packet_dts = _integer(packet.get("dts"))
    if frame_dts is not None and packet_dts != frame_dts:
        return None
    frame_pts = _integer(frame.get("pts"))
    packet_pts = _integer(packet.get("pts"))
    if frame_pts is not None and packet_pts is not None and frame_pts != packet_pts:
        return None
    if packet_pts is None or packet_dts is None:
        return None
    return {
        "packet_index": packet_index,
        "pts": packet_pts,
        "dts": packet_dts,
        "pict_type": str(frame.get("pict_type", "")).upper(),
    }


def _associate_frames(
    frames: list[dict[str, Any]], packets: list[dict[str, Any]], stream_index: int | None
) -> list[dict[str, Any]] | None:
    positions, decode_timestamps = _packet_indexes(packets)
    used: set[int] = set()
    associated: list[dict[str, Any]] = []
    for frame in frames:
        packet_index = _candidate_packet_index(
            frame, positions, decode_timestamps, packets, stream_index
        )
        if packet_index is None or packet_index in used:
            return None
        item = _associated_frame(frame, packets[packet_index], packet_index, stream_index)
        if item is None:
            return None
        used.add(packet_index)
        associated.append(item)
    if len(used) != len(packets):
        return None
    return associated


def _probe_data(
    raw: dict[str, Any],
) -> tuple[list[Any], Any, Any, dict[str, Any], int | None] | Diagnosis:
    status = raw.get("status")
    metadata = raw.get("metadata")
    frames = raw.get("decoder_frames")
    packets = raw.get("packets")
    frame_count = len(frames) if isinstance(frames, list) else None
    evidence: dict[str, Any] = {"probe_status": status}
    if status != "complete":
        return _unclear("Die ffprobe-Erhebung ist nicht vollständig.", evidence, frame_count=frame_count)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("streams"), list):
        return _unclear("Stream-Metadaten fehlen oder sind ungültig.", evidence, frame_count=frame_count)
    streams = metadata["streams"]
    return streams, frames, packets, evidence, frame_count


def _validate_frame_packet_data(
    frames: Any, packets: Any, evidence: dict[str, Any], frame_count: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | Diagnosis:
    if not isinstance(frames, list) or not isinstance(packets, list) or not frames or not packets:
        return _unclear("Paket- oder Decoderbilddaten fehlen.", {
            **evidence,
            "packet_count": len(packets) if isinstance(packets, list) else None,
            "decoder_frame_count": frame_count,
        }, frame_count=frame_count)
    if not all(isinstance(row, dict) for row in frames + packets):
        return _unclear("Paket- oder Decoderbilddaten haben eine ungültige Struktur.", evidence,
                        frame_count=frame_count)
    return frames, packets


def _select_video_stream(
    streams: list[Any], evidence: dict[str, Any], frame_count: int | None
) -> tuple[dict[str, Any], int | None, str] | Diagnosis:
    video_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    if len(video_streams) != 1:
        return _unsupported("Es muss genau einen Videostream geben.", {
            **evidence, "video_stream_count": len(video_streams),
        }, frame_count=frame_count)
    stream = video_streams[0]
    stream_index = _stream_index(stream)
    codec = str(stream.get("codec_name", "")).casefold()
    if codec != "h264":
        return _unsupported("Der Videostream ist kein H.264-Stream.", {
            **evidence, "codec_name": stream.get("codec_name"),
        }, frame_count=frame_count)
    return stream, stream_index, codec


def _validate_timing(
    stream: dict[str, Any], packets: list[dict[str, Any]], evidence: dict[str, Any],
    frame_count: int | None,
) -> tuple[Fraction, int] | Diagnosis:
    rate = _fraction(stream.get("avg_frame_rate"))
    nominal_rate = _fraction(stream.get("r_frame_rate"))
    time_base = _fraction(stream.get("time_base"))
    evidence.update({
        "codec_name": "h264",
        "stream_index": _stream_index(stream),
        "packet_count": len(packets),
        "decoder_frame_count": frame_count,
        "avg_frame_rate": str(rate) if rate is not None else None,
        "r_frame_rate": str(nominal_rate) if nominal_rate is not None else None,
        "time_base": str(time_base) if time_base is not None else None,
    })
    if rate is None or nominal_rate is None or time_base is None or rate <= 0 or nominal_rate <= 0 or time_base <= 0:
        return _unclear("Bildrate oder Zeitbasis kann nicht ganzzahlig und exakt bestimmt werden.",
                        evidence, rate, frame_count)
    if rate != nominal_rate:
        return _unsupported("Die gemeldeten Bildraten widersprechen sich.", evidence, rate, frame_count)
    expected_ticks = Fraction(1, 1) / (rate * time_base)
    packet_dts = [_integer(packet.get("dts")) for packet in packets]
    if expected_ticks.denominator != 1 or len(packet_dts) < 2 or any(v is None for v in packet_dts):
        return _unclear("DTS-Raster kann nicht mit ganzzahligen Ticks bestätigt werden.", {
            **evidence, "expected_frame_ticks": str(expected_ticks),
        }, rate, frame_count)
    tick_values = [value for value in packet_dts if value is not None]
    dts_steps = [right - left for left, right in zip(tick_values, tick_values[1:])]
    expected_tick_count = expected_ticks.numerator
    if any(step != expected_tick_count for step in dts_steps):
        return _unsupported("DTS laufen nicht in einem regelmäßigen, zur Bildrate passenden Raster.", {
            **evidence,
            "expected_frame_ticks": expected_tick_count,
            "observed_dts_steps": dts_steps[:20],
            "irregular_dts_step_count": sum(step != expected_tick_count for step in dts_steps),
        }, rate, frame_count)
    return rate, expected_tick_count


def _map_frames(
    frames: list[dict[str, Any]], packets: list[dict[str, Any]], stream_index: int | None,
    evidence: dict[str, Any], rate: Fraction, frame_count: int | None, expected_tick_count: int,
) -> tuple[list[dict[str, Any]], list[str]] | Diagnosis:
    mapped = _associate_frames(frames, packets, stream_index)
    if mapped is None or len(mapped) != len(frames) or len(frames) != len(packets):
        return _unclear("Decoderbilder lassen sich nicht eindeutig und vollständig Paketen zuordnen.", {
            **evidence,
            "expected_frame_ticks": expected_tick_count,
            "dts_regular": True,
            "frame_packet_mapping": "ambiguous_or_incomplete",
        }, rate, frame_count)
    picture_types = [item["pict_type"] for item in mapped]
    if any(kind not in {"I", "P", "B"} for kind in picture_types):
        return _unsupported("Es liegen nicht unterstützte oder fehlende Bildtypen vor.", {
            **evidence, "picture_type_counts": _counts(picture_types),
        }, rate, frame_count)
    return mapped, picture_types


def _pts_order_anomalies(
    mapped: list[dict[str, Any]],
) -> tuple[list[dict[str, int]], list[dict[str, int]]]:
    regressions: list[dict[str, int]] = []
    equal_neighbors: list[dict[str, int]] = []
    pts_sequence = [item["pts"] for item in mapped]
    for index, (previous, current) in enumerate(zip(pts_sequence, pts_sequence[1:]), start=1):
        if current < previous:
            regressions.append({
                "previous_frame": index - 1, "frame": index,
                "previous_pts": previous, "pts": current,
            })
        elif current == previous:
            equal_neighbors.append({
                "previous_frame": index - 1, "frame": index, "pts": current,
            })
    return regressions, equal_neighbors


def _cross_type_duplicates(mapped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_pts: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for index, item in enumerate(mapped):
        by_pts[item["pts"]].append((index, item["pict_type"]))
    duplicates: list[dict[str, Any]] = []
    for pts, members in sorted(by_pts.items()):
        has_b = any(kind == "B" for _, kind in members)
        has_p = any(kind == "P" for _, kind in members)
        if has_b and has_p:
            duplicates.append({
                "pts": pts,
                "frame_indices": [index for index, _ in members],
                "picture_types": [kind for _, kind in members],
            })
    return duplicates


def _b_frame_pts_equals_dts(mapped: list[dict[str, Any]]) -> list[dict[str, int]]:
    return [
        {"frame": index, "pts": item["pts"], "dts": item["dts"]}
        for index, item in enumerate(mapped)
        if item["pict_type"] == "B" and item["pts"] == item["dts"]
    ]


def _failure_signature(
    picture_types: list[str], regressions: list[dict[str, Any]],
    equal_neighbors: list[dict[str, Any]], cross_type_duplicates: list[dict[str, Any]],
    b_pts_equals_dts: list[dict[str, int]],
) -> dict[str, bool]:
    return {
        "h264": True,
        "b_frames_present": "B" in picture_types,
        "p_frames_present": "P" in picture_types,
        "constant_frame_rate_confirmed": True,
        "regular_dts_confirmed": True,
        "unique_frame_packet_mapping": True,
        "repeated_pts_across_b_and_p_frames": bool(cross_type_duplicates),
        "decoder_order_regression": bool(regressions),
        "decoder_order_equal_pts": bool(equal_neighbors),
        "b_frame_pts_equals_dts": bool(b_pts_equals_dts),
    }


def _record_timestamp_evidence(
    evidence: dict[str, Any], rate: Fraction, expected_tick_count: int,
    picture_types: list[str], regressions: list[dict[str, Any]],
    equal_neighbors: list[dict[str, Any]], cross_type_duplicates: list[dict[str, Any]],
    b_pts_equals_dts: list[dict[str, int]], signature: dict[str, bool],
) -> None:
    evidence.update({
        "frame_rate": str(rate),
        "expected_frame_ticks": expected_tick_count,
        "dts_regular": True,
        "frame_packet_mapping": "one_to_one",
        "picture_type_counts": _counts(picture_types),
        "decoder_order_regressions": regressions[:20],
        "decoder_order_regression_count": len(regressions),
        "decoder_order_equal_pts": equal_neighbors[:20],
        "decoder_order_equal_pts_count": len(equal_neighbors),
        "repeated_pts_across_b_and_p": cross_type_duplicates[:20],
        "repeated_pts_across_b_and_p_count": len(cross_type_duplicates),
        "b_frame_pts_equals_dts": b_pts_equals_dts[:20],
        "b_frame_pts_equals_dts_count": len(b_pts_equals_dts),
        "known_failure_pattern": signature,
    })


def _final_diagnosis(
    evidence: dict[str, Any], rate: Fraction, frame_count: int | None,
    signature: dict[str, bool], regressions: list[dict[str, Any]],
    equal_neighbors: list[dict[str, Any]], cross_type_duplicates: list[dict[str, Any]],
) -> Diagnosis:
    required = (
        "h264", "b_frames_present", "p_frames_present", "constant_frame_rate_confirmed",
        "regular_dts_confirmed", "unique_frame_packet_mapping",
        "repeated_pts_across_b_and_p_frames", "decoder_order_regression",
        "decoder_order_equal_pts", "b_frame_pts_equals_dts",
    )
    if all(signature[key] for key in required):
        return _diagnosis("confirmed", "Das dokumentierte H.264-Fehlerbild ist vollständig nachgewiesen.",
                          evidence, rate, frame_count)
    if regressions or equal_neighbors or cross_type_duplicates:
        return _unsupported("Zeitstempel-Anomalie vorhanden, aber das dokumentierte Fehlerbild ist nicht vollständig nachgewiesen.",
                            evidence, rate, frame_count)
    return _diagnosis("clean", "Nicht betroffen: Das dokumentierte Fehlerbild wurde nicht festgestellt.",
                      evidence, rate, frame_count)


def _classify_mapped(
    mapped: list[dict[str, Any]], picture_types: list[str], evidence: dict[str, Any],
    rate: Fraction, frame_count: int | None, expected_tick_count: int,
) -> Diagnosis:
    regressions, equal_neighbors = _pts_order_anomalies(mapped)
    cross_type_duplicates = _cross_type_duplicates(mapped)
    b_pts_equals_dts = _b_frame_pts_equals_dts(mapped)
    signature = _failure_signature(
        picture_types, regressions, equal_neighbors, cross_type_duplicates, b_pts_equals_dts
    )
    _record_timestamp_evidence(
        evidence, rate, expected_tick_count, picture_types, regressions,
        equal_neighbors, cross_type_duplicates, b_pts_equals_dts, signature,
    )
    return _final_diagnosis(
        evidence, rate, frame_count, signature, regressions,
        equal_neighbors, cross_type_duplicates,
    )


def _classify_valid_raw(raw: dict[str, Any]) -> Diagnosis:
    probe = _probe_data(raw)
    if isinstance(probe, Diagnosis):
        return probe
    streams, frames, packets, evidence, frame_count = probe
    selected = _select_video_stream(streams, evidence, frame_count)
    if isinstance(selected, Diagnosis):
        return selected
    stream, stream_index, _codec = selected
    frame_packet_data = _validate_frame_packet_data(frames, packets, evidence, frame_count)
    if isinstance(frame_packet_data, Diagnosis):
        return frame_packet_data
    frames, packets = frame_packet_data
    timing = _validate_timing(stream, packets, evidence, frame_count)
    if isinstance(timing, Diagnosis):
        return timing
    rate, expected_tick_count = timing
    mapped_result = _map_frames(
        frames, packets, stream_index, evidence, rate, frame_count, expected_tick_count
    )
    if isinstance(mapped_result, Diagnosis):
        return mapped_result
    mapped, picture_types = mapped_result
    return _classify_mapped(
        mapped, picture_types, evidence, rate, frame_count, expected_tick_count
    )


def classify(clip: ClipAnalysis) -> Diagnosis:
    """Classify one clip using only its collected ffprobe evidence."""
    raw = getattr(clip, "raw", None)
    if not isinstance(raw, dict):
        return _unclear("Rohdaten fehlen oder haben ein ungültiges Format.", {"raw_available": False})
    if raw.get("schema_version") != _SCHEMA_VERSION:
        return _unclear("Nicht unterstützte oder fehlende Rohdaten-Schemaversion.", {
            "schema_version": raw.get("schema_version"),
        })
    return _classify_valid_raw(raw)


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
