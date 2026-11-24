import csv
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def delivery_context(tmp_path, monkeypatch):
    module_path = REPO_ROOT / "oneoff" / "repariere_zeitstempel.py"
    spec = importlib.util.spec_from_file_location("repariere_zeitstempel_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    base_dir = tmp_path / "app"
    base_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    clip = source_dir / "clip.mp4"
    clip.write_bytes(b"clip-bytes")
    messages = []
    show_message = module.show_message
    monkeypatch.setattr(module, "base_dir", base_dir)
    monkeypatch.setattr(module, "find_tool", lambda name: name)
    monkeypatch.setattr(module, "tool_version", lambda exe: "test")
    monkeypatch.setattr(module, "show_message", lambda title, text, warning: messages.append((title, text, warning)), raising=False)
    monkeypatch.setattr(module, "ask_inputs", lambda: ([clip], target_dir), raising=False)
    monkeypatch.setattr(module, "check_clip", lambda path: (module.Check(module.OK, 10), None))
    return SimpleNamespace(module=module, base_dir=base_dir, source_dir=source_dir, target_dir=target_dir,
                           clip=clip, messages=messages, show_message=show_message)


def test_cancel_returns_0_without_reports(delivery_context, monkeypatch):
    context = delivery_context
    monkeypatch.setattr(context.module, "ask_inputs", lambda: None)
    assert context.module.main() == 0
    assert not (context.base_dir / "reports").exists()


def test_ok_clip_is_copied(delivery_context, tmp_path):
    context = delivery_context
    assert context.module.main() == 0
    assert (context.target_dir / context.clip.name).read_bytes() == context.clip.read_bytes()
    assert not (context.target_dir / f"{context.clip.name}.part").exists()
    csv_path = next((context.base_dir / "reports").glob("*.csv"))
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file, delimiter=";"))
    assert any(row[1] == "in Ordnung, kopiert" for row in rows[1:])
    assert context.messages[-1][2] is False


def test_problem_clip_not_delivered(delivery_context, monkeypatch):
    context = delivery_context
    monkeypatch.setattr(context.module, "check_clip", lambda path: (context.module.Check(context.module.ODD, 10, detail="x"), None))
    assert context.module.main() == 1
    assert list(context.target_dir.iterdir()) == []
    title, text, warning = context.messages[-1]
    assert warning is True
    assert context.clip.name in text
    assert "NICHT GELIEFERT (1)" in text
    report = next((context.base_dir / "reports").glob("*.txt")).read_text(encoding="utf-8")
    assert "NICHT GELIEFERT: 1 Clips" in report


def test_unsupported_defect_not_delivered(delivery_context, monkeypatch):
    context = delivery_context
    module = context.module
    stamps = module.Timestamps([0, 1, 2, 3], [0, 2, 2, 3], module.Fraction(1, 25))
    check = module.Check(module.DEFECT, 10, 1, 0, 1, "d")
    monkeypatch.setattr(module, "check_clip", lambda path: (check, stamps))
    monkeypatch.setattr(module, "probe_streams", lambda path: {})
    monkeypatch.setattr(module, "plan_repair", lambda info, timestamps, verdict: (_ for _ in ()).throw(module.Unsupported("d")))
    assert module.main() == 1
    assert list(context.target_dir.iterdir()) == []
    assert module.UNSUPPORTED in next((context.base_dir / "reports").glob("*.csv")).read_text(encoding="utf-8-sig")


def test_existing_target_not_overwritten(delivery_context, monkeypatch):
    context = delivery_context
    target = context.target_dir / context.clip.name
    target.write_bytes(b"old")
    monkeypatch.setattr(context.module, "check_clip", lambda path: pytest.fail("check_clip must not run"))
    assert context.module.main() == 1
    assert target.read_bytes() == b"old"


def test_input_in_output_folder_aborts(delivery_context, monkeypatch):
    context = delivery_context
    clip = context.target_dir / "clip.mp4"
    clip.write_bytes(b"source")
    monkeypatch.setattr(context.module, "ask_inputs", lambda: ([clip], context.target_dir))
    assert context.module.main() == 2
    assert context.messages[-1][2] is True
    assert not (context.base_dir / "reports").exists()


def test_duplicate_names_not_delivered(delivery_context, tmp_path, monkeypatch):
    context = delivery_context
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    clips = [a_dir / "clip.mp4", b_dir / "CLIP.mp4"]
    for clip in clips:
        clip.write_bytes(b"duplicate")
    monkeypatch.setattr(context.module, "ask_inputs", lambda: (clips, context.target_dir))
    assert context.module.main() == 1
    assert list(context.target_dir.iterdir()) == []
    rows = list(csv.reader(next((context.base_dir / "reports").glob("*.csv")).open(encoding="utf-8-sig"), delimiter=";"))
    assert [row[1] for row in rows[1:]] == [context.module.FAILED, context.module.FAILED]


def test_copy_failure_removes_part(delivery_context, monkeypatch):
    context = delivery_context
    module = context.module
    def fail_after_copy(src, part):
        part.write_bytes(b"partial")
        raise OSError("copy failed")
    monkeypatch.setattr(module.shutil, "copy2", fail_after_copy)
    result = module.process_clip(context.clip, context.target_dir, lambda text: None)
    assert result.status == module.FAILED
    assert not (context.target_dir / context.clip.name).exists()
    assert not (context.target_dir / f"{context.clip.name}.part").exists()


def test_judge_detects_defect(delivery_context):
    module = delivery_context.module
    assert module.judge([0, 1, 2, 3], [0, 2, 2, 3]).status == module.DEFECT


def test_show_message_never_raises_when_tk_fails(delivery_context, monkeypatch):
    fake_tk = ModuleType("tkinter")
    fake_tk.TclError = type("TclError", (Exception,), {})

    def fail_to_create_root():
        raise RuntimeError("display unavailable")

    fake_tk.Tk = fail_to_create_root
    fake_messagebox = ModuleType("tkinter.messagebox")
    fake_tk.messagebox = fake_messagebox
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", fake_messagebox)

    delivery_context.show_message("title", "text", True)


def test_unreadable_clip_not_delivered(delivery_context, monkeypatch):
    context = delivery_context
    monkeypatch.setattr(context.module, "check_clip",
                        lambda path: (context.module.Check(context.module.UNREADABLE, detail="unreadable"), None))

    assert context.module.main() == 1
    assert list(context.target_dir.iterdir()) == []
    assert "NICHT GELIEFERT (1)" in context.messages[-1][1]


def test_failed_repair_not_published(delivery_context, monkeypatch):
    context = delivery_context
    module = context.module
    stamps = module.Timestamps([0, 1, 2, 3], [0, 2, 2, 3], module.Fraction(1, 25))
    check = module.Check(module.DEFECT, 4, 1, 1, 1, "duplicate PTS")
    plan = module.Plan({}, module.Fraction(25), module.Fraction(1, 25), 100)
    monkeypatch.setattr(module, "check_clip", lambda path: (check, stamps))
    monkeypatch.setattr(module, "probe_streams", lambda path: {})
    monkeypatch.setattr(module, "plan_repair", lambda info, timestamps, verdict: plan)
    monkeypatch.setattr(module, "repair_command", lambda src, part, repair_plan: [str(part)])

    def fail_repair(command, show):
        Path(command[-1]).write_bytes(b"partial")
        raise module.RepairError("repair failed")

    monkeypatch.setattr(module, "run_ffmpeg", fail_repair)

    assert module.main() == 1
    assert not (context.target_dir / context.clip.name).exists()
    assert not (context.target_dir / f"{context.clip.name}.part").exists()
    assert "NICHT GELIEFERT (1)" in context.messages[-1][1]
    assert f"{context.clip.name}: {module.FAILED}" in context.messages[-1][1]
    report = next((context.base_dir / "reports").glob("*.txt")).read_text(encoding="utf-8")
    assert "repair failed" in report



def test_interrupted_repair_removes_part(delivery_context, monkeypatch):
    context = delivery_context
    module = context.module
    stamps = module.Timestamps([0, 1, 2, 3], [0, 2, 2, 3], module.Fraction(1, 25))
    check = module.Check(module.DEFECT, 4, 1, 1, 1, "duplicate PTS")
    plan = module.Plan({}, module.Fraction(25), module.Fraction(1, 25), 100)
    monkeypatch.setattr(module, "check_clip", lambda path: (check, stamps))
    monkeypatch.setattr(module, "probe_streams", lambda path: {})
    monkeypatch.setattr(module, "plan_repair", lambda info, timestamps, verdict: plan)
    monkeypatch.setattr(module, "repair_command", lambda src, part, repair_plan: [str(part)])

    def interrupt_repair(command, show):
        Path(command[-1]).write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_ffmpeg", interrupt_repair)

    assert module.main() == 1
    assert not (context.target_dir / context.clip.name).exists()
    assert not (context.target_dir / f"{context.clip.name}.part").exists()
    report = next((context.base_dir / "reports").glob("*.txt")).read_text(encoding="utf-8")
    assert "ABGEBROCHEN" in report
