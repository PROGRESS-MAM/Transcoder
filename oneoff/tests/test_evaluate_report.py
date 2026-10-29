import copy
import json
from pathlib import Path

from oneoff.analysis.evaluate import evaluate_clip
from oneoff.analysis.report import render_report, write_outputs
from oneoff import analyse_referenzclips


def clean_clip(name="53287289.mp4"):
    positions = [100, 200, 300, 400]
    return {
        "file": name,
        "analysed_at": "2026-10-29T12:00:00Z",
        "tools": {"ffprobe": "7.1"},
        "commands": ["ffprobe -show_packets -show_frames"],
        "diagnostics": [],
        "inventory": {
            "container": "mov,mp4,m4a,3gp,3g2,mj2",
            "streams": [{"codec_name": "h264", "width": 1920, "profile": None}],
            "unknown_parameter": None,
        },
        "packets": [
            {"index": index, "pts": index * 40, "dts": index * 40, "duration": 40,
             "pos": position, "size": 1000, "flags": "K_" if index == 0 else "__", "time_base": "1/1000"}
            for index, position in enumerate(positions)
        ],
        "frames": [
            {"pict_type": "I" if index == 0 else "P", "key_frame": 1 if index == 0 else 0,
             "pts": index * 40, "pkt_dts": index * 40, "best_effort_timestamp": index * 40,
             "duration": 40, "pkt_pos": position, "coded_picture_number": index,
             "display_picture_number": index}
            for index, position in enumerate(positions)
        ],
    }


def error_clip(name="58238241.mp4"):
    raw = clean_clip(name)
    pts = [0, 2, 1, 3, 3]
    dts = [0, 1, 2, 1, 4]
    positions = [100, 200, 300, 400, 500]
    raw["packets"] = [
        {"index": index, "pts": pts[index], "dts": dts[index], "duration": 1,
         "pos": positions[index], "size": 900, "flags": "K_" if index == 0 else "__", "time_base": "1/1000"}
        for index in range(len(positions))
    ]
    frame_order = [0, 2, 1, 3, 4]
    picture_types = ["I", "B", "P", "P", "B"]
    raw["frames"] = [
        {"pict_type": picture_types[index], "key_frame": 1 if index == 0 else 0,
         "pts": pts[packet_index], "pkt_dts": dts[packet_index],
         "best_effort_timestamp": pts[packet_index], "duration": 1,
         "pkt_pos": positions[packet_index], "coded_picture_number": index,
         "display_picture_number": packet_index}
        for index, packet_index in enumerate(frame_order)
    ]
    return raw


def test_error_and_clean_clips_are_separated_with_positions():
    bad = evaluate_clip(error_clip())
    clean = evaluate_clip(clean_clip())

    assert bad["observed_classification"] == "timestamp_anomaly_indicators_found"
    assert bad["reference"]["assessment"] == "supports_reference"
    assert any(item["kind"] == "non_monotonic_packet_dts" for item in bad["findings"]["strong_indicators"])
    assert bad["stored_packets"]["dts_checks_in_decode_order"]["backward_steps"][0]["to"]["pos"] == 400
    duplicate = bad["stored_packets"]["pts_checks_in_decode_order"]["duplicates"][0]
    assert duplicate["timestamp"] == 3
    assert [location["pos"] for location in duplicate["locations"]] == [400, 500]
    assert bad["decoder_frames"]["b_frame_locations"]

    assert clean["observed_classification"] == "no_timestamp_anomaly_indicators_detected"
    assert clean["reference"]["assessment"] == "supports_reference"
    assert clean["decoder_frames"]["b_frame_locations"] == []


def test_b_frames_and_backward_packet_pts_alone_are_not_error():
    raw = error_clip()
    raw["file"] = "58238527.mp4"
    raw["packets"] = [
        {"index": i, "pts": pts, "dts": dts, "duration": 1, "pos": 100 + i * 100,
         "size": 800, "flags": "K_" if i == 0 else "__", "time_base": "1/1000"}
        for i, (pts, dts) in enumerate([(0, 0), (2, 1), (1, 2), (3, 3)])
    ]
    raw["frames"] = [
        {"pict_type": picture_type, "key_frame": 1 if i == 0 else 0, "pts": pts,
         "pkt_dts": dts, "best_effort_timestamp": pts, "duration": 1,
         "pkt_pos": position}
        for i, (picture_type, pts, dts, position) in enumerate([
            ("I", 0, 0, 100), ("B", 1, 2, 300), ("P", 2, 1, 200), ("P", 3, 3, 400)
        ])
    ]

    result = evaluate_clip(raw)
    assert result["findings"]["stored_packet_timestamp_anomaly_detected"] is False
    assert result["findings"]["reordering_only_without_packet_integrity_anomaly"] is True
    assert result["reference"]["assessment"] == "not_demonstrated_by_available_indicators"


def test_missing_packet_timestamps_are_reported_at_positions():
    raw = clean_clip("58238241.mp4")
    raw["packets"][2]["pts"] = None
    raw["packets"][2]["dts"] = None

    result = evaluate_clip(raw)
    indicators = result["findings"]["strong_indicators"]
    assert result["stored_packets"]["pts_checks_in_decode_order"]["missing"][0]["pos"] == 300
    assert result["stored_packets"]["dts_checks_in_decode_order"]["missing"][0]["pos"] == 300
    assert sum(item["kind"] == "missing_packet_timestamp" for item in indicators) == 2


def test_explicit_packet_frame_pts_contradiction_is_reported():
    raw = clean_clip("58238241.mp4")
    raw["frames"][2]["pts"] = 999

    result = evaluate_clip(raw)
    mismatch = next(item for item in result["findings"]["strong_indicators"] if item["kind"] == "packet_decoder_pts_mismatch")
    assert "packet PTS=80" in mismatch["detail"]
    assert "decoder frame PTS=999" in mismatch["detail"]
    assert mismatch["locations"][0]["pos"] == 300


def test_periodic_pattern_keeps_exact_packet_positions():
    raw = clean_clip("58238241.mp4")
    offsets = [0, 1, 2, 0, 1, 2]
    raw["packets"] = [
        {"index": index, "pts": index * 10, "dts": index * 10 - offset, "duration": 10,
         "pos": 100 + index * 50, "size": 100, "flags": "__", "time_base": "1/1000"}
        for index, offset in enumerate(offsets)
    ]
    result = evaluate_clip(raw)
    patterns = [pattern for pattern in result["stored_packets"]["periodic_patterns"] if pattern["field"] == "packet PTS-DTS"]
    assert patterns
    assert patterns[0]["period_length"] == 3
    assert patterns[0]["repetitions"] == 2
    assert [location["pos"] for location in patterns[0]["locations"]] == [100, 150, 200, 250, 300, 350]


def test_report_and_raw_json_are_written(tmp_path):
    error = error_clip()
    clean = clean_clip()
    report_path, json_paths = write_outputs(tmp_path, [error, clean], generated_at="2026-10-29T12:01:00Z")

    report = report_path.read_text(encoding="utf-8")
    assert "58238241.mp4" in report
    assert "53287289.mp4" in report
    assert "supports_reference" in report
    assert "pos=400" in report
    assert "not classified as an error" in report
    assert len(json_paths) == 2
    assert json.loads((tmp_path / "58238241.json").read_text(encoding="utf-8")) == error
    assert json.loads((tmp_path / "53287289.json").read_text(encoding="utf-8")) == clean
    assert "unknown" in report


def test_cli_creates_a_new_timestamped_folder_each_run(tmp_path, capsys):
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()
    for name in analyse_referenzclips.CLIP_NAMES:
        (clip_dir / name).write_bytes(b"fixture")
    fixtures = {
        "58238241.mp4": error_clip("58238241.mp4"),
        "58238527.mp4": error_clip("58238527.mp4"),
        "53287289.mp4": clean_clip("53287289.mp4"),
    }
    output_dir = tmp_path / "reports"
    call_count = 0

    def collector(path):
        nonlocal call_count
        call_count += 1
        result = copy.deepcopy(fixtures[path.name])
        result["tools"]["run_number"] = 1 if call_count <= 3 else 2
        return result

    assert analyse_referenzclips.main([str(clip_dir), "--output-folder", str(output_dir)], collector) == 0
    first_message = capsys.readouterr().out.strip()
    assert analyse_referenzclips.main([str(clip_dir), "--output-folder", str(output_dir)], collector) == 0
    second_message = capsys.readouterr().out.strip()

    first_dir = Path(first_message.removeprefix("Analysis complete: "))
    second_dir = Path(second_message.removeprefix("Analysis complete: "))
    assert first_dir != second_dir
    assert (first_dir / "report.md").is_file()
    assert (second_dir / "report.md").is_file()
    assert sorted(path.name for path in first_dir.glob("*.json")) == ["53287289.json", "58238241.json", "58238527.json"]
    first_raw = json.loads((first_dir / "53287289.json").read_text(encoding="utf-8"))
    second_raw = json.loads((second_dir / "53287289.json").read_text(encoding="utf-8"))
    assert first_raw["tools"]["run_number"] == 1
    assert second_raw["tools"]["run_number"] == 2
    assert call_count == 6


def test_cli_reports_missing_clip_clearly(tmp_path, capsys):
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()
    for name in analyse_referenzclips.CLIP_NAMES[:-1]:
        (clip_dir / name).write_bytes(b"fixture")
    called = []

    result = analyse_referenzclips.main([str(clip_dir), "--output-folder", str(tmp_path / "reports")], lambda path: called.append(path))
    captured = capsys.readouterr()

    assert result == 2
    assert "Missing required clip(s): 53287289.mp4" in captured.err
    assert called == []
    assert not (tmp_path / "reports").exists()


def test_collected_file_metadata_is_consumed(tmp_path):
    raw = clean_clip("58238241.mp4")
    raw["file"] = {
        "name": "58238241.mp4",
        "abs_path": "/synthetic/58238241.mp4",
        "size": 1234,
    }

    evaluation = evaluate_clip(raw)
    assert evaluation["reference"]["expected"] == "error_present"

    report = render_report([raw], [evaluation], "2026-10-29T12:00:00Z")
    assert "| `58238241.mp4` | error_present |" in report
    assert "- Source file: `/synthetic/58238241.mp4`" in report

    report_path, json_paths = write_outputs(tmp_path, [raw], [evaluation])
    assert report_path.name == "report.md"
    assert [path.name for path in json_paths] == ["58238241.json"]
    assert json.loads(json_paths[0].read_text())["file"] == raw["file"]
