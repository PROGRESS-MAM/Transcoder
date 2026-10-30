"""Command-line entry point for ``python -m oneoff``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config, resolve_exe_dir
from .pipeline import PipelineError, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze and repair one-off proxy clips.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="analyze clips and report proposed actions without writing files",
    )
    args = parser.parse_args(argv)
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    executable = Path(sys.executable) if getattr(sys, "frozen", False) else None
    exe_dir = resolve_exe_dir(Path(__file__), executable)

    try:
        config = load_config(exe_dir)
        result = run_pipeline(config, dry_run=args.dry_run, argv=arguments)
    except (ConfigError, PipelineError, OSError, ValueError) as exc:
        print(f"One-Off-Lauf fehlgeschlagen: {exc}", file=sys.stderr)
        if not args.dry_run and isinstance(exc, PipelineError) and exc.report is not None:
            print(f"Protokoll: {exc.report.markdown_path}", file=sys.stderr)
        return 2

    if not args.dry_run:
        print(f"Protokoll: {result.report.markdown_path}")
    failure_statuses = {
        "analysis_error",
        "repair_error",
        "verification_error",
        "publication_error",
    }
    return 1 if any(result.summary.counts.get(status, 0) for status in failure_statuses) else 0


if __name__ == "__main__":
    raise SystemExit(main())
