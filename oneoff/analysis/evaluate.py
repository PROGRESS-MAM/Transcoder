"""Evaluate stored packet timestamps separately from decoded frame timestamps."""

from collections import defaultdict
from fractions import Fraction
import re
from pathlib import Path

REFERENCE_LABELS = {
    "58238241.mp4": "error_present",
    "58238527.mp4": "error_present",
    "53287289.mp4": "no_error_no_b_frames",
}


def clip_name(raw):
    source = raw.get("file") if isinstance(raw, dict) else raw
    if isinstance(source, dict):
        source = source.get("name") or source.get("abs_path")
    return Path(str(source or "")).name


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _time_base(value):
    if isinstance(value, dict):
        numerator = _integer(value.get("num", value.get("numerator")))
        denominator = _integer(value.get("den", value.get("denominator")))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        numerator, denominator = (_integer(part) for part in value)
    elif isinstance(value, str) and "/" in value:
        numerator_text, denominator_text = value.split("/", 1)
        numerator, denominator = _integer(numerator_text), _integer(denominator_text)
    else:
        return None
    if numerator is None or denominator in (None, 0):
        return None
    return Fraction(numerator, denominator)


def _seconds(ticks, time_base):
    tick_value = _integer(ticks)
    base = _time_base(time_base)
    if tick_value is None or base is None:
        return None
    return float(tick_value * base)


def _location(record):
    return {
        "decode_order_index": record["decode_order_index"],
        "packet_index": record.get("packet_index"),
        "pos": record.get("pos"),
    }


def _frame_location(record):
    return {
        "decoder_frame_index": record["decoder_frame_index"],
        "pkt_pos": record.get("pkt_pos"),
    }


def _duplicate_groups(records, field, location_func):
    grouped = defaultdict(list)
    for record in records:
        value = _integer(record.get(field))
        if value is not None:
            grouped[value].append(location_func(record))
    return [
        {"timestamp": value, "locations": locations}
        for value, locations in sorted(grouped.items())
        if len(locations) > 1
    ]


def _adjacent_steps(records, field, location_func, time_base=None):
    steps = []
    for previous, current in zip(records, records[1:]):
        left = _integer(previous.get(field))
        right = _integer(current.get(field))
        delta = right - left if left is not None and right is not None else None
        step_time_base = time_base or current.get("time_base") or previous.get("time_base")
        step = {
            "from": location_func(previous),
            "to": location_func(current),
            "delta_ticks": delta,
            "delta_seconds": _seconds(delta, step_time_base),
        }
        if delta is None:
            step["status"] = "unknown_missing_timestamp"
        elif delta < 0:
            step["status"] = "backward"
        elif delta == 0:
            step["status"] = "duplicate"
        else:
            step["status"] = "increasing"
        steps.append(step)
    return steps


def _timestamp_checks(records, field, location_func, order_name, time_base=None):
    missing = [location_func(record) for record in records if _integer(record.get(field)) is None]
    duplicates = _duplicate_groups(records, field, location_func)
    steps = _adjacent_steps(records, field, location_func, time_base)
    backward = [step for step in steps if step["delta_ticks"] is not None and step["delta_ticks"] < 0]
    equal = [step for step in steps if step["delta_ticks"] == 0]
    return {
        "ordered_by": order_name,
        "missing": missing,
        "duplicates": duplicates,
        "backward_steps": backward,
        "equal_adjacent_steps": equal,
        "adjacent_deltas": steps,
    }


def _packet_records(raw):
    records = []
    for order_index, packet in enumerate(raw.get("packets") or []):
        packet = packet if isinstance(packet, dict) else {}
        records.append({
            "decode_order_index": order_index,
            "packet_index": packet.get("index", order_index),
            "pts": packet.get("pts"),
            "dts": packet.get("dts"),
            "duration": packet.get("duration"),
            "pos": packet.get("pos"),
            "size": packet.get("size"),
            "flags": packet.get("flags"),
            "time_base": packet.get("time_base"),
        })
    return records


def _frame_records(raw):
    records = []
    for frame_index, frame in enumerate(raw.get("frames") or []):
        frame = frame if isinstance(frame, dict) else {}
        records.append({
            "decoder_frame_index": frame_index,
            "pict_type": frame.get("pict_type"),
            "key_frame": frame.get("key_frame"),
            "pts": frame.get("pts"),
            "pkt_dts": frame.get("pkt_dts"),
            "best_effort_timestamp": frame.get("best_effort_timestamp"),
            "duration": frame.get("duration"),
            "pkt_pos": frame.get("pkt_pos"),
            "coded_picture_number": frame.get("coded_picture_number"),
            "display_picture_number": frame.get("display_picture_number"),
        })
    return records


def _presentation_order(records, timestamp_field, location_func, fallback_field=None):
    known = []
    missing = []
    for record in records:
        value = _integer(record.get(timestamp_field))
        source = timestamp_field
        if value is None and fallback_field is not None:
            value = _integer(record.get(fallback_field))
            source = fallback_field
        if value is None:
            missing.append(location_func(record))
        else:
            known.append((value, record, source))
    known.sort(key=lambda item: (item[0], location_func(item[1]).get("decode_order_index", location_func(item[1]).get("decoder_frame_index", 0))))
    order = []
    for rank, (timestamp, record, source) in enumerate(known):
        order.append({"presentation_rank": rank, "timestamp": timestamp, "timestamp_source": source, "location": location_func(record)})
    sorted_by = timestamp_field if fallback_field is None else f"{timestamp_field} (fallback: {fallback_field})"
    return {"sorted_by": sorted_by, "order": order, "missing_timestamp": missing}


def _periodic_patterns(values, locations, field, min_period=2, max_period=16):
    """Find repeated, non-constant adjacent tick patterns and retain their locations."""
    numeric = [_integer(value) for value in values]
    patterns = []
    start = 0
    while start < len(numeric):
        best = None
        upper = min(max_period, (len(numeric) - start) // 2)
        for period in range(min_period, upper + 1):
            block = numeric[start:start + period]
            if any(value is None for value in block) or len(set(block)) <= 1:
                continue
            repetitions = 1
            while start + (repetitions + 1) * period <= len(numeric):
                next_block = numeric[start + repetitions * period:start + (repetitions + 1) * period]
                if next_block != block:
                    break
                repetitions += 1
            if repetitions >= 2 and (best is None or period < best["period"]):
                best = {"period": period, "repetitions": repetitions, "values": block}
        if best:
            end = start + best["period"] * best["repetitions"]
            patterns.append({
                "field": field,
                "period_length": best["period"],
                "repetitions": best["repetitions"],
                "values_ticks": best["values"],
                "locations": locations[start:end],
            })
            start = end
        else:
            start += 1
    return patterns


def _gop_sequences(frames):
    gops = []
    current = []
    for frame in frames:
        picture_type = frame.get("pict_type")
        if picture_type == "I" and current:
            gops.append(current)
            current = []
        current.append(frame)
    if current:
        gops.append(current)
    return [
        {
            "gop_number": index,
            "decoder_frame_indices": [frame["decoder_frame_index"] for frame in group],
            "pict_types": [frame.get("pict_type") or "?" for frame in group],
            "sequence": " ".join(frame.get("pict_type") or "?" for frame in group),
        }
        for index, group in enumerate(gops)
    ]


def _compare_packet_frames(packets, frames):
    by_pos = defaultdict(list)
    for packet in packets:
        if packet.get("pos") is not None:
            by_pos[str(packet.get("pos"))].append(packet)
    comparisons = []
    contradictions = []
    for frame in frames:
        frame_pos = frame.get("pkt_pos")
        matches = by_pos.get(str(frame_pos), []) if frame_pos is not None else []
        if len(matches) != 1:
            if frame_pos is not None:
                comparisons.append({
                    "frame": _frame_location(frame),
                    "status": "packet_position_unmatched_or_ambiguous",
                    "matching_packet_count": len(matches),
                })
            continue
        packet = matches[0]
        packet_pts = _integer(packet.get("pts"))
        frame_pts = _integer(frame.get("pts"))
        result = {
            "packet": _location(packet),
            "frame": _frame_location(frame),
            "packet_pts": packet_pts,
            "decoder_frame_pts": frame_pts,
            "best_effort_timestamp": _integer(frame.get("best_effort_timestamp")),
            "comparison_basis": "exact integer comparison; frame time_base is not supplied by the input interface",
        }
        if packet_pts is None or frame_pts is None:
            result["status"] = "unknown_missing_timestamp"
        elif packet_pts != frame_pts:
            result["status"] = "explicit_pts_mismatch"
            contradictions.append(result)
        else:
            result["status"] = "explicit_pts_match"
        comparisons.append(result)
    return comparisons, contradictions


def _indicator(kind, locations, detail):
    return {"kind": kind, "count": len(locations), "locations": locations, "detail": detail}


def evaluate_clip(raw):
    """Return timestamp, ordering, GOP, and reference analysis for a probe result."""
    packets = _packet_records(raw)
    frames = _frame_records(raw)
    packet_time_base = next((packet.get("time_base") for packet in packets if packet.get("time_base") is not None), None)
    for packet in packets:
        if packet.get("time_base") is None:
            packet["time_base"] = packet_time_base
    packet_pos_dts = _timestamp_checks(packets, "dts", _location, "stored packet decode order")
    packet_pos_pts = _timestamp_checks(packets, "pts", _location, "stored packet decode order")
    packet_pts_dts = []
    for packet in packets:
        pts, dts = _integer(packet.get("pts")), _integer(packet.get("dts"))
        difference = pts - dts if pts is not None and dts is not None else None
        packet_pts_dts.append({
            **_location(packet),
            "pts": pts,
            "dts": dts,
            "pts_minus_dts_ticks": difference,
            "pts_minus_dts_seconds": _seconds(difference, packet.get("time_base")),
        })
    packet_pts_delta = packet_pos_pts["adjacent_deltas"]
    packet_dts_delta = packet_pos_dts["adjacent_deltas"]
    packet_delta_locations = [step["to"] for step in packet_pts_delta]
    packet_offset_locations = [_location(packet) for packet in packets]
    periodic_patterns = (
        _periodic_patterns([step["delta_ticks"] for step in packet_pts_delta], packet_delta_locations, "packet PTS delta")
        + _periodic_patterns([step["delta_ticks"] for step in packet_dts_delta], [step["to"] for step in packet_dts_delta], "packet DTS delta")
        + _periodic_patterns([item["pts_minus_dts_ticks"] for item in packet_pts_dts], packet_offset_locations, "packet PTS-DTS")
    )
    frame_checks = {
        field: _timestamp_checks(frames, field, _frame_location, "decoder frame-list order")
        for field in ("pts", "pkt_dts", "best_effort_timestamp")
    }
    frame_b_locations = [_frame_location(frame) for frame in frames if frame.get("pict_type") == "B"]
    frame_presentation = _presentation_order(frames, "best_effort_timestamp", _frame_location, "pts")
    decoder_periodic_patterns = []
    for field_name, checks in frame_checks.items():
        decoder_periodic_patterns.extend(_periodic_patterns(
            [step["delta_ticks"] for step in checks["adjacent_deltas"]],
            [step["to"] for step in checks["adjacent_deltas"]],
            f"decoder {field_name} delta",
        ))
    packet_presentation = _presentation_order(packets, "pts", _location)
    packet_frame_comparisons, contradictions = _compare_packet_frames(packets, frames)

    strong_indicators = []
    for label, locations in (
        ("missing stored packet PTS", packet_pos_pts["missing"]),
        ("missing stored packet DTS", packet_pos_dts["missing"]),
    ):
        if locations:
            strong_indicators.append(_indicator("missing_packet_timestamp", locations, label))
    for field_name, check in (("PTS", packet_pos_pts), ("DTS", packet_pos_dts)):
        for duplicate in check["duplicates"]:
            strong_indicators.append(_indicator(
                "duplicate_packet_timestamp",
                duplicate["locations"],
                f"duplicate stored packet {field_name}={duplicate['timestamp']}",
            ))
    if packet_pos_dts["backward_steps"]:
        strong_indicators.append(_indicator(
            "non_monotonic_packet_dts",
            [step["to"] for step in packet_pos_dts["backward_steps"]],
            "stored packet DTS moves backward in decode order",
        ))
    for mismatch in contradictions:
        strong_indicators.append(_indicator(
            "packet_decoder_pts_mismatch",
            [mismatch["packet"], mismatch["frame"]],
            f"packet PTS={mismatch['packet_pts']} and decoder frame PTS={mismatch['decoder_frame_pts']} differ for the same packet position",
        ))

    source_anomaly = bool(strong_indicators)
    only_reordering = bool(frame_b_locations or packet_pos_pts["backward_steps"]) and not source_anomaly
    filename = clip_name(raw)
    reference = REFERENCE_LABELS.get(filename)
    if source_anomaly:
        observed = "timestamp_anomaly_indicators_found"
    elif only_reordering:
        observed = "reordering_observed_no_corresponding_packet_integrity_anomaly"
    else:
        observed = "no_timestamp_anomaly_indicators_detected"
    if reference == "error_present":
        reference_assessment = "supports_reference" if source_anomaly else "not_demonstrated_by_available_indicators"
    elif reference == "no_error_no_b_frames":
        reference_assessment = "supports_reference" if not source_anomaly and not frame_b_locations else "conflicts_with_reference"
    else:
        reference_assessment = "unknown_no_reference_label"

    return {
        "file": raw.get("file"),
        "analysed_at": raw.get("analysed_at"),
        "reference": {"expected": reference, "assessment": reference_assessment},
        "observed_classification": observed,
        "stored_packets": {
            "decode_order": packets,
            "presentation_order_by_pts": packet_presentation,
            "pts_checks_in_decode_order": packet_pos_pts,
            "dts_checks_in_decode_order": packet_pos_dts,
            "pts_minus_dts": packet_pts_dts,
            "periodic_patterns": periodic_patterns,
        },
        "decoder_frames": {
            "decoder_list_order": frames,
            "pict_type_sequence": [frame.get("pict_type") or "?" for frame in frames],
            "gop_sequences": _gop_sequences(frames),
            "b_frame_locations": frame_b_locations,
            "presentation_order_by_best_effort_timestamp": frame_presentation,
            "timestamp_checks": frame_checks,
            "periodic_patterns": decoder_periodic_patterns,
            "packet_frame_comparisons": packet_frame_comparisons,
        },
        "findings": {
            "stored_packet_timestamp_anomaly_detected": source_anomaly,
            "strong_indicators": strong_indicators,
            "packet_pts_backward_steps_are_not_sufficient_alone": packet_pos_pts["backward_steps"],
            "b_frames_alone_are_not_sufficient": bool(frame_b_locations),
            "reordering_only_without_packet_integrity_anomaly": only_reordering,
            "decoder_best_effort_timestamps_are_separate_from_stored_packet_timestamps": True,
        },
    }
