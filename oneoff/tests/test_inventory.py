from pathlib import Path

import pytest

from oneoff.contracts import AppConfig, Candidate, ClipAnalysis, RunPolicy
from oneoff.inventory import collect_candidate, select_candidates


TEST_NAMES = frozenset({"58238241.mp4", "58238527.mp4"})


def make_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    return AppConfig(
        exe_dir=tmp_path,
        source_dir=source,
        target_dir=target,
        reports_dir=tmp_path / "reports",
        repair_test=False,
    )


def write(path: Path) -> Path:
    path.write_bytes(b"fixture")
    return path


def test_series_scans_only_immediate_regular_non_part_files(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write(config.source_dir / "clip.mp4")
    write(config.source_dir / "partial.mp4.part")
    (config.source_dir / "subfolder").mkdir()
    write(config.source_dir / "subfolder" / "nested.mp4")
    (config.source_dir / "fixed").mkdir()

    selection = select_candidates(
        config, RunPolicy(mode="series", candidate_names=None, repair_confirmed_only=True)
    )

    assert [item.source_path.name for item in selection.candidates] == ["clip.mp4"]
    assert [(item.source_path.name, item.reason) for item in selection.skipped] == [
        ("partial.mp4.part", "partial_file_ignored")
    ]
    assert selection.missing_test_sources == ()


def test_existing_target_matches_case_insensitive_full_filename(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    existing_source = write(config.source_dir / "Clip.MP4")
    other_extension = write(config.source_dir / "Clip.mov")
    write(config.target_dir / "cLiP.mp4")
    write(config.target_dir / "Clip.mov.part")

    selection = select_candidates(
        config, RunPolicy(mode="series", candidate_names=None, repair_confirmed_only=True)
    )

    assert selection.candidates == (
        Candidate(other_extension, config.target_dir / "Clip.mov"),
    )
    assert [(item.source_path, item.reason) for item in selection.skipped] == [
        (existing_source, "matching_final_target_exists")
    ]


def test_test_mode_allowlist_missing_sources_and_existing_target(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = write(config.source_dir / "58238241.mp4")
    write(config.source_dir / "58238527.mp4")
    write(config.source_dir / "53287289.mp4")
    write(config.source_dir / "other.mp4")
    write(config.target_dir / "58238241.MP4")
    policy = RunPolicy(mode="test", candidate_names=TEST_NAMES, repair_confirmed_only=True)

    selection = select_candidates(config, policy)

    assert [item.source_path.name for item in selection.candidates] == ["58238527.mp4"]
    assert selection.missing_test_sources == ()
    assert [(item.source_path.name, item.reason) for item in selection.skipped] == [
        ("53287289.mp4", "not_in_test_allowlist"),
        ("58238241.mp4", "matching_final_target_exists"),
        ("other.mp4", "not_in_test_allowlist"),
    ]
    assert first.exists()


def test_test_mode_reports_each_missing_allowlisted_source(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write(config.source_dir / "58238241.mp4")
    policy = RunPolicy(mode="test", candidate_names=TEST_NAMES, repair_confirmed_only=True)

    selection = select_candidates(config, policy)

    assert selection.missing_test_sources == (config.source_dir / "58238527.mp4",)
    assert [item.source_path.name for item in selection.candidates] == ["58238241.mp4"]


def test_collect_candidate_uses_injected_collector_and_preserves_raw_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.mp4"
    candidate = Candidate(source, tmp_path / "target" / source.name)
    raw = {"format": {"format_name": "mov,mp4"}, "streams": [], "packets": []}
    collected_paths: list[Path] = []

    def fake_collector(path: Path) -> dict:
        collected_paths.append(path)
        return raw

    result = collect_candidate(candidate, fake_collector)

    assert result == ClipAnalysis(candidate=candidate, raw=raw)
    assert collected_paths == [source]


def test_collect_candidate_rejects_non_dictionary_analysis(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    candidate = Candidate(source, tmp_path / "target" / source.name)

    with pytest.raises(TypeError, match="must return a dictionary"):
        collect_candidate(candidate, lambda _: [])
