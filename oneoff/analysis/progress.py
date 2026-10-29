"""Format and render one-line analysis progress updates."""

from __future__ import annotations

import sys
import time
from typing import TextIO


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def estimate_remaining(current: int, total: int | None, elapsed: float) -> float | None:
    if total is None or total <= 0 or current <= 0:
        return None
    if current >= total:
        return 0.0
    return max(0.0, elapsed) * (total - current) / current


def format_progress_line(
    clip_index: int,
    clip_total: int,
    filename: str,
    phase: str,
    current: int,
    total: int | None,
    elapsed: float,
    estimated: bool = False,
) -> str:
    current = max(0, current)
    elapsed_text = format_elapsed(elapsed)
    if total is None or total <= 0:
        return (
            f"clip {clip_index}/{clip_total} {filename} | {phase} "
            f"{current}/? | elapsed {elapsed_text}"
        )

    marker = "~" if estimated else ""
    percentage = min(100, int(current * 100 / total))
    remaining = estimate_remaining(current, total, elapsed)
    eta_text = format_elapsed(remaining) if remaining is not None else "--:--"
    return (
        f"clip {clip_index}/{clip_total} {filename} | {phase} "
        f"{current}/{marker}{total} {percentage}% | elapsed {elapsed_text} ETA {eta_text}"
    )


class ProgressDisplay:
    """Render progress updates in place, clearing any longer previous line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self._line_length = 0
        self._phase_started: float | None = None
        self._last_clip: tuple[int, str] | None = None
        self._last_phase: str | None = None

    def update(
        self,
        clip_index: int,
        clip_total: int,
        filename: str,
        phase: str,
        current: int,
        total: int | None,
        estimated: bool = False,
    ) -> None:
        clip = (clip_index, filename)
        if clip != self._last_clip or phase != self._last_phase:
            self._phase_started = time.monotonic()
            self._last_clip = clip
            self._last_phase = phase
        elapsed = time.monotonic() - (self._phase_started or time.monotonic())
        line = format_progress_line(
            clip_index,
            clip_total,
            filename,
            phase,
            current,
            total,
            elapsed,
            estimated,
        )
        self.stream.write("\r" + line + " " * max(0, self._line_length - len(line)))
        self.stream.flush()
        self._line_length = len(line)

    def finish_line(self) -> None:
        if self._line_length:
            self.stream.write("\n")
            self.stream.flush()
            self._line_length = 0
            self._phase_started = None
            self._last_clip = None
            self._last_phase = None
