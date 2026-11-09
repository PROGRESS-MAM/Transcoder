from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oneoff"))
import maischberger_clips as m


def test_clip_collection_shape_and_count():
    assert len(m.clips) == 211
    assert len({clip[0] for clip in m.clips}) == len(m.clips)
    assert all(isinstance(clip, tuple) and len(clip) == 4 and all(isinstance(value, str) for value in clip) for clip in m.clips)


def test_required_boundary_entries():
    assert m.clips[0] == ("595000", "PROB0616", "Maischberger Kompakt 24.01.2003", "Proxy fix:audio: Ton: Kanal 3 hörbar und nicht doppelt zu Kanal 1/2 (Ähnlichkeit 0.50)")
    assert m.clips[207][:3] == ("624751", "PROB0389", "Maischberger 15.01.2002 - Dieter Hildebrandt")
    assert m.clips[208] == ("594645", "PROB0622", "Maischberger 03.02.2003 - Annette Schavan, Heide Simonis", "Proxy fix:audio: Ton unsicher: Kanal 4 trust 0.78 < 0.90")
    assert m.clips[-1][:2] == ("593502", "PROB0137")


def test_title_spacing_and_values():
    by_id = {clip[0]: clip for clip in m.clips}
    assert by_id["623792"][2] == "Maischberger 20.08.2003 - Ludger Volmer,  Hans-Ulrich Klose"
    assert all(identifier.startswith("PROB") for _, identifier, _, _ in m.clips)
    assert all("Maischberger" in title for _, _, title, _ in m.clips)
