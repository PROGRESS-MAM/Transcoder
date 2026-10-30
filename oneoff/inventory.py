from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import (
    AppConfig,
    Candidate,
    ClipAnalysis,
    ClipCollector,
    RunPolicy,
    Selection,
    SkippedClip,
)


def _default_clip_collector(source_path: Path) -> dict[str, Any]:
    from .analysis.probe import collect_clip

    return collect_clip(source_path)


def _is_part_file(path: Path) -> bool:
    return path.name.casefold().endswith(".part")


def _regular_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.iterdir() if path.is_file()),
        key=lambda path: (path.name.casefold(), path.name),
    )


def select_candidates(config: AppConfig, policy: RunPolicy) -> Selection:
    if policy.mode not in {"test", "series"}:
        raise ValueError(f"unsupported selection mode: {policy.mode}")
    if policy.mode == "test" and not policy.candidate_names:
        raise ValueError("test mode requires a non-empty candidate allowlist")
    if policy.mode == "series" and policy.candidate_names is not None:
        raise ValueError("series mode must not restrict candidate names")

    source_files = _regular_files(config.source_dir)
    target_names = {
        path.name.casefold()
        for path in _regular_files(config.target_dir)
        if not _is_part_file(path)
    }
    allowed_names = (
        {name.casefold() for name in policy.candidate_names}
        if policy.candidate_names is not None
        else None
    )
    present_test_names: set[str] = set()
    candidates: list[Candidate] = []
    skipped: list[SkippedClip] = []

    for source_path in source_files:
        if _is_part_file(source_path):
            skipped.append(SkippedClip(source_path, "partial_file_ignored"))
            continue

        source_name = source_path.name.casefold()
        if allowed_names is not None and source_name not in allowed_names:
            skipped.append(SkippedClip(source_path, "not_in_test_allowlist"))
            continue
        if allowed_names is not None:
            present_test_names.add(source_name)

        if source_name in target_names:
            skipped.append(SkippedClip(source_path, "matching_final_target_exists"))
            continue

        candidates.append(
            Candidate(
                source_path=source_path,
                target_path=config.target_dir / source_path.name,
            )
        )

    missing_test_sources = ()
    if policy.candidate_names is not None:
        missing_test_sources = tuple(
            config.source_dir / name
            for name in sorted(policy.candidate_names, key=lambda item: (item.casefold(), item))
            if name.casefold() not in present_test_names
        )

    return Selection(
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        missing_test_sources=missing_test_sources,
    )


def collect_candidate(
    candidate: Candidate,
    collector: ClipCollector = _default_clip_collector,
) -> ClipAnalysis:
    raw = collector(candidate.source_path)
    if not isinstance(raw, dict):
        raise TypeError("clip collector must return a dictionary of raw analysis data")
    return ClipAnalysis(candidate=candidate, raw=raw)
