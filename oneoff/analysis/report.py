"""Markdown reporting and durable JSON output for the reference-clip analysis."""

import json
from pathlib import Path

from .evaluate import clip_name, evaluate_clip


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _unknownify(value):
    if value is None:
        return "unknown"
    if isinstance(value, dict):
        return {key: _unknownify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unknownify(item) for item in value]
    return value


def _locations(locations):
    if not locations:
        return "none"
    values = []
    for item in locations:
        parts = []
        for key in ("decode_order_index", "packet_index", "pos", "decoder_frame_index", "pkt_pos"):
            if key in item:
                parts.append(f"{key}={item[key] if item[key] is not None else 'unknown'}")
        values.append("(" + ", ".join(parts) + ")")
    return ", ".join(values)


def _code_block(title, value):
    return [f"**{title}:**", "", "```json", _json(_unknownify(value)), "```", ""]


def _inventory_block(inventory):
    if not isinstance(inventory, dict) or not inventory:
        inventory = "unknown"
    return _code_block("Complete probe inventory", inventory)


def _source_file(raw):
    source = raw.get("file")
    if isinstance(source, dict):
        source = source.get("abs_path") or source.get("name")
    return str(source or "unknown")


def _packet_excerpt(evaluation, limit=24):
    packets = evaluation["stored_packets"]["decode_order"]
    if not packets:
        return ["No stored packet records were provided."]
    lines = ["| Decode index | Packet index | Position | Time base | PTS | DTS | PTS−DTS (ticks) | PTS−DTS (s) | Duration |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    offsets = {item["decode_order_index"]: item for item in evaluation["stored_packets"]["pts_minus_dts"]}
    for packet in packets[:limit]:
        offset = offsets.get(packet["decode_order_index"], {})
        pts, dts = packet.get("pts"), packet.get("dts")
        difference = offset.get("pts_minus_dts_ticks")
        difference_seconds = offset.get("pts_minus_dts_seconds")
        values = [packet.get("decode_order_index"), packet.get("packet_index"), packet.get("pos"), packet.get("time_base"), pts, dts, difference, difference_seconds, packet.get("duration")]
        lines.append("| " + " | ".join("unknown" if value is None else str(value) for value in values) + " |")
    if len(packets) > limit:
        lines.append(f"| … | … | … | … | … | … | … | … | … | _(showing {limit} of {len(packets)} packets; full data is in JSON)_")
    return lines


def _delta_excerpt(evaluation, limit=24):
    pts_steps = evaluation["stored_packets"]["pts_checks_in_decode_order"]["adjacent_deltas"]
    dts_steps = evaluation["stored_packets"]["dts_checks_in_decode_order"]["adjacent_deltas"]
    if not pts_steps:
        return ["No adjacent stored packet timestamps were provided."]
    lines = ["| From (index, pos) | To (index, pos) | PTS delta (ticks / s) | DTS delta (ticks / s) |", "|---|---|---:|---:|"]
    for pts_step, dts_step in list(zip(pts_steps, dts_steps))[:limit]:
        def location_text(location):
            return f"({location.get('decode_order_index')}, {location.get('pos') if location.get('pos') is not None else 'unknown'})"
        pts_value = "unknown" if pts_step["delta_ticks"] is None else f"{pts_step['delta_ticks']} / {pts_step['delta_seconds'] if pts_step['delta_seconds'] is not None else 'unknown'}"
        dts_value = "unknown" if dts_step["delta_ticks"] is None else f"{dts_step['delta_ticks']} / {dts_step['delta_seconds'] if dts_step['delta_seconds'] is not None else 'unknown'}"
        lines.append(f"| {location_text(pts_step['from'])} | {location_text(pts_step['to'])} | {pts_value} | {dts_value} |")
    if len(pts_steps) > limit:
        lines.append(f"| … | … | … | … | _(showing {limit} of {len(pts_steps)} transitions; full data is in JSON)_")
    return lines


def _frame_excerpt(evaluation, limit=40):
    frames = evaluation["decoder_frames"]["decoder_list_order"]
    if not frames:
        return ["No decoded frame records were provided."]
    lines = ["| Frame index | Packet position | Type | Key frame | PTS | Packet DTS | Best-effort timestamp |", "|---:|---:|:---:|:---:|---:|---:|---:|"]
    for frame in frames[:limit]:
        values = [frame.get("decoder_frame_index"), frame.get("pkt_pos"), frame.get("pict_type"), frame.get("key_frame"), frame.get("pts"), frame.get("pkt_dts"), frame.get("best_effort_timestamp")]
        lines.append("| " + " | ".join("unknown" if value is None else str(value) for value in values) + " |")
    if len(frames) > limit:
        lines.append(f"| … | … | … | … | … | … | … | _(showing {limit} of {len(frames)} frames; full data is in JSON)_")
    return lines


def _check_summary(name, check):
    missing = check["missing"]
    duplicate_groups = check["duplicates"]
    backward = check["backward_steps"]
    equal = check["equal_adjacent_steps"]
    lines = [f"- **{name}:** {len(missing)} missing; {len(duplicate_groups)} duplicate value group(s); {len(backward)} backward step(s); {len(equal)} equal adjacent step(s)."]
    if missing:
        lines.append(f"  - Missing locations: {_locations(missing)}.")
    for group in duplicate_groups:
        lines.append(f"  - Duplicate `{group['timestamp']}` at {_locations(group['locations'])}.")
    for step in backward:
        lines.append(f"  - Backward delta `{step['delta_ticks']}` ticks from {_locations([step['from']])} to {_locations([step['to']])}.")
    return lines


def _findings(evaluation):
    findings = evaluation["findings"]
    indicators = findings["strong_indicators"]
    expected = evaluation["reference"]["expected"] or "unknown"
    assessment = evaluation["reference"]["assessment"]
    lines = [
        f"- **Observed classification:** `{evaluation['observed_classification']}`.",
        f"- **Martin's reference:** `{expected}`; assessment: `{assessment}`.",
    ]
    if indicators:
        lines.append("- **Timestamp-integrity indicators:**")
        for item in indicators:
            lines.append(f"  - `{item['kind']}` — {item['detail']}; positions: {_locations(item['locations'])}.")
    else:
        lines.append("- **Timestamp-integrity indicators:** none detected in the available probe data.")
    backward_steps = findings["packet_pts_backward_steps_are_not_sufficient_alone"]
    b_locations = evaluation["decoder_frames"]["b_frame_locations"]
    if backward_steps:
        lines.append(f"- Stored packet PTS moves backward at {_locations([step['to'] for step in backward_steps])}; this alone is not classified as an error because it can reflect frame reordering.")
    if b_locations:
        lines.append(f"- B-frames occur at {_locations(b_locations)}; their presence alone is not classified as an error.")
    if findings["reordering_only_without_packet_integrity_anomaly"]:
        lines.append("- Reordering is observed without a separate stored-packet timestamp-integrity indicator.")
    if expected == "error_present" and assessment == "not_demonstrated_by_available_indicators":
        lines.append("- The error-present reference is not corroborated by strong timestamp-integrity indicators in this run; no error is inferred from B-frames or backward packet PTS alone.")
    elif expected != "unknown":
        lines.append(f"- The observed evidence {('supports' if assessment == 'supports_reference' else 'conflicts with')} the supplied reference classification.")
    lines.append("- Decoder `best_effort_timestamp` values remain separate; they do not replace original stored packet PTS/DTS evidence.")
    lines.append("- Packet/frame explicit PTS comparisons use exact integer ticks. The input does not provide a frame time base, so cross-record time-base equivalence remains unknown.")
    return lines


def _gop_excerpt(evaluation, limit=20):
    gops = evaluation["decoder_frames"]["gop_sequences"]
    if not gops:
        return "unknown"
    values = [f"GOP {group['gop_number']}: {group['sequence']}" for group in gops[:limit]]
    if len(gops) > limit:
        values.append(f"… ({len(gops) - limit} more GOPs in JSON)")
    return "; ".join(values)


def _order_excerpt(order, limit=40):
    values = [item["location"].get("decode_order_index", item["location"].get("decoder_frame_index")) for item in order[:limit]]
    if len(order) > limit:
        values.append(f"… ({len(order) - limit} more)")
    return values or "unknown"


def render_report(raw_clips, evaluations=None, generated_at=None):
    """Render one comparison report from raw probe results."""
    evaluations = evaluations or [evaluate_clip(raw) for raw in raw_clips]
    evaluation_by_file = {clip_name(item): item for item in evaluations}
    lines = [
        "# Reference Clip Analysis",
        "",
        f"Generated at: {generated_at or 'unknown'}",
        "",
        "> Read-only analysis. No input clip was modified and no repaired video was produced.",
        "",
        "## Comparison",
        "",
        "| Clip | Martin reference | Observed evidence | Assessment | Packets | Frames | B-frames |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for raw in raw_clips:
        name = clip_name(raw) or "unknown"
        evaluation = evaluation_by_file.get(name) or evaluate_clip(raw)
        lines.append(
            f"| `{name}` | {evaluation['reference']['expected'] or 'unknown'} | "
            f"{evaluation['observed_classification']} | {evaluation['reference']['assessment']} | "
            f"{len(raw.get('packets') or [])} | {len(raw.get('frames') or [])} | "
            f"{len(evaluation['decoder_frames']['b_frame_locations'])} |"
        )
    for raw in raw_clips:
        name = clip_name(raw) or "unknown"
        evaluation = evaluation_by_file.get(name) or evaluate_clip(raw)
        lines.extend([
            "",
            f"## `{name}`",
            "",
            f"- Source file: `{_source_file(raw)}`",
            f"- Analysis time: `{raw.get('analysed_at') or 'unknown'}`",
            "",
            "### Probe record",
            "",
            *_code_block("Tools", raw.get("tools", "unknown")),
            *_code_block("Commands", raw.get("commands", "unknown")),
            *_code_block("Diagnostics", raw.get("diagnostics", "unknown")),
            "### Technical inventory",
            "",
            *_inventory_block(raw.get("inventory")),
            "### Findings and reference classification",
            "",
            *_findings(evaluation),
            "",
            "### Stored packet timestamps (decode order)",
            "",
            "Packet PTS/DTS are original stored packet timestamps. Decode order follows the packet list supplied by the probe; presentation order is sorted by stored PTS. Deltas are in stream ticks, with seconds available in the JSON when time base is known.",
            "",
            *_packet_excerpt(evaluation),
            "",
            "Adjacent timestamp deltas in stored packet decode order:",
            "",
            *_delta_excerpt(evaluation),
            "",
            f"- Packet presentation order by stored PTS (decode-order indices): `{_order_excerpt(evaluation['stored_packets']['presentation_order_by_pts']['order'])}`.",
            "- **Stored timestamp checks:**",
            *_check_summary("Packet PTS", evaluation["stored_packets"]["pts_checks_in_decode_order"]),
            *_check_summary("Packet DTS", evaluation["stored_packets"]["dts_checks_in_decode_order"]),
            "",
            "### Decoder frame sequence and timestamps",
            "",
            f"- I/P/B sequence in decoder frame-list order: `{''.join(evaluation['decoder_frames']['pict_type_sequence'])[:160] or 'unknown'}`{(' …' if len(evaluation['decoder_frames']['pict_type_sequence']) > 160 else '')}.",
            f"- GOP sequence excerpts: {_gop_excerpt(evaluation)}.",
            f"- Decoder presentation order by best-effort timestamp (frame-list indices): `{_order_excerpt(evaluation['decoder_frames']['presentation_order_by_best_effort_timestamp']['order'])}`.",
            "",
            *_frame_excerpt(evaluation),
            "",
            "**Decoder timestamp checks (separate from packet checks):**",
        ])
        for field, check in evaluation["decoder_frames"]["timestamp_checks"].items():
            lines.extend(_check_summary(f"Decoder {field}", check))
        lines.extend([
            "",
            "### PTS−DTS and periodic patterns",
            "",
            "The packet excerpt above shows PTS−DTS at its listed positions. Periodic patterns are descriptive only; they are not by themselves classified as errors.",
            "",
            *_code_block("Stored packet periodic patterns (exact positions)", evaluation["stored_packets"]["periodic_patterns"]),
            *_code_block("Decoder timestamp periodic patterns (exact positions)", evaluation["decoder_frames"]["periodic_patterns"]),
            "### Unknowns",
            "",
            "Probe fields that are absent or not uniquely reconstructible are marked `unknown`. Export parameters are not inferred from the clip alone.",
            "",
            "Full unabridged probe data is saved in the corresponding JSON file.",
        ])
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(output_dir, raw_clips, evaluations=None, generated_at=None):
    """Write the Markdown comparison and unmodified per-clip probe JSON."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    evaluations = evaluations or [evaluate_clip(raw) for raw in raw_clips]
    report_path = output_dir / "report.md"
    report_path.write_text(render_report(raw_clips, evaluations, generated_at), encoding="utf-8")
    json_paths = []
    for raw in raw_clips:
        name = clip_name(raw) or "unknown"
        json_path = output_dir / f"{Path(name).stem}.json"
        json_path.write_text(_json(raw) + "\n", encoding="utf-8")
        json_paths.append(json_path)
    return report_path, json_paths
