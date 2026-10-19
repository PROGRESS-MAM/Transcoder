"""Loading and checking of output presets (TOML)."""
# --------- IMPORTS ---------
from pathlib import Path
import copy
import tomllib


# --------- CONFIG ---------
preset_folder = Path(__file__).resolve().parent / "presets"


# --------- STATIC ---------
schema = {
    "extends": str, "container": ("mp4", "mov"), "faststart": bool,
    "video": {"codec": ("libx264", "dnxhd"), "profile": str, "pix_fmt": str, "color": ("bt709",), "bitrate": str,
              "maxrate": str, "bufsize": str, "x264_preset": str, "frame": str, "rasters": list,
              "scan": ("progressive", "interlaced", "source")},
    "audio": {"codec": ("aac", "pcm"), "bitrate": str, "sample_rate": int, "bit_depth": ("source", 16, 24),
              "layout": ("stereo", "tracks")},
}
required = (("container",), ("video", "codec"), ("video", "frame"), ("video", "scan"), ("audio", "codec"),
            ("audio", "layout"))


# --------- FUNC ---------
def _path(preset: str | Path) -> Path:
    path = Path(preset)
    return path if path.suffix.casefold() == ".toml" else preset_folder / f"{preset}.toml"


def _merge(base: dict, extra: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def _check(data: dict, rules: dict, where: str) -> None:
    for key, value in data.items():
        rule = rules.get(key)
        name = f"{where}{key}"
        if rule is None:
            raise ValueError(f"Preset: unbekannter Eintrag {name}; erlaubt: {', '.join(rules)}.")
        if isinstance(rule, dict):
            if not isinstance(value, dict):
                raise ValueError(f"Preset: {name} muss ein Abschnitt sein.")
            _check(value, rule, f"{name}.")
        elif isinstance(rule, tuple) and value not in rule:
            raise ValueError(f"Preset: {name} = {value!r}, erlaubt: {', '.join(map(repr, rule))}.")
        elif isinstance(rule, type) and (not isinstance(value, rule) or isinstance(value, bool) and rule is not bool):
            raise ValueError(f"Preset: {name} muss {rule.__name__} sein, erhalten {value!r}.")


def frame_size(text: str) -> tuple[int, int]:
    width, separator, height = text.casefold().partition("x")
    if not separator or not width.isdigit() or not height.isdigit() or int(width) % 2 or int(height) % 2:
        raise ValueError(f"Preset: Bildgröße {text!r} ungültig, erwartet z. B. '1920x1080' mit geraden Werten.")
    return int(width), int(height)


def load(preset: str | Path, seen: tuple[Path, ...] = ()) -> tuple[str, dict]:
    """(name, complete preset) of a preset name in the preset folder or a path to a TOML file."""
    path = _path(preset).resolve()
    if path in seen:
        raise ValueError(f"Preset: extends im Kreis ({' → '.join(item.stem for item in (*seen, path))}).")
    if not path.is_file():
        raise ValueError(f"Preset nicht gefunden: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Preset {path} nicht lesbar: {exc}") from exc
    _check(data, schema, "")
    if parent := data.pop("extends", None):
        nearby = Path(parent) if parent.endswith(".toml") else path.parent / f"{parent}.toml"
        data = _merge(load(nearby if nearby.is_file() else parent, (*seen, path))[1], data)
    if seen:
        return path.stem, data
    if missing := [".".join(keys) for keys in required if not _get(data, keys)]:
        raise ValueError(f"Preset {path.stem}: Pflichteinträge fehlen: {', '.join(missing)}.")
    frame = data["video"]["frame"]
    if frame == "raster":
        if not data["video"].get("rasters"):
            raise ValueError(f"Preset {path.stem}: frame = 'raster' braucht video.rasters.")
        for item in data["video"]["rasters"]:
            frame_size(item)
    elif frame != "source":
        frame_size(frame)
    if data["audio"]["codec"] == "aac" and data["audio"]["layout"] != "stereo":
        raise ValueError(f"Preset {path.stem}: AAC nur mit layout = 'stereo'.")
    return path.stem, data


def _get(data: dict, keys: tuple[str, ...]):
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data
