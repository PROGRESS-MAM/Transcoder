from pathlib import Path

import pytest

from oneoff.config import ConfigError, build_policy, load_config, resolve_exe_dir


def write_config(exe_dir: Path, *, source: Path, target: Path, repair_test: str = "true") -> None:
    (exe_dir / "config.ini").write_text(
        "[paths]\n"
        f"source_path = {source}\n"
        f"target_path = {target}\n\n"
        "[repair]\n"
        f"repair-test = {repair_test}\n",
        encoding="utf-8",
    )


def make_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    exe_dir = tmp_path / "app"
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    exe_dir.mkdir()
    source_dir.mkdir()
    target_dir.mkdir()
    return exe_dir, source_dir, target_dir


def test_resolve_exe_dir_uses_executable_or_script_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "source" / "runner.py"
    executable = tmp_path / "dist" / "repair.exe"
    monkeypatch.chdir(tmp_path)
    assert resolve_exe_dir(script) == script.parent.resolve()
    assert resolve_exe_dir(script, executable) == executable.parent.resolve()


def test_load_config_resolves_relative_values_from_exe_dir(tmp_path: Path) -> None:
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    (exe_dir / "input").mkdir()
    (exe_dir / "fixed").mkdir()
    (exe_dir / "reports").mkdir()
    write_config(exe_dir, source=Path("input"), target=Path("fixed"), repair_test="true")

    config = load_config(exe_dir)

    assert config.exe_dir == exe_dir.resolve()
    assert config.source_dir == (exe_dir / "input").resolve()
    assert config.target_dir == (exe_dir / "fixed").resolve()
    assert config.reports_dir == (exe_dir / "reports").resolve()
    assert config.repair_test is True


def test_load_config_does_not_create_reports_directory(tmp_path: Path) -> None:
    exe_dir, source, target = make_dirs(tmp_path)
    write_config(exe_dir, source=source, target=target)

    config = load_config(exe_dir)

    assert config.reports_dir == exe_dir / "reports"
    assert not config.reports_dir.exists()


def test_config_and_policy_for_test_mode(tmp_path: Path) -> None:
    exe_dir, source, target = make_dirs(tmp_path)
    write_config(exe_dir, source=source, target=target, repair_test="true")

    policy = build_policy(load_config(exe_dir))

    assert policy.mode == "test"
    assert policy.candidate_names == frozenset({"58238241.mp4", "58238527.mp4"})
    assert "53287289.mp4" not in policy.candidate_names
    assert policy.repair_confirmed_only is True


def test_config_and_policy_for_series_mode(tmp_path: Path) -> None:
    exe_dir, source, target = make_dirs(tmp_path)
    write_config(exe_dir, source=source, target=target, repair_test="false")

    policy = build_policy(load_config(exe_dir))

    assert policy.mode == "series"
    assert policy.candidate_names is None
    assert policy.repair_confirmed_only is True


@pytest.mark.parametrize("repair_test", ["", "yes", "1", "TRUE"])
def test_rejects_invalid_mode_switch_without_filesystem_changes(tmp_path: Path, repair_test: str) -> None:
    exe_dir, source, target = make_dirs(tmp_path)
    write_config(exe_dir, source=source, target=target, repair_test=repair_test)
    before = sorted(path.name for path in tmp_path.rglob("*"))

    with pytest.raises(ConfigError, match="repair-test"):
        load_config(exe_dir)

    assert sorted(path.name for path in tmp_path.rglob("*")) == before


def test_missing_config_is_clear_and_does_not_create_paths(tmp_path: Path) -> None:
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()

    with pytest.raises(ConfigError, match="Configuration file is missing"):
        load_config(exe_dir)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["app"]
    assert sorted(path.name for path in exe_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[paths]\nsource_path = x\ntarget_path = y\n[repair]\nrepair-test = true\nextra = x\n", "unknown option"),
        ("[paths]\nsource_path = x\ntarget_path = y\n[repair]\nrepair-test = true\n[other]\nx = y\n", "unknown section"),
        ("[paths]\nsource_path = x\n[repair]\nrepair-test = true\n", "missing option"),
        ("[paths]\nsource_path = x\ntarget_path = y\n[repair]\nrepair-test = true\n[repair]\nrepair-test = false\n", "Cannot read valid configuration"),
    ],
)
def test_rejects_invalid_config_shape(tmp_path: Path, content: str, message: str) -> None:
    exe_dir, _, _ = make_dirs(tmp_path)
    (exe_dir / "config.ini").write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(exe_dir)


def test_rejects_identical_source_and_target(tmp_path: Path) -> None:
    exe_dir, source, _ = make_dirs(tmp_path)
    write_config(exe_dir, source=source, target=source)

    with pytest.raises(ConfigError, match="must be different"):
        load_config(exe_dir)


def test_rejects_missing_or_unusable_directories(tmp_path: Path) -> None:
    exe_dir, source, target = make_dirs(tmp_path)
    missing = tmp_path / "missing"
    write_config(exe_dir, source=source, target=missing)
    with pytest.raises(ConfigError, match="Target directory"):
        load_config(exe_dir)

    write_config(exe_dir, source=target, target=source)
    (exe_dir / "reports").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigError, match="Report directory"):
        load_config(exe_dir)
