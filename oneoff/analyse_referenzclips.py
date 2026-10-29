"""Read-only CLI for analysing the three specified proxy reference clips."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIP_FOLDER = Path(r"\\10.0.77.102\FiMu 1.5 Mbit Proxy")
OUTPUT_FOLDER = PROJECT_ROOT / "oneoff" / "analysis_reports"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from oneoff.analysis.evaluate import REFERENCE_LABELS, evaluate_clip
from oneoff.analysis.report import write_outputs
from oneoff.analysis.progress import ProgressDisplay

CLIP_NAMES = tuple(REFERENCE_LABELS)


def _collector(path, progress=None):
    from oneoff.analysis.probe import collect_clip

    return collect_clip(path, progress=progress)


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
    parser.add_argument(
        "clip_folder",
        nargs="?",
        type=Path,
        default=CLIP_FOLDER,
        help="folder containing the original reference clips",
    )
    parser.add_argument(
        "--output-folder",
        type=Path,
        default=OUTPUT_FOLDER,
        help="base folder for timestamped report runs",
    )
    args = parser.parse_args(argv)

    try:
        if not args.clip_folder.is_dir():
            print(f"Clip folder is unavailable or is not a directory: {args.clip_folder}", file=sys.stderr)
            return 2
        clip_paths = [args.clip_folder / name for name in CLIP_NAMES]
        missing = [path.name for path in clip_paths if not path.is_file()]
    except OSError:
        print(f"Clip folder is unavailable or not readable: {args.clip_folder}", file=sys.stderr)
        return 2
    if missing:
        print("Missing required clip(s): " + ", ".join(missing), file=sys.stderr)
        return 2

    progress = ProgressDisplay() if collector is None else None
    raw_clips = []
    for clip_index, clip_path in enumerate(clip_paths, start=1):
        try:
            if collector is None:
                def update(phase, current, total, estimated=False):
                    if progress:
                        progress.update(
                            clip_index,
                            len(clip_paths),
                            clip_path.name,
                            phase,
                            current,
                            total,
                            estimated,
                        )

                raw_clips.append(_collector(clip_path, progress=update))
            else:
                raw_clips.append(collector(clip_path))
        except Exception as error:
            if progress:
                progress.finish_line()
            print(f"Could not analyse {clip_path.name}: {error}", file=sys.stderr)
            return 1

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        run_dir = _create_run_directory(args.output_folder, datetime.now(timezone.utc))
    except OSError as error:
        print(f"Could not create analysis output folder: {error}", file=sys.stderr)
        return 1
    try:
        if progress:
            progress.update(1, len(raw_clips), CLIP_NAMES[0], "evaluation", 0, len(raw_clips))
        evaluations = []
        for clip_index, raw in enumerate(raw_clips, start=1):
            evaluations.append(evaluate_clip(raw))
            if progress:
                progress.update(
                    clip_index,
                    len(raw_clips),
                    CLIP_NAMES[clip_index - 1],
                    "evaluation",
                    clip_index,
                    len(raw_clips),
                )
        if progress:
            progress.update(len(raw_clips), len(raw_clips), CLIP_NAMES[-1], "report", 0, 1)
        write_outputs(run_dir, raw_clips, evaluations, generated_at)
        if progress:
            progress.update(len(raw_clips), len(raw_clips), CLIP_NAMES[-1], "report", 1, 1)
    except Exception as error:
        for child in run_dir.iterdir():
            if child.is_file():
                child.unlink()
        run_dir.rmdir()
        if progress:
            progress.finish_line()
        print(f"Could not write analysis report: {error}", file=sys.stderr)
        return 1
    if progress:
        progress.finish_line()
    print(f"Analysis complete: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
