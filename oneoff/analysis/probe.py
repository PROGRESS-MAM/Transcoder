"""Collect ffprobe data for one clip.

``ClipAnalysis.raw`` is a JSON-compatible mapping with ``schema_version``,
``source``, ``status`` (``complete``, ``partial``, or ``error``), ``tool``,
``tool_version``, ``arguments`` (argv by stage), ``metadata`` (format,
streams, and chapters), ``packets`` (stored video packet timestamps and
properties), ``decoder_frames`` (decoded-frame timestamps and properties),
and ``diagnostics``. A failed stage is described in diagnostics; unavailable
sections remain empty, so callers can distinguish missing evidence from a
healthy clip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_TIMEOUT_SECONDS = 600


def _executable_names() -> tuple[str, ...]:
    return ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe",)


def _resolve_ffprobe() -> str | None:
    roots: list[Path] = []
    frozen_dir = getattr(sys, "_MEIPASS", None)
    if frozen_dir:
        roots.append(Path(frozen_dir) / "res")
    try:
        roots.append(Path(sys.executable).resolve().parent / "res")
    except OSError:
        pass
    roots.extend((Path(__file__).resolve().parents[1] / "res", Path(__file__).resolve().parents[2] / "res"))

    for root in roots:
        for name in _executable_names():
            candidate = root / name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue

    for name in _executable_names():
        found = shutil.which(name)
        if found:
            return found
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run(argv: list[str]) -> tuple[int | None, str, str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return None, _text(exc.stdout), f"ffprobe timed out after {_TIMEOUT_SECONDS} seconds: {_text(exc.stderr)}".strip()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, "", f"Could not run ffprobe: {exc}"

    return result.returncode, _text(result.stdout), _text(result.stderr)


def _diagnostic(
    stage: str,
    message: str,
    *,
    returncode: int | None = None,
    stderr: str = "",
    severity: str = "error",
) -> dict[str, Any]:
    item: dict[str, Any] = {"stage": stage, "severity": severity, "message": message}
    if returncode is not None:
        item["returncode"] = returncode
    if stderr:
        item["stderr"] = stderr
    return item


def _probe_json(
    executable: str,
    source: str,
    stage: str,
    options: tuple[str, ...],
    arguments: dict[str, list[str]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any] | None:
    argv = [executable, "-v", "error", *options, "-of", "json", source]
    arguments[stage] = argv
    returncode, stdout, stderr = _run(argv)
    if returncode is None:
        diagnostics.append(_diagnostic(stage, stderr or "ffprobe could not complete the command"))
        return None
    if returncode != 0:
        diagnostics.append(_diagnostic(stage, "ffprobe rejected the clip", returncode=returncode, stderr=stderr))
        return None
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        diagnostics.append(_diagnostic(stage, f"ffprobe returned invalid JSON: {exc}", returncode=returncode, stderr=stderr))
        return None
    if not isinstance(value, dict):
        diagnostics.append(_diagnostic(stage, "ffprobe returned an unexpected JSON value", returncode=returncode, stderr=stderr))
        return None
    if stderr.strip():
        diagnostics.append(_diagnostic(stage, "ffprobe reported a warning", returncode=returncode, stderr=stderr, severity="warning"))
    return value


def _empty_raw(source: str, status: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "source": source,
        "status": status,
        "tool": "ffprobe",
        "tool_version": None,
        "arguments": {},
        "metadata": {"format": {}, "streams": [], "chapters": []},
        "packets": [],
        "decoder_frames": [],
        "diagnostics": diagnostics,
    }


def _collect_tool_version(
    executable: str,
    raw: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> None:
    version_argv = [executable, "-version"]
    raw["arguments"]["version"] = version_argv
    version_code, version_output, version_error = _run(version_argv)
    if version_code == 0:
        version_lines = version_output.splitlines()
        raw["tool_version"] = version_lines[0] if version_lines else ""
        if version_error.strip():
            diagnostics.append(
                _diagnostic(
                    "version",
                    "ffprobe reported a warning",
                    returncode=version_code,
                    stderr=version_error,
                    severity="warning",
                )
            )
        return
    if version_code is None:
        diagnostics.append(
            _diagnostic("version", version_error or "Could not determine ffprobe version")
        )
        return
    diagnostics.append(
        _diagnostic(
            "version",
            "Could not determine ffprobe version",
            returncode=version_code,
            stderr=version_error,
        )
    )


def _validated_metadata_sections(
    metadata: dict[str, Any], diagnostics: list[dict[str, Any]]
) -> dict[str, Any] | None:
    required = ("format", "streams", "chapters")
    missing = [key for key in required if key not in metadata]
    if missing:
        diagnostics.append(
            _diagnostic(
                "metadata",
                f"ffprobe metadata is missing required sections: {', '.join(missing)}",
            )
        )
        return None

    sections = {key: metadata[key] for key in required}
    if not isinstance(sections["format"], dict) or not isinstance(sections["streams"], list) or not isinstance(sections["chapters"], list):
        diagnostics.append(
            _diagnostic("metadata", "ffprobe metadata has an unexpected structure")
        )
        return None
    return sections


def _collect_metadata(
    executable: str,
    source: str,
    raw: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> bool:
    metadata = _probe_json(
        executable,
        source,
        "metadata",
        ("-show_format", "-show_streams", "-show_chapters"),
        raw["arguments"],
        diagnostics,
    )
    if metadata is None:
        return False
    sections = _validated_metadata_sections(metadata, diagnostics)
    if sections is None:
        return False
    raw["metadata"] = sections
    return True


def _store_video_section(
    stage: str,
    response_key: str,
    result_key: str,
    data: dict[str, Any] | None,
    raw: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> bool:
    if data is None:
        return False
    if response_key not in data:
        diagnostics.append(
            _diagnostic(
                stage,
                f"ffprobe data is missing the required {response_key} section",
            )
        )
        return False
    section = data[response_key]
    if not isinstance(section, list):
        diagnostics.append(
            _diagnostic(stage, f"ffprobe {response_key} data has an unexpected structure")
        )
        return False
    raw[result_key] = section
    return True


def _collect_video_sections(
    executable: str,
    source: str,
    raw: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> bool:
    sections = (
        ("packets", "packets", "packets", ("-select_streams", "v", "-show_packets")),
        ("decoder_frames", "frames", "decoder_frames", ("-select_streams", "v", "-show_frames")),
    )
    results = []
    for stage, response_key, result_key, options in sections:
        data = _probe_json(executable, source, stage, options, raw["arguments"], diagnostics)
        results.append((stage, response_key, result_key, data))

    failed = False
    for stage, response_key, result_key, data in results:
        if not _store_video_section(
            stage, response_key, result_key, data, raw, diagnostics
        ):
            failed = True
    return failed

def collect_clip(source_path: Path) -> dict[str, Any]:
    """Return complete or explicitly incomplete ffprobe evidence for a clip."""
    try:
        source = str(Path(source_path))
    except (TypeError, ValueError, OSError) as exc:
        return _empty_raw(
            str(source_path),
            "error",
            [_diagnostic("input", f"Invalid clip path: {exc}")],
        )

    executable = _resolve_ffprobe()
    if executable is None:
        return _empty_raw(
            source,
            "error",
            [_diagnostic("resolve", "ffprobe was not found in bundled resources or PATH")],
        )

    diagnostics: list[dict[str, Any]] = []
    raw = _empty_raw(source, "error", diagnostics)
    _collect_tool_version(executable, raw, diagnostics)
    if not _collect_metadata(executable, source, raw, diagnostics):
        raw["status"] = "error"
        return raw

    failed = not bool(raw["tool_version"])
    if _collect_video_sections(executable, source, raw, diagnostics):
        failed = True
    raw["status"] = "partial" if failed else "complete"
    return raw
