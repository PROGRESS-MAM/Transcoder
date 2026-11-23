from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "toolbox" not in sys.modules:
    toolbox = ModuleType("toolbox")
    toolbox.tb_write_log = lambda *args: None
    sys.modules["toolbox"] = toolbox

from transcoder._source import Source
from marathon import transcode_marathon as marathon


def ch(number, audible, trust):
    return {"channel": number, "audible": audible, "trust": trust}


def found(*channels):
    return {"channels": list(channels)}


def test_extra_audible_channel_uses_1_2():
    assert marathon._audio_fallback(found(ch(1, True, 1.0), ch(2, True, 1.0), ch(3, True, 1.0), ch(4, False, 1.0)), 0.9) == [1, 2]


def test_unsure_channel_uses_1_2():
    assert marathon._audio_fallback(found(ch(1, True, 1.0), ch(2, True, 0.76), ch(3, False, 1.0), ch(4, False, 1.0)), 0.9) == [1, 2]


def test_unsure_silence_uses_1_2():
    assert marathon._audio_fallback(found(*(ch(index, False, 0.75) for index in range(1, 5))), 0.9) == [1, 2]
    assert marathon._audio_fallback(found(ch(1, False, 0.75), ch(2, False, 0.75), ch(3, True, 0.75), ch(4, False, 0.75)), 0.9) == [1, 2]


def test_sure_silence_returns_none():
    assert marathon._audio_fallback(found(*(ch(index, False, 1.0) for index in range(1, 5))), 0.9) is None


def test_silent_1_2_uses_audible_rest():
    assert marathon._audio_fallback(found(ch(1, False, 1.0), ch(2, False, 1.0), ch(3, True, 0.75), ch(4, True, 1.0),
                                         ch(5, False, 0.72), ch(6, False, 1.0)), 0.9) == [3, 4]


def test_silent_1_2_single_rest_is_doubled():
    assert marathon._audio_fallback(found(ch(1, False, 1.0), ch(2, False, 1.0), ch(3, True, 0.8), ch(4, False, 0.7)), 0.9) == [3, 3]


def test_silent_1_2_without_audible_rest_uses_1_2():
    assert marathon._audio_fallback(found(ch(1, False, 1.0), ch(2, False, 1.0), ch(3, False, 0.7), ch(4, False, 0.7)), 0.9) == [1, 2]


def test_one_sure_silent_side_doubles_other():
    assert marathon._audio_fallback(found(ch(1, True, 1.0), ch(2, False, 1.0), ch(3, True, 0.8)), 0.9) == [1, 1]
    assert marathon._audio_fallback(found(ch(1, False, 1.0), ch(2, True, 0.8)), 0.9) == [2, 2]


def test_single_unsure_channel_is_doubled():
    assert marathon._audio_fallback(found(ch(1, False, 0.5)), 0.9) == [1, 1]


def test_trust_rounding_matches_fix_audio():
    assert marathon._audio_fallback(found(*(ch(index, False, 0.897) for index in range(1, 5))), 0.9) is None
    assert marathon._audio_fallback(found(*(ch(index, False, 0.894) for index in range(1, 5))), 0.9) == [1, 2]


def _source(channel_count):
    return Source(files=(Path("film.mov"),), probes=(), video=object(), audio=(),
                  channels=tuple(object() for _ in range(channel_count)), fps=Fraction(25), duration=12.0,
                  start_tc="00:00:00:00", tc_origin="default", reports={})


def _run(audio_report, audio_problem, tmp_path):
    source = _source(4)

    def analyse(_source, checks, *_args):
        if checks == "streams":
            return {"streams": {"video": {"width": 1920, "height": 1080, "fps": Fraction(25)}}}
        if checks == "audio":
            return {"audio": audio_report}
        return {"black_bars": {"rating": "none"},
                "interlace": {"rating": "progressive", "trust": 1.0, "field_order": "tff"}}

    result = SimpleNamespace(state="ok", summary="proxy", file=Path("proxy.mov"), errors=[])
    job = {"inputs": ["film.mov"], "proxy_name": "proxy.mov", "ffe_tafel": False,
           "identifier": "DEFA00001", "title": "Title", "collection": "DEFA Der Augenzeuge"}
    with (
        patch.object(marathon.adapter, "share_path", side_effect=Path),
        patch.object(marathon.adapter, "log") as log,
        patch.object(marathon.transcoder, "source", return_value=source),
        patch.object(marathon.transcoder, "analyse", side_effect=analyse),
        patch.object(marathon.transcoder, "output", return_value=object()) as output,
        patch.object(marathon.transcoder, "render", return_value=[result]),
        patch.object(marathon, "_audio_problem", return_value=audio_problem),
        patch.object(marathon, "_render_source", return_value=source),
        patch.object(marathon, "_fix", side_effect=lambda name, **kwargs: (name, kwargs)),
        patch.object(marathon, "_write_json"),
    ):
        report = marathon.run_job(job, tmp_path, Path("log"))
    return report, output, log


def test_run_job_applies_audio_rule(tmp_path):
    problem = "Proxy fix:audio: Ton unsicher: Kanal 2 trust 0.76 < 0.90"
    audio = {"audible": [1, 2], "channels": [ch(1, True, 1.0), ch(2, True, 0.76), ch(3, False, 1.0), ch(4, False, 1.0)],
             "trust": 0.76}
    report, output, log = _run(audio, problem, tmp_path)

    assert report.status == "ok"
    assert output.call_args.kwargs["modify"][0].kind == "audio_map"
    assert output.call_args.kwargs["modify"][0].value == [1, 2]
    assert not any(item[0] == "audio" for item in output.call_args.args[2])
    assert any(call.args[0] == f"Tonregel: {problem} → audio_map([1, 2])" for call in log.call_args_list)


def test_run_job_sure_silence_stays_manual(tmp_path):
    problem = "Proxy fix:audio: Ton: kein hörbarer Kanal"
    audio = {"audible": [], "channels": [ch(index, False, 1.0) for index in range(1, 5)], "trust": 1.0}
    report, output, _ = _run(audio, problem, tmp_path)

    assert report.status == "rejected"
    assert report.result == f"Manuelle Prüfung: {problem}"
    output.assert_not_called()


def test_new_exception_entries():
    assert marathon._expected_silence({"collection": "DEFA Videoarchive Cintec", "identifier": "DEFA06955"})
    assert marathon._expected_silence({"collection": "DEFA Videoarchive Cintec", "identifier": "DEFA12880_2"})
    assert marathon._expected_silence({"collection": "DEFA Videoarchive Thomas Grimm", "identifier": "DEFA02244_3"})
    assert not marathon._expected_silence({"collection": "DEFA Videoarchive Cintec", "identifier": "DEFA01584"})


def test_interlace_metadata_fallback_enabled():
    assert marathon.settings["interlace"] == {"metadata_fallback": True}
    assert marathon._analysis.split_settings(marathon.settings)["interlace"]["metadata_fallback"] is True
