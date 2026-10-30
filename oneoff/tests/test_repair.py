from fractions import Fraction
from pathlib import Path

import pytest

from oneoff.contracts import Candidate, ClipAnalysis, CommandResult, Diagnosis
from oneoff.repair import RepairError, encode_repair


def make_clip(tmp_path: Path) -> tuple[ClipAnalysis, Diagnosis, Path]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source remains unchanged")
    target = tmp_path / "fixed" / source.name
    target.parent.mkdir()
    clip = ClipAnalysis(
        Candidate(source, target),
        {
            "streams": [
                {
                    "index": 0,
                    "codec_name": "h264",
                    "codec_type": "video",
                    "width": 2048,
                    "height": 1080,
                    "sample_aspect_ratio": "1:1",
                    "pix_fmt": "yuv420p",
                    "bit_rate": "1200000",
                    "color_range": "tv",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                    "color_space": "bt709",
                    "tags": {"language": "und"},
                },
                {
                    "index": 1,
                    "codec_name": "aac",
                    "codec_type": "audio",
                    "tags": {"language": "eng"},
                },
                {"index": 2, "codec_name": "tmcd", "codec_type": "data"},
            ],
            "chapters": [{"id": 0}],
        },
    )
    diagnosis = Diagnosis("confirmed", "known case", {}, Fraction(24000, 1001), 28089, True)
    part_path = target.with_name(target.name + ".candidate.part")
    return clip, diagnosis, part_path


def fake_success(argv, progress_callback):
    Path(argv[-1]).write_bytes(b"encoded candidate")
    return CommandResult(argv, 0, "frame=28089 bitrate=1180.4kbits/s")


def test_encodes_only_confirmed_case_to_new_candidate(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    original = clip.candidate.source_path.read_bytes()
    captured = {}

    def runner(argv, callback):
        captured["argv"] = argv
        return fake_success(argv, callback)

    artifact = encode_repair(clip, diagnosis, part_path, runner)
    args = captured["argv"]

    assert artifact.part_path == part_path
    assert artifact.expected_video_frames == 28089
    assert part_path.read_bytes() == b"encoded candidate"
    assert clip.candidate.source_path.read_bytes() == original
    assert args[args.index("-i") + 1] == str(clip.candidate.source_path)
    assert args[-1] == str(part_path)
    assert "-n" in args and "-y" not in args
    assert args[args.index("-map") + 1] == "0"
    assert args[args.index("-c:v:0") + 1] == "libx264"
    assert args[args.index("-bf:v:0") + 1] == "0"
    assert args[args.index("-b:v:0") + 1] == "1200000"
    assert args[args.index("-pix_fmt:v:0") + 1] == "yuv420p"
    assert args[args.index("-vf:v:0") + 1] == (
        "settb=expr=1001/24000,setpts=N,setsar=1/1"
    )
    assert args[args.index("-fps_mode:v:0") + 1] == "passthrough"
    assert args[args.index("-enc_time_base:v:0") + 1] == "1001/24000"
    assert "-map_chapters" in args and "0" in args[args.index("-map_chapters") + 1]
    assert "-copy_unknown" in args
    assert args[args.index("-c:a") + 1] == "copy"
    assert "-copyts" in args and "-avoid_negative_ts" in args
    assert "-stats" in args
    assert "bitrate=1180.4kbits/s" in artifact.command_result.stderr
    assert "-shortest" not in args and "-r" not in args and "-t" not in args
    assert "-color_primaries:v:0" in args and "bt709" in args
    assert "-color_trc:v:0" in args and "-colorspace:v:0" in args
    assert "-map_metadata:s:a:0" in args and "0:1" in args
    assert "-map_metadata:s:d:0" in args and "0:2" in args


def test_rejects_unconfirmed_diagnosis_without_calling_runner(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    diagnosis = Diagnosis("unclear", "not proven", {}, Fraction(25), 100, False)

    def runner(*_):
        pytest.fail("runner must not be called")

    with pytest.raises(RepairError, match="Only confirmed"):
        encode_repair(clip, diagnosis, part_path, runner)
    assert not part_path.exists()


@pytest.mark.parametrize("bad_part", ["wrong.mp4", "existing.part"])
def test_rejects_non_candidate_or_existing_output(tmp_path, bad_part):
    clip, diagnosis, part_path = make_clip(tmp_path)
    if bad_part == "wrong.mp4":
        part_path = part_path.with_name(bad_part)
        expected = "must be a .part"
    else:
        part_path.write_bytes(b"keep me")
        expected = "already exists"

    with pytest.raises(RepairError, match=expected):
        encode_repair(clip, diagnosis, part_path, fake_success)
    if bad_part == "existing.part":
        assert part_path.read_bytes() == b"keep me"


def test_rejects_missing_source_bitrate_and_keeps_source(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    clip.raw["streams"][0].pop("bit_rate")
    original = clip.candidate.source_path.read_bytes()

    with pytest.raises(RepairError, match="source video bitrate"):
        encode_repair(clip, diagnosis, part_path, fake_success)
    assert clip.candidate.source_path.read_bytes() == original
    assert not part_path.exists()


def test_rejects_ffmpeg_failure_and_does_not_publish(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)

    def failed_runner(argv, _callback):
        Path(argv[-1]).write_bytes(b"incomplete")
        return CommandResult(argv, 1, "encoding error")

    with pytest.raises(RepairError, match="exit code 1"):
        encode_repair(clip, diagnosis, part_path, failed_runner)
    assert clip.candidate.source_path.read_bytes() == b"source remains unchanged"
    assert not clip.candidate.target_path.exists()


def test_success_without_candidate_file_is_rejected(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    no_file = lambda argv, _callback: CommandResult(argv, 0, "")

    with pytest.raises(RepairError, match="did not create"):
        encode_repair(clip, diagnosis, part_path, no_file)
    assert not clip.candidate.target_path.exists()


def test_rejects_multiple_video_streams_conservatively(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    clip.raw["streams"].append({"index": 3, "codec_type": "video", "codec_name": "h264"})

    with pytest.raises(RepairError, match="exactly one"):
        encode_repair(clip, diagnosis, part_path, fake_success)


def test_rejects_cover_video_stream_to_avoid_reencoding_wrong_track(tmp_path):
    clip, diagnosis, part_path = make_clip(tmp_path)
    clip.raw["streams"].append(
        {
            "index": 3,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
        }
    )

    with pytest.raises(RepairError, match="exactly one video stream"):
        encode_repair(clip, diagnosis, part_path, fake_success)
