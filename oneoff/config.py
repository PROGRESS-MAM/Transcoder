"""External configuration and run-mode policy for the one-off repair tool."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from .contracts import AppConfig, RunPolicy


class ConfigError(ValueError):
    """Raised when the external configuration is missing or unusable."""


_TEST_CANDIDATES = frozenset({"58238241.mp4", "58238527.mp4"})
_REQUIRED_SECTIONS = {"paths", "repair"}
_REQUIRED_OPTIONS = {
    "paths": {"source_path", "target_path"},
    "repair": {"repair-test"},
}


def resolve_exe_dir(script_file: Path, executable: Path | None = None) -> Path:
    """Return the directory of the frozen executable or source script."""
    entry_path = Path(executable) if executable is not None else Path(script_file)
    return entry_path.resolve().parent


def _fail(message: str, cause: Exception | None = None) -> ConfigError:
    error = ConfigError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _parse_config(config_path: Path) -> configparser.ConfigParser:
    if not config_path.is_file():
        raise _fail(f"Configuration file is missing: {config_path}")

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with config_path.open("r", encoding="utf-8-sig") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise _fail(f"Cannot read valid configuration file {config_path}: {exc}", exc)

    sections = set(parser.sections())
    if sections != _REQUIRED_SECTIONS:
        missing = sorted(_REQUIRED_SECTIONS - sections)
        extra = sorted(sections - _REQUIRED_SECTIONS)
        details = []
        if missing:
            details.append(f"missing section(s): {', '.join(missing)}")
        if extra:
            details.append(f"unknown section(s): {', '.join(extra)}")
        raise _fail("Invalid configuration: " + "; ".join(details))
    if parser.defaults():
        raise _fail("Invalid configuration: DEFAULT values are not allowed")

    for section, required in _REQUIRED_OPTIONS.items():
        actual = set(parser[section].keys())
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        details = []
        if missing:
            details.append(f"missing option(s): {', '.join(missing)}")
        if extra:
            details.append(f"unknown option(s): {', '.join(extra)}")
        if details:
            raise _fail(f"Invalid [{section}] section: " + "; ".join(details))
    return parser


def _configured_path(value: str, exe_dir: Path, label: str) -> Path:
    if not value.strip():
        raise _fail(f"Invalid configuration: {label} must not be empty")
    try:
        path = Path(value.strip())
        if not path.is_absolute():
            path = exe_dir / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(f"Invalid {label} path: {value!r}: {exc}", exc)


def _same_directory(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)).casefold() == os.path.normcase(str(second)).casefold()


def _require_directory(path: Path, label: str, access: int) -> None:
    try:
        is_directory = path.is_dir()
    except OSError as exc:
        raise _fail(f"Cannot access {label} directory {path}: {exc}", exc)
    if not is_directory:
        raise _fail(f"{label.capitalize()} directory does not exist or is not a directory: {path}")
    if not os.access(path, access):
        action = "read" if access & os.R_OK else "write"
        raise _fail(f"{label.capitalize()} directory is not usable for {action}: {path}")


def _require_report_location(reports_dir: Path) -> None:
    if reports_dir.is_symlink() and not reports_dir.exists():
        raise _fail(f"Report directory is a broken symbolic link: {reports_dir}")
    if reports_dir.exists():
        _require_directory(reports_dir, "report", os.R_OK | os.W_OK | os.X_OK)
        return

    parent = reports_dir.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    _require_directory(parent, "report parent", os.W_OK | os.X_OK)


def load_config(exe_dir: Path) -> AppConfig:
    """Load and validate config.ini next to the executable, without writes."""
    try:
        resolved_exe_dir = Path(exe_dir).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(f"Invalid executable directory {exe_dir}: {exc}", exc)
    _require_directory(resolved_exe_dir, "executable", os.R_OK | os.X_OK)

    parser = _parse_config(resolved_exe_dir / "config.ini")
    source_dir = _configured_path(parser["paths"]["source_path"], resolved_exe_dir, "source")
    target_dir = _configured_path(parser["paths"]["target_path"], resolved_exe_dir, "target")
    repair_value = parser["repair"]["repair-test"].strip()
    if repair_value not in {"true", "false"}:
        raise _fail("Invalid [repair] repair-test value: expected exactly 'true' or 'false'")
    if _same_directory(source_dir, target_dir):
        raise _fail(f"Source and target directories must be different: {source_dir}")

    _require_directory(source_dir, "source", os.R_OK | os.X_OK)
    _require_directory(target_dir, "target", os.R_OK | os.W_OK | os.X_OK)
    reports_dir = resolved_exe_dir / "reports"
    _require_report_location(reports_dir)

    return AppConfig(
        exe_dir=resolved_exe_dir,
        source_dir=source_dir,
        target_dir=target_dir,
        reports_dir=reports_dir,
        repair_test=repair_value == "true",
    )


def build_policy(config: AppConfig) -> RunPolicy:
    """Build the immutable candidate and repair policy for the selected mode."""
    if config.repair_test:
        return RunPolicy(
            mode="test",
            candidate_names=_TEST_CANDIDATES,
            repair_confirmed_only=True,
        )
    return RunPolicy(mode="series", candidate_names=None, repair_confirmed_only=True)
