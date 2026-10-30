from copy import deepcopy
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
from transcoder._source import Source
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
        source = Source(
            files=(Path("film.mov"),), probes=(), video=object(), audio=(),
            channels=(object(), object()), fps=Fraction(25), duration=12.0,
            start_tc="00:00:00:00", tc_origin="default", reports={},
        )
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


class OpAtomReferenceTests(unittest.TestCase):
    @staticmethod
    def _source(probes):
        return Source(
            files=tuple(Path(f"clip_{index}.mxf") for index in range(len(probes))),
            probes=tuple(probes),
            video=None,
            audio=(),
            channels=(),
            fps=Fraction(25),
            duration=12.0,
            start_tc="00:00:00:00",
            tc_origin="default",
            reports={"fixture": "unchanged"},
        )

    @staticmethod
    def _real_stream(index, media_type, umid):
        codec_name = "mpeg2video" if media_type == "video" else "pcm_s24le"
        return {
            "index": index,
            "codec_type": media_type,
            "codec_name": codec_name,
            "width": 1920 if media_type == "video" else None,
            "channels": 2 if media_type == "audio" else None,
            "tags": {
                "file_package_umid": umid,
                "language": "eng",
                "fixture_note": "keep unchanged",
            },
            "_fixture_id": f"real-{umid}",
        }

    @staticmethod
    def _reference(index, media_type, umid, fixture_id):
        return {
            "index": index,
            "codec_type": "data",
            "codec_tag_string": "[0][0][0][0]",
            "nb_frames": "0",
            "tags": {
                "data_type": media_type,
                "file_package_umid": umid,
                "fixture_note": "keep unchanged",
            },
            "_fixture_id": fixture_id,
        }

    @staticmethod
    def _probe(index, streams, opatom=True):
        pattern = (
            "060E2B34.04010102.0D010201.1002.00000000"
            if opatom else "060E2B34.04010102.0D010301.0101.00000000"
        )
        return {
            "streams": streams,
            "format": {
                "filename": f"clip_{index}.mxf",
                "duration": "12.000",
                "tags": {
                    "operational_pattern_ul": pattern,
                    "fixture_note": "format unchanged",
                },
            },
            "chapters": [{"id": index, "fixture_note": "unchanged"}],
        }

    def _assert_result(self, source, removed_count, removed_ids):
        source_before = deepcopy(source.__dict__)
        expected_probes = []
        for probe in source_before["probes"]:
            expected_probe = deepcopy(probe)
            expected_probe["streams"] = [
                stream for stream in expected_probe["streams"]
                if stream.get("_fixture_id") not in removed_ids
            ]
            expected_probes.append(expected_probe)

        result, count = marathon._without_opatom_refs(source)

        self.assertEqual(count, removed_count)
        self.assertIsNot(result, source)
        self.assertEqual(result.probes, tuple(expected_probes))
        self.assertEqual(source.__dict__, source_before)
        original_media = [
            stream
            for probe in source_before["probes"]
            for stream in probe["streams"]
            if stream["codec_type"] in ("video", "audio")
        ]
        retained_media = [
            stream
            for probe in result.probes
            for stream in probe["streams"]
            if stream["codec_type"] in ("video", "audio")
        ]
        self.assertEqual(retained_media, original_media)
        return result

    def _clip(self, file_count):
        media_types = ("video", "audio", "video", "audio", "video")
        umids = [f"urn:synthetic:package:{index}" for index in range(file_count)]
        probes = []
        reference_ids = set()
        for file_index in range(file_count):
            streams = [self._real_stream(0, media_types[file_index], umids[file_index])]
            for target_index in range(file_count):
                if target_index == file_index:
                    continue
                fixture_id = f"ref-{file_index}-{target_index}"
                reference_ids.add(fixture_id)
                streams.append(self._reference(
                    len(streams), media_types[target_index], umids[target_index], fixture_id,
                ))
            probes.append(self._probe(file_index, streams))
        return probes, reference_ids

    def test_five_file_clip_removes_twenty_references(self):
        probes, reference_ids = self._clip(5)
        self.assertEqual(len(reference_ids), 20)
        self._assert_result(self._source(probes), 20, reference_ids)

    def test_three_file_clip_removes_six_references(self):
        probes, reference_ids = self._clip(3)
        self.assertEqual(len(reference_ids), 6)
        self._assert_result(self._source(probes), 6, reference_ids)

    def test_reference_to_missing_partner_is_retained(self):
        video_umid = "urn:synthetic:own:video"
        audio_umid = "urn:synthetic:own:audio"
        missing_umid = "urn:synthetic:missing:partner"
        probes = [
            self._probe(0, [
                self._real_stream(0, "video", video_umid),
                self._reference(1, "audio", audio_umid, "valid-0-1"),
                self._reference(2, "video", missing_umid, "missing-partner"),
            ]),
            self._probe(1, [
                self._real_stream(0, "audio", audio_umid),
                self._reference(1, "video", video_umid, "valid-1-0"),
            ]),
        ]
        result = self._assert_result(
            self._source(probes), 2, {"valid-0-1", "valid-1-0"},
        )
        retained_ids = {
            stream["_fixture_id"]
            for probe in result.probes
            for stream in probe["streams"]
        }
        self.assertIn("missing-partner", retained_ids)

    def test_non_opatom_data_stream_is_left_unchanged(self):
        video_umid = "urn:synthetic:own:video"
        audio_umid = "urn:synthetic:own:audio"
        probes = [
            self._probe(0, [
                self._real_stream(0, "video", video_umid),
                self._reference(1, "audio", audio_umid, "data-0-1"),
            ], opatom=False),
            self._probe(1, [
                self._real_stream(0, "audio", audio_umid),
                self._reference(1, "video", video_umid, "data-1-0"),
            ], opatom=False),
        ]
        self._assert_result(self._source(probes), 0, set())


if __name__ == "__main__":
    unittest.main()
