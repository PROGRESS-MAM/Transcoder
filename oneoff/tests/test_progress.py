from __future__ import annotations

import io
import time

from oneoff.analysis import probe
from oneoff.analysis.probe import _JsonArrayCounter, _progress_total
from oneoff.analysis.progress import (
    ProgressDisplay,
    estimate_remaining,
    format_progress_line,
)


def test_formats_estimated_progress_with_elapsed_and_eta() -> None:
    line = format_progress_line(2, 3, "clip.mov", "frames", 25, 100, 20, estimated=True)

    assert line == "clip 2/3 clip.mov | frames 25/~100 25% | elapsed 00:20 ETA 01:00"


def test_formats_known_total_and_completed_eta() -> None:
    line = format_progress_line(1, 3, "clip.mov", "packets", 8, 8, 5)

    assert line == "clip 1/3 clip.mov | packets 8/8 100% | elapsed 00:05 ETA 00:00"


def test_unknown_total_has_count_and_elapsed_without_percentage_or_eta() -> None:
    line = format_progress_line(1, 3, "clip.mov", "frames", 12, None, 65)

    assert line == "clip 1/3 clip.mov | frames 12/? | elapsed 01:05"
    assert "%" not in line
    assert "ETA" not in line


def test_eta_is_unavailable_without_a_measured_rate() -> None:
    assert estimate_remaining(0, 100, 10) is None
    assert estimate_remaining(25, None, 10) is None
    assert estimate_remaining(25, 100, 10) == 30


def test_progress_display_overwrites_and_clears_previous_text() -> None:
    stream = io.StringIO()
    display = ProgressDisplay(stream)
    display.update(1, 3, "long-clip-name.mov", "frames", 10, 100, True)
    display.update(1, 3, "clip.mov", "frames", 11, 100, True)
    display.finish_line()

    output = stream.getvalue()
    first_line, second_line = output.split("\r")[1:]
    second_line = second_line.rstrip("\n")
    expected_padding = max(0, len(first_line) - len(second_line))

    assert output.count("\r") == 2
    assert output.endswith(" " * expected_padding + "\n")


def test_progress_total_prefers_stream_counts_then_estimates() -> None:
    inventory = {
        "format": {"duration": "10"},
        "streams": [
            {
                "codec_type": "video",
                "nb_frames": "240",
                "nb_read_frames": "238",
                "nb_read_packets": "220",
                "avg_frame_rate": "24/1",
            },
        ],
    }

    assert _progress_total("frames", inventory) == (238, False)
    assert _progress_total("packets", inventory) == (220, False)


def test_progress_total_estimates_when_stream_counts_are_missing() -> None:
    inventory = {
        "format": {"duration": "10"},
        "streams": [
            {"codec_type": "video", "nb_frames": "240", "avg_frame_rate": "24/1"},
        ],
    }

    assert _progress_total("frames", inventory) == (240, False)
    assert _progress_total("packets", inventory) == (240, True)


def test_progress_total_can_be_unknown() -> None:
    inventory = {"streams": [{"codec_type": "video", "avg_frame_rate": "0/0"}]}

    assert _progress_total("frames", inventory) == (None, False)


def test_json_counter_counts_objects_across_chunks_and_ignores_braces_in_strings() -> None:
    counter = _JsonArrayCounter("frames")

    assert counter.feed('{"frames":[{"a":"}"') == 0
    assert counter.feed(',"nested":{"b":1}},{"a":2}]}') == 2
    assert counter.count == 2


class _SlowPipe(io.BytesIO):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__()
        self.chunks = iter(chunks)

    def read1(self, size: int = -1) -> bytes:
        chunk = next(self.chunks, b"")
        if chunk:
            time.sleep(0.26)
        return chunk


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _SlowPipe([b'{"packets":[{"a":1},', b'{"a":2}]}'])
        self.stderr = io.BytesIO()

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def test_streamed_probe_reports_progress_before_the_pass_finishes(monkeypatch) -> None:
    process = _FakeProcess()
    updates = []
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: process)

    payload = probe._run_json_stream(
        "packets", ["ffprobe"], {}, {}, "packets", 2, False,
        lambda *update: updates.append(update),
    )

    assert payload == {"packets": [{"a": 1}, {"a": 2}]}
    assert any(update[0] == "packets" and update[1] == 1 for update in updates)
    assert updates[-1][:3] == ("packets", 2, 2)
