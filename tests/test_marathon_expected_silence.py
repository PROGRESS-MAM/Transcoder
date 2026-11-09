from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace, ModuleType
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


def _source(channel_count):
    return Source(
        files=(Path("film.mov"),), probes=(), video=object(), audio=(),
        channels=tuple(object() for _ in range(channel_count)), fps=Fraction(25), duration=12.0,
        start_tc="00:00:00:00", tc_origin="default", reports={},
    )


def _job(**values):
    job = {
        "inputs": ["film.mov"], "proxy_name": "proxy.mov", "ffe_tafel": False,
        "identifier": "LA000674", "title": "Title", "collection": "LYNXarchive",
    }
    return {**job, **values}


def _run(job, channel_count, audible=None, trust=1.0, tmp_path=None):
    source = _source(channel_count)
    analyzed = []

    def analyse(_source, checks, *_args):
        analyzed.append(checks)
        if checks == "streams":
            return {"streams": {"video": {"width": 1920, "height": 1080, "fps": Fraction(25)}}}
        if checks == "audio":
            return {"audio": {"audible": audible or [], "channels": list(range(1, channel_count + 1)), "trust": trust}}
        return {
            "black_bars": {"rating": "none"},
            "interlace": {"rating": "progressive", "trust": 1.0, "field_order": "tff"},
        }

    result = SimpleNamespace(state="ok", summary="proxy", file=Path("proxy.mov"), errors=[])
    output_folder = tmp_path or Path(".")
    with (
        patch.object(marathon.adapter, "share_path", side_effect=Path),
        patch.object(marathon.adapter, "log") as log,
        patch.object(marathon.transcoder, "source", return_value=source),
        patch.object(marathon.transcoder, "analyse", side_effect=analyse),
        patch.object(marathon.transcoder, "output", return_value=object()) as output,
        patch.object(marathon.transcoder, "render", return_value=[result]),
        patch.object(marathon, "_audio_problem", return_value=None) as audio_problem,
        patch.object(marathon, "_render_source", return_value=source),
        patch.object(marathon, "_fix", side_effect=lambda name, **kwargs: (name, kwargs)) as fix,
        patch.object(marathon, "_write_json"),
    ):
        report = marathon.run_job(job, output_folder, Path("log"))
    return report, analyzed, output, audio_problem, fix, log


def test_config_and_expected_silence_matching_rules():
    assert marathon.expected_silence == {"LYNXarchive": ("LA",), "Historiathek": ("H",)}
    assert marathon._expected_silence(_job(identifier=" LA000674 "))
    assert not marathon._expected_silence(_job(identifier="la000674"))
    assert not marathon._expected_silence(_job(collection="lynxarchive"))
    assert not marathon._expected_silence(_job(collection="LYNXarchive2"))
    assert not marathon._expected_silence(_job(collection="DEFA Der Augenzeuge"))
    assert not marathon._expected_silence(_job(identifier="PROB1051"))
    assert not marathon._expected_silence(_job(collection=None))
    assert not marathon._expected_silence({"identifier": "LA000674"})
    assert marathon._expected_silence(_job(collection="Historiathek", identifier=" H00015 "))
    assert not marathon._expected_silence(_job(collection="Historiathek", identifier="h00015"))
    assert not marathon._expected_silence(_job(collection="historiathek", identifier="H00015"))
    assert not marathon._expected_silence(_job(collection="Historiathek", identifier="LA000674"))


def test_app_version_is_0_4_1():
    assert marathon.app_version == "0.4.1"


def test_expected_silence_without_channels_uses_stereo_silence(tmp_path):
    report, analyzed, output, audio_problem, fix, log = _run(_job(), 0, tmp_path=tmp_path)

    assert report.status == "ok"
    assert analyzed == ["streams", ["black_bars", "interlace"]]
    audio_problem.assert_not_called()
    assert all(call.args[0] != "audio" for call in fix.call_args_list)
    kwargs = output.call_args.kwargs
    assert kwargs["silent_audio"] is True
    assert kwargs["modify"] == ()
    assert any("Ton fehlt erwartungsgemäß (LYNXarchive, LA000674): stille Stereospur" in call.args[0]
               for call in log.call_args_list)
    assert any("hörbar keiner von 0 Kanälen" in call.args[0] for call in log.call_args_list)


def test_historiathek_expected_silence_without_channels_uses_stereo_silence(tmp_path):
    job = _job(collection="Historiathek", identifier="H00015")
    report, analyzed, output, audio_problem, fix, log = _run(job, 0, tmp_path=tmp_path)

    assert report.status == "ok"
    audio_problem.assert_not_called()
    assert output.call_args.kwargs["silent_audio"] is True
    assert output.call_args.kwargs["modify"] == ()
    assert any("Ton fehlt erwartungsgemäß (Historiathek, H00015)" in call.args[0]
               for call in log.call_args_list)


def test_expected_silence_with_two_silent_channels_maps_stereo_even_at_low_trust(tmp_path):
    report, analyzed, output, audio_problem, fix, log = _run(_job(), 2, trust=0.01, tmp_path=tmp_path)

    assert report.status == "ok"
    audio_problem.assert_not_called()
    assert all(call.args[0] != "audio" for call in fix.call_args_list)
    mapping = output.call_args.kwargs["modify"]
    assert len(mapping) == 1
    assert mapping[0].kind == "audio_map"
    assert mapping[0].value == [1, 2]
    assert "silent_audio" not in output.call_args.kwargs
    assert any("audio_map([1, 2])" in call.args[0] for call in log.call_args_list)


def test_expected_silence_with_one_silent_channel_duplicates_it(tmp_path):
    report, _, output, audio_problem, _, log = _run(_job(), 1, trust=0.0, tmp_path=tmp_path)

    assert report.status == "ok"
    audio_problem.assert_not_called()
    assert output.call_args.kwargs["modify"][0].value == [1, 1]
    assert any("audio_map([1, 1])" in call.args[0] for call in log.call_args_list)


def test_rule_does_not_apply_to_other_jobs_or_changed_case(tmp_path):
    excluded = [
        _job(collection="DEFA Der Augenzeuge"),
        _job(collection="LYNXarchive2"),
        _job(identifier="PROB1051"),
        _job(collection=None),
        _job(collection="lynxarchive"),
        _job(identifier="la000674"),
        _job(collection=" LYNXarchive"),
    ]
    for job in excluded:
        report, _, output, _, _, _ = _run(job, 0, tmp_path=tmp_path)
        assert report.status == "rejected"
        output.assert_not_called()


def test_trimmed_identifier_matches_expected_silence(tmp_path):
    report, _, output, _, _, log = _run(_job(identifier=" LA000674 "), 0, tmp_path=tmp_path)

    assert report.status == "ok"
    assert output.call_args.kwargs["silent_audio"] is True
    assert any("(LYNXarchive, LA000674)" in call.args[0] for call in log.call_args_list)


def test_ffe_tafel_remains_manual_for_expected_silence():
    report, analyzed, output, _, _, _ = _run(
        _job(ffe_tafel=True, ffe_reference_clips=["HD_1_66.mov"]), 0
    )

    assert report.status == "rejected"
    assert analyzed == []
    output.assert_not_called()


def test_silent_channels_without_rule_keep_existing_audio_fix_path(tmp_path):
    report, _, output, audio_problem, _, _ = _run(_job(collection="DEFA Der Augenzeuge"), 2)

    assert report.status == "ok"
    audio_problem.assert_called_once()
    output.assert_called_once()
    assert output.call_args.args[2][0][0] == "audio"
    assert "silent_audio" not in output.call_args.kwargs
    assert output.call_args.kwargs["modify"] == ()
