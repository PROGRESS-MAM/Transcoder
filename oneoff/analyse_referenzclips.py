"""Read-only CLI for analysing the three specified proxy reference clips."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from oneoff.analysis.evaluate import REFERENCE_LABELS, evaluate_clip
from oneoff.analysis.report import write_outputs

CLIP_NAMES = tuple(REFERENCE_LABELS)


def _collector(path):
    from oneoff.analysis.probe import collect_clip

    return collect_clip(path)


def _create_run_directory(output_root, timestamp):
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = "analysis_" + timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 0
    while True:
        name = base_name if suffix == 0 else f"{base_name}_{suffix}"
        candidate = output_root / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            suffix += 1


def main(argv=None, collector=None):
    parser = argparse.ArgumentParser(description="Read-only analysis of the three reference clips.")
    parser.add_argument("clip_folder", type=Path, help="folder containing the original reference clips")
    parser.add_argument("--output-folder", type=Path, help="base folder for timestamped report runs")
    args = parser.parse_args(argv)

    if not args.clip_folder.is_dir():
        print(f"Clip folder does not exist or is not a directory: {args.clip_folder}", file=sys.stderr)
        return 2
    clip_paths = [args.clip_folder / name for name in CLIP_NAMES]
    missing = [path.name for path in clip_paths if not path.is_file()]
    if missing:
        print("Missing required clip(s): " + ", ".join(missing), file=sys.stderr)
        return 2

    clip_collector = collector or _collector
    raw_clips = []
    for clip_path in clip_paths:
        try:
            raw_clips.append(clip_collector(clip_path))
        except Exception as error:
            print(f"Could not analyse {clip_path.name}: {error}", file=sys.stderr)
            return 1

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_root = args.output_folder or args.clip_folder / "analysis_reports"
    try:
        run_dir = _create_run_directory(output_root, datetime.now(timezone.utc))
    except OSError as error:
        print(f"Could not create analysis output folder: {error}", file=sys.stderr)
        return 1
    try:
        evaluations = [evaluate_clip(raw) for raw in raw_clips]
        write_outputs(run_dir, raw_clips, evaluations, generated_at)
    except Exception as error:
        for child in run_dir.iterdir():
            if child.is_file():
                child.unlink()
        run_dir.rmdir()
        print(f"Could not write analysis report: {error}", file=sys.stderr)
        return 1
    print(f"Analysis complete: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
