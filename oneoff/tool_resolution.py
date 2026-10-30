"""Locate and validate the ffmpeg and ffprobe executables."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contracts import AppConfig

_CHECK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ToolResolution:
    """Resolved executable paths, version lines, and any actionable errors."""

    ffmpeg_path: Path | None
    ffprobe_path: Path | None
    ffmpeg_version: str | None
    ffprobe_version: str | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether both tools were found and passed their version checks."""
        return self.ffmpeg_path is not None and self.ffprobe_path is not None and not self.errors


def _bundled_candidates(exe_dir: Path, tool_name: str) -> list[Path]:
    suffixes = (".exe", "") if os.name == "nt" else ("", ".exe")
    candidates = [exe_dir / "res" / f"{tool_name}{suffix}" for suffix in suffixes]
    existing: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_file():
                existing.append(candidate)
        except OSError:
            continue
    return existing


def _tool_candidates(exe_dir: Path, tool_name: str) -> list[Path]:
    candidates = _bundled_candidates(exe_dir, tool_name)
    path_result = shutil.which(tool_name)
    if path_result:
        path_candidate = Path(path_result)
        if path_candidate not in candidates:
            candidates.append(path_candidate)
    return candidates


def _first_line(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _check_tool(exe_dir: Path, tool_name: str) -> tuple[Path | None, str | None, str | None]:
    candidates = _tool_candidates(exe_dir, tool_name)
    if not candidates:
        return None, None, f"{tool_name} was not found in {exe_dir / 'res'} or on PATH."

    failures: list[str] = []
    for candidate in candidates:
        try:
            result = subprocess.run(
                [str(candidate), "-version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_CHECK_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{candidate} could not be run: {exc}")
            continue

        if result.returncode == 0:
            version = _first_line(f"{result.stdout or ''}\n{result.stderr or ''}")
            return candidate, version, None

        detail = _first_line(result.stderr or "") or _first_line(result.stdout or "")
        message = f"{candidate} exited with status {result.returncode}"
        if detail:
            message = f"{message}: {detail}"
        failures.append(message)

    return None, None, f"{tool_name} was found but could not be run: {'; '.join(failures)}."


def resolve_tools(config: AppConfig) -> ToolResolution:
    """Find bundled tools first, then PATH, and check each with ``-version``."""
    ffmpeg_path, ffmpeg_version, ffmpeg_error = _check_tool(config.exe_dir, "ffmpeg")
    ffprobe_path, ffprobe_version, ffprobe_error = _check_tool(config.exe_dir, "ffprobe")
    errors = tuple(error for error in (ffmpeg_error, ffprobe_error) if error is not None)
    return ToolResolution(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version=ffprobe_version,
        errors=errors,
    )
