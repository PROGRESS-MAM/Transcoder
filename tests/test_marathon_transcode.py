from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "toolbox" not in sys.modules:
    toolbox = ModuleType("toolbox")
    toolbox.tb_write_log = lambda *args: None
    sys.modules["toolbox"] = toolbox

import transcoder
from transcoder import _render
from marathon import transcode_marathon as marathon


class PlateSelectionTests(unittest.TestCase):
    def select(self, clips, wanted, formats):
        with patch.object(marathon.adapter, "share_path", side_effect=Path), \
                patch.object(marathon, "_plate_format", side_effect=lambda path: formats[path.name]), \
                patch.object(marathon.adapter, "log"):
            return marathon._plate(clips, wanted)

    def test_fullhd_uses_hd_fallback_when_exact_plate_is_missing(self):
        fallback_format = (1440, 900, Fraction(25))
        selected = self.select(
            ["HD_1_66.mov"], (1920, 1080, Fraction(25)),
            {"HD_1_66.mov": fallback_format},
        )
        self.assertEqual(selected, (Path("HD_1_66.mov"), True))

    def test_existing_matching_plate_has_priority(self):
        wanted = (1920, 1080, Fraction(25))
        selected = self.select(
            ["HD_1_66.mov", "matching.mov"], wanted,
            {"HD_1_66.mov": (1440, 900, Fraction(25)), "matching.mov": wanted},
        )
        self.assertEqual(selected, (Path("matching.mov"), False))

    def test_other_source_formats_do_not_use_fullhd_fallback(self):
        selected = self.select(
            ["HD_1_66.mov"], (1280, 720, Fraction(25)),
            {"HD_1_66.mov": (1440, 900, Fraction(25))},
        )
        self.assertIsInstance(selected, str)
        self.assertIn("keine FFE-Tafel", selected)

    def test_centered_segment_pads_without_scaling_or_cropping(self):
        path = Path("HD_1_66.mov")
        plate = SimpleNamespace(
            files=[path], video=SimpleNamespace(file=path, ordinal=0),
            width=1440, height=900, sar=Fraction(1), field_order=None,
            fps=Fraction(25), video_frames=225, channels=[],
        )
        source = SimpleNamespace(fps=Fraction(25))
        canvas = transcoder.centered(path, 1920, 1080)
        for fit in ("contain", "cover"):
            errors = []
            with patch.object(_render, "open_source", return_value=plate):
                segments = _render._segments(
                    source, [canvas, transcoder.SOURCE], (1920, 1080, Fraction(1)), True, errors, {}
                )
            self.assertEqual(errors, [])
            self.assertEqual(segments, [("canvas", plate, 1920, 1080), ("source",)])
            graph = _render._Graph()
            plan = SimpleNamespace(
                frame=(1920, 1080, Fraction(1)), field=None,
                preset={"video": {"fit": fit}}, crop=None, deinterlace=False,
            )
            _render._video(graph, plan, segments[0], {(id(plate), path): 0}, source, True)
            filters = graph.text()
            self.assertIn("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black", filters)
            self.assertNotIn("scale=", filters)
            self.assertNotIn("crop=", filters)


class OutputPathTests(unittest.TestCase):
    def test_proxy_and_master_share_centered_fallback_and_timing(self):
        source = SimpleNamespace(video=object(), channels=[object(), object()])
        audio = {"audible": [1, 2], "channels": [{}, {}]}

        def analyse(_source, checks, *_args):
            if checks == "streams":
                return {"streams": {"video": {"width": 1920, "height": 1080, "fps": Fraction(25)}}}
            if checks == "audio":
                return {"audio": audio}
            return {
                "black_bars": {"rating": "none"},
                "interlace": {"rating": "progressive", "trust": 1.0, "field_order": "tff"},
            }

        results = [
            SimpleNamespace(state="ok", summary="proxy", file=Path("proxy.mov"), errors=[]),
            SimpleNamespace(state="ok", summary="master", file=Path("master.mov"), errors=[]),
        ]
        job = {
            "inputs": ["film.mov"], "proxy_name": "proxy.mov", "ffe_tafel": True,
            "ffe_reference_clips": ["HD_1_66.mov"], "identifier": "ID", "title": "Title",
        }

        with patch.object(marathon.adapter, "share_path", side_effect=Path), \
                patch.object(marathon.adapter, "log"), \
                patch.object(marathon, "_plate_format", return_value=(1440, 900, Fraction(25))), \
                patch.object(marathon.transcoder, "source", return_value=source), \
                patch.object(marathon.transcoder, "analyse", side_effect=analyse), \
                patch.object(marathon, "_audio_problem", return_value=None), \
                patch.object(marathon, "_render_source", return_value=source), \
                patch.object(marathon, "_fix", side_effect=lambda name, **target: (name, target)), \
                patch.object(marathon._render, "check_source_encoder"), \
                patch.object(marathon.transcoder, "output", side_effect=[object(), object()]) as output, \
                patch.object(marathon.transcoder, "render", return_value=results), \
                patch.object(marathon, "_write_json"):
            report = marathon.run_job(job, Path("out"), Path("worker.log"))

        self.assertEqual(report.status, "ok")
        self.assertEqual(output.call_count, 2)
        proxy_call, master_call = output.call_args_list
        proxy_merge = proxy_call.kwargs["merge"]
        master_merge = master_call.kwargs["merge"]
        self.assertIs(proxy_merge[0], master_merge[0])
        self.assertIsInstance(proxy_merge[0], type(transcoder.centered("x.mov", 1920, 1080)))
        self.assertEqual((proxy_merge[0].width, proxy_merge[0].height), (1920, 1080))
        self.assertEqual(proxy_merge[1:], master_merge[1:])
        self.assertIs(proxy_merge[2], transcoder.SOURCE)
        self.assertIs(proxy_call.kwargs["timecode"], master_call.kwargs["timecode"])
        self.assertIs(proxy_call.kwargs["timecode"].anchor, transcoder.SOURCE)
        self.assertEqual(len(proxy_call.args[2]), 3)
        self.assertEqual(len(master_call.args[2]), 2)


if __name__ == "__main__":
    unittest.main()
