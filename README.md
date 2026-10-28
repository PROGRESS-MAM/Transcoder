# Transcoder

> **Version 0.2.0:** Vor dem ersten Einsatz des NVIDIA-Proxys ffmpeg, GPU und Treiber mit den Befehlen unter „NVIDIA-Proxy“ prüfen. Für den EXE-Betrieb nach Änderungen einen neuen Windows-Build erstellen.

Rendert Mediendateien mit ffmpeg und analysiert sie. Ganz unten stehen zwei Betriebsarten:

```
source ── render ──► Output 1, Output 2, Output 3, …
source ── analyse ─► Bericht 1, Bericht 2, Bericht 3, …
```

Der Kern `transcoder` macht genau das. Die minimale App `sample.py` sucht Sources in Ordnern, stellt Outputs und Segmente bereit, speichert Berichte und Fehlerliste, zeigt Fortschritt und Meldungen und übergibt Analyseergebnisse an eine Use-Case-Logik. Der Marathon-Adapter ist angeflanscht und steuert den Transcoder für den Marathon-Use-Case fern; auf Kern, Presets und App hat er keinen Einfluss.

## Aufbau

| Pfad | Aufgabe |
|---|---|
| `transcoder/` | Kern: `source`, `analyse`, `output`, `render`, Fixes, Änderungen, Segmente, `timecode` |
| `transcoder/presets/` | Presets (TOML) |
| `res/` | `ffmpeg` und `ffprobe`; ohne sie wird im Systempfad gesucht |
| `sample.py` | minimale App |
| `marathon/` | Marathon-Adapter: angeflanschte Fernsteuerung für den Marathon-Use-Case (siehe „Marathon-Adapter“) |

## Installation

Python 3.11+, ffmpeg ≥ 6 mit libx264 und DNxHD/DNxHR (`ffmpeg` und `ffprobe` in `res/` oder im Systempfad). Für das NVIDIA-Proxy-Preset zusätzlich `h264_nvenc` im ffmpeg-Build und einen zum Build passenden NVIDIA-Treiber verwenden; siehe „NVIDIA-Proxy“. Einmal je Rechner die Paketquelle der Flow-API eintragen (für `toolbox`):

```
pip config set global.extra-index-url https://artifacts.editshare.com/artifactory/api/pypi/editshare-pypi-public/simple
```

Dann im Projektordner:

```
pip install -e .            # transcoder mit toolbox und numpy
pip install -e ".[build]"   # zusätzlich pyinstaller für den Build
```

In ein anderes Projekt installieren:

```
pip install "transcoder @ git+https://github.com/PROGRESS-MAM/Transcoder.git@main"
```

## Begriffe

| Begriff | Bedeutung |
|---|---|
| Source | die Quelle: eine Datei oder mehrere zusammengehörige Dateien (z. B. OP-Atom: je Stream eine Datei) |
| Output | Beschreibung einer Ausgabedatei: Preset, Zieldatei, Fixes, Änderungen, Segmente, Timecode |
| Render | Ausführung: rendert eine Source in einen oder mehrere Outputs |
| Preset | Ausgabeformat: Container, Codecs, Bitraten, Bildrahmen, Tonlayout |
| Fix | Analyse mit Reparaturempfehlung plus Änderung, die die Empfehlung anwendet |
| Änderung | direkter Eingriff ohne Analyse, z. B. Beschnitt |
| Segment | Teil einer Zusammenstellung (`merge`): Datei, erzeugtes Schwarzbild oder die Source selbst |
| Bericht | Ergebnis einer Analyse-Prüfung als Daten |

Ein Fix beschreibt den Übergang von der Source zum Ausgabeformat, das Preset das Ausgabeformat: erst Fix, dann Ausgabe. Fixes, Änderungen, Segmente und Presets sinnvoll zu kombinieren, liegt beim Aufrufer.

## Kern

```python
import transcoder

src = transcoder.source(["D:/in/film.mov"])

reports = transcoder.analyse(src, ["streams", "audio", "black_bars", "interlace"])

web = transcoder.output("h264_10mbit_1080p", "D:/out/film_10Mbit.mp4",
                        fixes=[transcoder.fix("audio", channels=2),
                               transcoder.fix("black_bars"), transcoder.fix("interlace")])
edit = transcoder.output("dnxhr_hqx", "D:/out/film.mov",
                         fixes=[transcoder.fix("audio", min_audible=2)])

results = transcoder.render(src, [web, edit], on_message=show)
for result in results:
    print(result.file, result.state, result.summary)
```

Ein einzelner Output: `transcoder.render(src, web)`. Der Kern sucht keine Dateien, gibt nichts selbst aus und schreibt weder Berichte noch Logs; alles kommt über `on_message` und die Rückgabewerte.

| Funktion | Bedeutung |
|---|---|
| `source(files, tc=None)` | Source aus einer oder mehreren Dateien; Streams aller Dateien bilden zusammen die Quelle. `tc` setzt den Start-TC; sonst aus den Dateien, ohne TC `01:00:00:00`. Analysen werden an der Source gemerkt: jede Prüfung läuft je Source und Einstellungen nur einmal, auch über `analyse` und `fix` hinweg |
| `analyse(source, checks, settings=None, on_message=None)` | Berichte ohne Änderung, `checks` als Name oder Liste; `settings` mit einem Abschnitt je Prüfung (siehe „Einstellungen“); Rückgabe `{Prüfung: Bericht}`, z. B. `reports["audio"]["audible"]`; eine Prüfung, die nicht laufen kann, bekommt `{"error": Text}` |
| `output(preset, file, fixes=(), modify=(), merge=(), timecode=None)` | beschreibt eine Ausgabedatei; rendert noch nicht. Eine Änderung in `modify` ersetzt den Fix derselben Art (`crop` → `black_bars`, `deinterlace` → `interlace`, `audio_map` → `audio`) |
| `render(source, outputs, on_message=None)` | rendert die Source in einen Output oder eine Liste von Outputs, in einem Durchlauf (die Source wird einmal gelesen); Rückgabe: je Output ein Ergebnis in derselben Reihenfolge |
| `fix(name, settings=None, **target)` | Fix für einen Output (siehe „Analyse und Fixes“); `target` = was der Fix erreichen soll, z. B. `channels=2`; `settings` = Abschnitt dieser Prüfung, z. B. `settings["audio"]`; ohne `settings` Standardwerte |
| `crop(x, y, w, h)` | Änderung: Beschnitt auf diese Fläche, dann Einpassen in den Bildrahmen des Presets |
| `deinterlace()` | Änderung: `bwdif`, ein Bild je Frame (25i → 25p) |
| `audio_map(channels)` | Änderung: Quellkanäle der Ausgabe, z. B. `[1, 1]` |
| `black(seconds)` | Segment: Schwarzbild mit Stille im Format der Ausgabe |
| `SOURCE` | Segment: die Source |
| `timecode(start=None, shift=None, anchor=None)` | Timecode der Ausgabe (siehe „Merge und Timecode“) |
| `versions()` | Versionen von Kern und ffmpeg |

### Ergebnis je Output

| Feld | Bedeutung |
|---|---|
| `state` | `ok` oder `error` |
| `file` | Zieldatei |
| `summary` | Kurztext |
| `errors` | Fehlerbericht: je Fehler `step` (z. B. `fix:audio`, `fix:black_bars`, `merge`, `ffmpeg`, `file`) und `text` |
| `preset` | Name des Presets |
| `fixes` | je Fix `name`, `changed`, `detail` (z. B. „Kanal 1 auf [1, 1]“, „beschnitten auf 1920×800“, „deinterlaced“), `trust` |
| `modify`, `segments`, `timecode` | angewendete Änderungen, Segmente, Start-TC der Ausgabe |
| `command`, `seconds` | ffmpeg-Aufruf, Dauer |

Jeder Output wird einzeln betrachtet: Ein Output mit Fehler (z. B. Fix nicht sicher genug, Segment passt nicht) wird nicht gerendert; die anderen laufen weiter. Scheitert ffmpeg im gemeinsamen Durchlauf, rendert der Kern die betroffenen Outputs einzeln nach, sofern der Fehler nicht als dauerhaft erkannt wurde. Bei erkannten ungültigen Encoderoptionen oder einer ffmpeg-Assertion gibt es keinen internen Einzeloutput-Wiederholungsversuch. Gerendert wird unter `<Datei>.part`, nach Erfolg umbenannt; bei Fehler oder Abbruch (`Strg+C` beendet ffmpeg) werden die Teildateien gelöscht. Zielordner werden angelegt, vorhandene Dateien nie überschrieben (`error`). Die fertigen Dateien prüft der Kern nicht.

### Meldungen `on_message(message)`

| Feld | Bedeutung |
|---|---|
| `kind` | `info`, `warning` oder `progress` |
| `text` | fertige Textzeile |
| `data` | bei `progress`: `percent`, beim Rendern zusätzlich `media_time`, `fps`, `speed`, `eta` |

`analyse` und `render` melden über `on_message`. `progress`-Meldungen ersetzen einander. Ohne `on_message` bleibt der Kern still.

## Analyse und Fixes

| Prüfung | Bericht | Fix |
|---|---|---|
| `streams` | aus den Metadaten: Container, Operational Pattern und Package-ID (MXF), Codecs, Profil, Größe, SAR/DAR, fps, Feldreihenfolge, Pixelformat, Bitrate, Dauer, Start-TC, Audio-Spuren/Kanäle/Layout, Encoder-Angaben (inkl. x264-Optionen) | – |
| `interlace` | Feldreihenfolge aus den Metadaten, gemessene Anteile interlaced/progressiv, `rating`: `interlaced` / `progressive` / `unclear`, `trust` | `interlaced` → deinterlacen; `progressive` → nichts |
| `black_bars` | obere/untere Ränder, `bars.left = bars.right = 0`, aktive Fläche, `rating` und `trust` | nur oben/unten beschneiden, `extra_crop` ausschließlich an einer Seite mit erkanntem Rand |
| `audio` | je Kanal Anteil mit Ton und Spitzenpegel, `audible` (Liste der hörbaren Kanäle), je Kanalpaar Ähnlichkeit (pegelunabhängig), Zeitversatz und Pegelunterschied, `duplicates`, `trust` | siehe unten |

- **Stichproben:** über die Laufzeit verteilt (`samples.per_minute`, `samples.seconds`) oder über die volle Länge (`samples.full`).
- **Statistik:** Die Stichproben werden gemeinsam ausgewertet; Ausreißer fallen heraus, z. B. Schwarz am Anfang und Ende oder sehr dunkle Szenen bei `black_bars`. Jede Bewertung hat ein `trust` von 0 bis 1 aus Anzahl und Übereinstimmung der Stichproben (bei `samples.full` für den Ton 1).
- **Sicherheit:** Ein Fix ändert nur, wenn seine Bewertung mindestens das `trust` seines Abschnitts erreicht; sonst `error` mit Bewertung und erreichter Sicherheit. Für vage Fälle `trust` erhöhen, z. B. `"audio": {"trust": 0.99}`.
- **`black_bars`:** Nur Zeilen oben/unten werden bewertet. Schwarz liegt nach Graustufendecodierung bei 0, Toleranz 6 von 255; ein dunklerer Bildbereich wird nicht zum Schwarzreferenzwert erklärt. Verwendet wird der kleinste Rand über alle gültigen Bilder; seitliche Balken lösen keinen Fix aus. Die Erkennung bleibt stichprobenbasiert: Für die ersten Läufe Master manuell vergleichen.
- **`interlace`:** Die Metadaten sind die erste Angabe; die Messung bestätigt sie. Mit `"interlace": {"source": "metadata"}` gilt nur die Angabe in den Metadaten, ohne Messung.
- **Stille** wird wie im AQC-Prüftool `digital_silence` gemessen: je Kanal Fenster der Länge `audio.silence_window`, ein Fenster zählt als Ton bei Pegel (RMS) ≥ `audio.silence_threshold`; ein Kanal ist hörbar, wenn sein Anteil an Fenstern mit Ton mindestens `audio.silence_min_active` beträgt. Zusätzlich vergleicht der Transcoder die Kanäle untereinander.

| Fix | Ergebnis |
|---|---|
| `fix("audio", channels=2)` | erste zwei sicher hörbare Kanäle in Quellreihenfolge; weitere hörbare Kanäle müssen sichere Dubletten sein, sonst `error`. Stumme Kanäle entfallen. Genau ein hörbarer Kanal → `[n, n]` |
| `fix("audio", min_audible=2)` | alle sicher hörbaren Kanäle in Quellreihenfolge als Einzelspuren, stumme Kanäle entfernen. Genau ein hörbarer Kanal → `[n, n]`; kein Ton oder unsichere Kanalbewertung → `error` |

### Einstellungen

`settings` ist ein Dict mit einem Abschnitt je Prüfung, dazu `samples` für alle Prüfungen mit Stichproben. Fehlende Abschnitte und Einträge haben Standardwerte; die Werte sind Startwerte. `analyse` bekommt das ganze Dict, ein Fix seinen Abschnitt. Ändert sich ein Abschnitt, wird diese Prüfung neu gemessen; sonst nutzt der Fix, was an der Source schon gemessen wurde.

```python
settings = {
    "samples":    {"per_minute": 6, "seconds": 2},
    "black_bars": {"extra_crop": 2, "trust": 0.9},
    "audio":      {"trust": 0.99, "duplicate_min": 0.95, "max_offset_ms": 40},
}

reports = transcoder.analyse(src, "black_bars", settings)
reports["black_bars"]
# {"rating": "bars", "trust": 0.97, "black_level": 16,
#  "bars": {"top": 140, "bottom": 140, "left": 0, "right": 0},
#  "active": (0, 140, 1920, 800)}

web = transcoder.output("h264_10mbit_1080p", "D:/out/film_10Mbit.mp4",
                        fixes=[transcoder.fix("black_bars", settings=settings["black_bars"]),
                               transcoder.fix("audio", channels=2, settings=settings["audio"])])
transcoder.render(src, web)
# black_bars: aktive Fläche (0, 140, 1920, 800), mit extra_crop=2 → Beschnitt (0, 142, 1920, 796),
#             danach Einpassen in 1920×1080
```

| Abschnitt | Eintrag | Standard | Bedeutung |
|---|---|---|---|
| `samples` | `per_minute` | `6` | Stichproben je Minute Laufzeit, auch Bruchteile (z. B. `0.5`); mindestens 1 Stichprobe |
| `samples` | `seconds` | `2` | Dauer je Stichprobe, auch Bruchteile (z. B. `0.5` ≈ 12 Frames bei 24 fps) |
| `samples` | `full` | `False` | `True`: volle Länge statt Stichproben |
| `interlace` | `source` | `"analyse"` | `"analyse"`: Metadaten plus Messung; `"metadata"`: nur Metadaten |
| `interlace` | `trust` | `0.9` | geforderte Sicherheit, ab der der Fix ändert (0 bis 1) |
| `black_bars` | `extra_crop` | `0` | zusätzliche Pixel je Seite mit Rand beim Beschnitt; Seiten ohne Rand bleiben unverändert |
| `black_bars` | `trust` | `0.9` | wie oben |
| `audio` | `silence_threshold` | `"-60dB"` | Pegel (RMS je Fenster), ab dem ein Fenster als Ton zählt |
| `audio` | `silence_min_active` | `"1%"` | Anteil der Fenster mit Ton, ab dem ein Kanal hörbar ist |
| `audio` | `silence_window` | `"1s"` | Fensterlänge |
| `audio` | `duplicate_min` | `0.95` | Ähnlichkeit, ab der zwei Kanäle als doppelt gelten; darunter nicht doppelt |
| `audio` | `max_offset_ms` | `40` | erlaubter Zeitversatz beim Kanalvergleich |
| `audio` | `trust` | `0.9` | wie oben |

Ein Abschnitt kann auch eigene `samples` enthalten, die für diese Prüfung gelten, z. B. `"audio": {"samples": {"full": True}}`. Der Beschnitt wird bei Bedarf auf gerade Werte nach innen gerundet (4:2:0).

### Master-Nachprüfung und Metadaten

- Die Quell-framerate wird ausdrücklich gesetzt. Bei DNxHD 185x interlaced sind nur 25 Frames/s zulässig; andere fps führen zur Fehlerliste, der Clip wird nicht transcodiert. Die Bildgröße darf für DNxHD auf 1920×1080 angepasst werden.
- Vor dem Marathon-Render wird ein generiertes Testbild mit dem Zielcodec erzeugt und decodiert. Nicht sicher übertragbare Farbangaben und Zusatzmetadaten (z. B. HDR-Side-Data oder Rotation) führen zu einer manuellen Fehlermeldung statt zu stiller Änderung.
- `preserve_metadata = true` in den DNx-Master-Presets übernimmt Container- und Stream-Metadaten, Kapitel sowie bekannte Farbangaben. `video.color = "source"` nimmt keine pauschale BT.709-Umwandlung vor. Codecabhängige Eigenschaften wie Kompression, Pixelformat, Bitrate und Zeitbasis ergeben sich aus dem Zielcodec.
- Nach dem Encode werden Codec/Profil, Bildgröße, SAR, Framerate, Framezahl, Scan/Feldreihenfolge, PCM-Kanäle, Samplerate, Bittiefe, Timecode und bekannte Farbangaben geprüft. Bei Abweichungen wird der Master nicht freigegeben. Diese technische Prüfung ersetzt nicht den manuellen Bildvergleich.
- Ein Stereo-Proxy verwirft weiterhin keine zusätzlichen eigenständigen Toninhalte stillschweigend; solche Quellen bleiben zur manuellen Prüfung blockiert. Der Master-Audio-Fix selbst erhält alle hörbaren Kanäle.

## Merge und Timecode

`merge` legt die Segmente eines Outputs und ihre Reihenfolge fest; ohne `merge` nur die Source. Segmente sind Dateien, `black(seconds)` und `SOURCE`. Welche Dateien passen, entscheidet der Aufrufer.

```python
intro = ["D:/intro/intro_1920x1080_24p.mov", transcoder.black(1), transcoder.SOURCE]
web = transcoder.output("h264_10mbit_1080p", "D:/out/film_10Mbit.mp4", merge=intro,
                        timecode=transcoder.timecode(anchor=transcoder.SOURCE))
```

- Segment-Dateien werden nie beschnitten; sie werden in den Bildrahmen des Presets eingepasst. Bei `frame = "raster"` muss eine Segment-Datei genau die Bildgröße der Ausgabe haben, sonst `error`.
- Segment-Dateien müssen die fps der Source haben (keine Bildwiederholung), sonst `error`. Ohne Ton → Stille.
- `merge` ändert den Timecode nicht: Ohne `timecode` beginnt die Ausgabe mit dem Start-TC der Source.

| `timecode(…)` | Start-TC der Ausgabe |
|---|---|
| ohne Angabe | Start-TC der Source |
| `start="09:59:50:00"` | fester Start-TC |
| `shift="-00:00:10:00"` | Start-TC der Source plus Verschiebung |
| `anchor=transcoder.SOURCE` | so, dass das Segment `SOURCE` beim Start-TC der Source beginnt (der TC wandert um die Länge der Segmente davor nach vorn) |

Unterlauf über 00:00:00:00 → Umlauf über 24 h. Beispiel: 9-s-Vorspann plus `black(1)`, 24 fps, Source-Start 10:00:00:00, `anchor=SOURCE` → Vorspann 09:59:50:00–09:59:58:23, Schwarz 09:59:59:00–09:59:59:23, Source ab 10:00:00:00.

## Presets

`preserve_metadata` ist ein optionaler boolescher Eintrag auf Preset-Ebene (Standard `false`; DNx-Master: `true`). `video.fit` ist `contain` oder `cover` (Standard `contain`); `video.color` ist `bt709` oder `source`.

Ein Preset beschreibt nur das Ausgabeformat. Datei `transcoder/presets/<name>.toml` oder Pfad zu einer TOML-Datei; `extends` übernimmt ein anderes Preset, eigene Einträge überschreiben dessen Werte.

| Preset | Format | Bildrahmen |
|---|---|---|
| `h264_10mbit_1080p` | MP4, H.264 High, 8 bit 4:2:0, BT.709, 10 Mbit/s, quadratische Pixel, progressiv, fps der Source; AAC 320 kbit/s Stereo 48 kHz | 1920×1080 |
| `h264_10mbit_1080p_nvenc` | wie `h264_10mbit_1080p`, H.264-Encoding mit NVIDIA NVENC statt libx264; p5, HQ, VBR | 1920×1080 |
| `dnxhr_hqx` | MOV, DNxHR HQX 10 Bit 4:2:2, Quell-fps und Quell-SAR; Quellmetadaten soweit sicher übertragbar; einzelne PCM-Kanäle, 48 kHz, 24 Bit | Quellbildgröße, proportional ausfüllen (`fit = "cover"`) |
| `dnxhd_185x` | MOV, DNxHD 185x interlaced, Quell-Feldreihenfolge; 25 Frames/s ohne Framerateänderung; Audio/Metadaten wie oben | 1920×1080, proportional ausfüllen |

`video.fit = "contain"` (Standard) passt proportional ein und füllt Überstand mit Schwarz auf. `video.fit = "cover"` vergrößert proportional bis der Zielrahmen vollständig gefüllt ist und schneidet Überstand mittig ab. Nach vertikalem Beschnitt bei gleichem Quell-/Zielrahmen wird dadurch auch seitlich beschnitten; dort können echte Bildinhalte betroffen sein. Beispiel: 1920×1080, oben/unten je 50+10 Pixel weg → 1920×960 → 2160×1080 → links/rechts je 120 Pixel weg. Die Master-Presets verwenden die Quellgröße statt eines Rasters; nur DNxHD verwendet 1920×1080. Eigene Raster-Presets bleiben nutzbar.

Beispiel `transcoder/presets/h264_10mbit_1080p.toml`:

```toml
container = "mp4"
faststart = true

[video]
codec       = "libx264"
profile     = "high"
pix_fmt     = "yuv420p"
color       = "bt709"
bitrate     = "10M"
maxrate     = "10.5M"
bufsize     = "20M"
x264_preset = "medium"
frame       = "1920x1080"
scan        = "progressive"

[audio]
codec       = "aac"
bitrate     = "320k"
sample_rate = 48000
layout      = "stereo"
```

| Eintrag | Bedeutung |
|---|---|
| `extends` | Name eines anderen Presets als Grundlage |
| `container`, `faststart` | Container (`mp4`, `mov`); `faststart` nur bei MP4 |
| `video.codec` | `libx264`, `h264_nvenc`, `dnxhd` |
| `video.profile` | H.264-Profil bzw. DNx-Profil (`dnxhr_hqx`, `dnxhd`) |
| `video.bitrate`, `maxrate`, `bufsize` | Bitrate und Regelung (H.264; bei DNxHD 185x die Profilbitrate `185M`) |
| `video.x264_preset` | Geschwindigkeit/Qualität von x264 |
| `video.nvenc_preset` | NVENC-Preset `p1` bis `p7`, Standard `p5`; gilt nur für `h264_nvenc` |
| `video.nvenc_tune` | NVENC-Tuning, für den Proxy `hq`; gilt nur für `h264_nvenc` |
| `video.nvenc_rc` | Bitratensteuerung, für den Proxy `vbr`; gilt nur für `h264_nvenc` |
| `video.pix_fmt`, `video.color` | Pixelformat, Farbraum |
| `video.frame` | Bildrahmen: `"<B>x<H>"`, `"raster"` oder `"source"` (Bild der Source) |
| `video.rasters` | bei `frame = "raster"`: erlaubte Raster, z. B. `["1920x1080", "2048x1080", "3840x2160", "4096x2160"]` |
| `video.scan` | `progressive`, `interlaced` (Feldreihenfolge der Source) oder `source` |
| `audio.codec` | `aac` oder `pcm` |
| `audio.bitrate`, `audio.sample_rate` | nur AAC |
| `audio.bit_depth` | nur PCM: `"source"` (mind. 16) oder `16`/`24` |
| `audio.layout` | `stereo` (eine Stereospur) oder `tracks` (je Kanal eine Mono-Spur) |

## Minimale App `sample.py`

Alle Einstellungen stehen im Kopf von `sample.py`; es gibt keine Kommandozeilenargumente. Start aus dem Projektordner:

```
python sample.py
```

Die App sucht Sources, ruft je Source `analyse` oder `render` auf, stellt Outputs und Segmente bereit, speichert Berichte und Fehlerliste und zeigt Meldungen und Fortschritt in Konsole und Log.

### Einstellungen

| Eintrag | Standard | Bedeutung |
|---|---|---|
| `mode` | `"analyse"` | `"analyse"`: Berichte schreiben, nichts rendern. `"render"`: Outputs rendern |
| `inputs` | `[]` | Dateien und/oder Ordner |
| `recursive` | `True` | Ordner samt Unterordnern |
| `media_suffixes` | `(".mov", ".mxf", ".mp4", ".wav")` | Endungen, die als Mediendatei zählen |
| `group_op_atom` | `True` | zusammengehörige OP-Atom-Dateien als eine Source (siehe unten) |
| `checks` | `["streams", "interlace", "black_bars", "audio"]` | Prüfungen im Modus `analyse` |
| `settings` | `None` | Einstellungen je Prüfung (siehe „Einstellungen“); gelten für Analyse und alle Fixes ohne eigene `settings`; `None` = Standardwerte |
| `report_folder` | `"reports"` | Ziel von Analysebericht und Fehlerliste; relativ zum Ordner von `sample.py` |
| `outputs` | | Outputs je Source im Modus `render` (siehe unten) |
| `source_settings` | `{}` | Einstellungen je Source, Schlüssel `{stem}` |

### Sources finden

- Jede Mediendatei ist eine Source, außer sie gehört zu einer OP-Atom-Gruppe.
- OP-Atom: MXF-Dateien mit Operational Pattern OP-Atom und derselben Material-Package-ID im selben Ordner sind eine Source (Angaben aus `analyse(…, "streams")`); die App gibt alle Dateien der Gruppe an `source`. `{stem}` ist dann der gemeinsame Clipname aus den MXF-Angaben, sonst der Name der Videodatei.
- Unvollständige Gruppen (z. B. Video ohne Ton) kommen auf die Fehlerliste.

### Outputs

```python
outputs = [
    {"preset": "h264_10mbit_1080p", "file": "D:/out/web/{stem}_10Mbit.mp4",
     "fixes": [transcoder.fix("audio", channels=2), transcoder.fix("black_bars"), transcoder.fix("interlace")]},
    {"preset": "dnxhr_hqx", "file": "D:/out/edit/{stem}.mov",
     "fixes": [transcoder.fix("audio", min_audible=2)]},
]
```

| Schlüssel | Bedeutung |
|---|---|
| `preset` | Name des Presets |
| `file` | Zieldatei; `{stem}` = Name der Source ohne Endung |
| `fixes`, `modify`, `merge`, `timecode` | wie bei `output()` im Kern; optional |

### Use-Case-Logik

Zwischen Analyse und Rendern ruft die App `decide(source, reports, outputs)` auf. Die Funktion steht in `sample.py`; dort gehört die Logik des jeweiligen Use-Cases hin, z. B. welche Outputs eine Source bekommt oder welche Segment-Dateien zu ihr passen. Sie bekommt die Einträge aus `outputs` für diese Source (`{stem}` eingesetzt) und gibt die Einträge zurück, die gerendert werden sollen – auch geänderte, z. B. `{**outputs[0], "merge": [...]}` –, oder einen Text, wenn die Source mit diesem Grund auf die Fehlerliste soll. Bricht `decide()` mit einem Fehler ab, kommt die Source auf die Fehlerliste. Ohne eigene Logik werden alle `outputs` gerendert, ohne vorherige Analyse.

```python
def decide(source, reports, outputs):
    """Second output only if exactly one channel is audible."""
    return outputs if len(reports["audio"]["audible"]) == 1 else outputs[:1]

decide_checks = ["audio"]    # Prüfungen, die decide() braucht; leer = keine Analyse vor dem Rendern
```

### Einstellungen je Source

```python
source_settings = {
    "film_0815": {"tc": "10:00:00:00", "modify": [transcoder.crop(0, 140, 1920, 800)]},
}
```

| Schlüssel | Bedeutung |
|---|---|
| `tc` | Start-TC der Source |
| `modify` | feste Änderungen für alle Outputs dieser Source; ersetzen Fixes derselben Art |
| `outputs` | Liste von Indizes in `outputs`, z. B. `[0]`: nur diese Outputs |

### Ausgaben

| Datei | Inhalt |
|---|---|
| `<report_folder>/analyse_<Zeitstempel>.txt` | Berichte aller Sources, nach jeder Source fortgeschrieben (Modus `analyse`) |
| `<report_folder>/fehlerliste_<Zeitstempel>.txt` | Outputs mit `error` und Fehlerbericht, Sources, die `decide()` aussortiert, nicht lesbare Sources und unvollständige OP-Atom-Gruppen |
| Zieldateien aus `outputs` | gerenderte Dateien |
| `<Zieldatei>.render.json` neben jeder gerenderten Datei | Ergebnis des Outputs (Preset, Fixes, Änderungen, Segmente, Timecode, ffmpeg-Aufruf, Dauer) |
| `sample.log` neben `sample.py` | alle Konsolenzeilen außer der Fortschrittszeile |

In der Konsole beginnt jede Zeile mit der Uhrzeit; beim Rendern steht darunter eine Fortschrittszeile (Prozent, Medienzeit, fps, Tempo, Restzeit), die die nächste Zeile ersetzt. Am Ende folgt eine Zusammenfassung mit den Pfaden von Analysebericht und Fehlerliste. Ungültige Einstellungen meldet die App vor dem Start und beendet sich. Ein Output mit `error` hält die übrigen nicht auf. Beenden mit `Strg+C`: ffmpeg wird beendet, Teildateien werden gelöscht.

## NVIDIA-Proxy

DNxHD 185x und DNxHR werden per CPU decodiert. Analyse, Audioverarbeitung, Beschnitt, Skalierung und Deinterlacing bleiben auf der CPU. Nur das H.264-Encoding des Proxys verwendet NVENC; neue DNx-Master werden weiterhin per CPU encodiert. Für DNx-Quellen kein `-hwaccel cuda` einschalten.

### ffmpeg und Treiber prüfen

Im Projektordner mit dem tatsächlich verwendeten ffmpeg prüfen; hier für die Installation in `res/` unter Windows:

```powershell
.\res\ffmpeg.exe -hide_banner -encoders
.\res\ffmpeg.exe -hide_banner -h encoder=h264_nvenc
.\res\ffmpeg.exe -hide_banner -f lavfi -i color=c=black:s=1920x1080:r=25 -frames:v 1 -an -c:v h264_nvenc -preset p5 -tune hq -rc vbr -b:v 10M -maxrate 10.5M -bufsize 20M -f null -
```

Die Encoderliste muss `h264_nvenc` enthalten. Erst der letzte Befehl prüft zusätzlich, ob der Encoder mit GPU und Treiber startet; er schreibt keine Mediendatei. Bei einer anderen ffmpeg-Installation deren ausführbare Datei verwenden, nicht versehentlich einen anderen Build aus dem Systempfad.

### Preset auswählen

Preset `transcoder/presets/h264_10mbit_1080p_nvenc.toml`:

```toml
extends = "h264_10mbit_1080p"

[video]
codec        = "h264_nvenc"
nvenc_preset = "p5"
nvenc_tune   = "hq"
nvenc_rc     = "vbr"
```

`extends` übernimmt Container, H.264 High, Pixelformat, BT.709, Bildgröße, Bildrate, AAC-Stereo, Faststart und Bitratenvorgaben des CPU-Presets. `nvenc_preset`, `nvenc_tune` und `nvenc_rc` entsprechen den ffmpeg-Optionen `-preset`, `-tune` und `-rc`. Das geerbte `x264_preset` gilt nur für libx264 und wird mit NVENC nicht verwendet.

Im Marathon-Adapter `proxy_preset = "h264_10mbit_1080p_nvenc"` wählen. Für expliziten CPU-Betrieb `proxy_preset = "h264_10mbit_1080p"` setzen. Bei einer EXE gelten Änderungen erst nach einem neuen Build. Für andere Aufrufer bleibt das Preset frei wählbar; ihre bisherigen Presets ändern sich nicht.

Ist NVENC ausgewählt, prüft der Worker den Encoder vor der Jobübernahme. Bei fehlendem Encoder oder nicht nutzbarer GPU/Treiber-Kombination bricht er mit einer konkreten Fehlermeldung ab. Es gibt keinen automatischen Wechsel auf CPU-Encoding.

### Lokal vergleichen

Repräsentative DNx-Clips einschließlich zugehöriger Audiotracks lokal bereitstellen. CPU- und NVENC-Preset mit denselben Quellen, Fixes und Ausgabebedingungen vergleichen; für den Speichereinfluss zusätzlich dieselben Quellen von der Freigabe verwenden. Nur in einer lokalen Testumgebung arbeiten, nicht mit produktiven Marathon-Jobs. Laufzeit, fps, speed, Audio/Timecode und Bildqualität insbesondere bei Filmkorn prüfen. Identische Bitratenvorgaben bedeuten keine identische Bildqualität.

Quellen zur Hardware-Unterstützung: NVIDIA Video Codec SDK 13.0, „Using FFmpeg with NVIDIA GPU Hardware Acceleration“, Abschnitt 3, und „NVDEC Video Decoder API Programming Guide“, Abschnitt 1.1.

## Marathon-Adapter

Angeflanschte Fernsteuerung für den Marathon-Use-Case: Der Adapter holt Transcode-Jobs aus dem Marathon-Pool, übersetzt sie in Aufrufe des Kerns (`source`, `analyse`, `output`, `render`) und die Ergebnisse in Reports an Marathon. Er nutzt den Kern wie jeder andere Aufrufer und ändert nichts an dessen Funktionslogik, an den Presets oder an der App; seine Use-Case-Logik steht nur in `transcode_marathon.py`.

### Aufbau

| Pfad | Aufgabe |
|---|---|
| `marathon/marathon_adapter.py` | allgemeiner Teil aller Marathon-Adapter, Kopie der Referenz 1.3.0, **nicht verändern** |
| `marathon/transcode_marathon.py` | Transcode-Adapter: Einstellungen, Use-Case-Logik, Einstieg der EXE |
| `marathon/build_transcode.py` | baut die EXE |

### Starten

Voraussetzung: Marathon 1.17.0 (`schema_version` 6) und Marathon-Adapter 1.3.0.

Als Skript aus dem Projektordner:

```
python -m marathon.transcode_marathon
```

Als EXE: `transcode_marathon_<version>.exe` auf den Rechner kopieren und starten. Der Rechner braucht Lese- und Schreibzugriff auf die Marathon-Freigabe und auf `master_dir`.

- Der Worker heißt `<Rechnername>-Transcode`. Je Rechner läuft ein Transcode-Worker; ein zweiter Start auf demselben Rechner bricht ab.
- Er bearbeitet einen Job nach dem anderen. Ohne Jobs schaut er alle `poll_seconds` erneut in den Pool.
- Beenden mit `Strg+C`. ffmpeg wird beendet, Teildateien werden gelöscht; der Job wird beim nächsten Start als `failed` gemeldet, Marathon legt ihn neu aus.
- Log `transcode_marathon.log` neben der EXE bzw. in `marathon/`; enthält Meldungen, verwendeten Encoder, Zeiten je Analyseschritt und Renderdurchlauf sowie Fortschrittswerte (`fps`, `speed`, Position, ETA) im Abstand von `log_progress_seconds`. Laufzeiten werden unabhängig von Änderungen der Systemuhr gemessen.
- Pfade werden in Konsole und Log gekürzt: ab `.marathon` (`.marathon\transcode\…`) bzw. ab `++ Master repariert\…`. Report und Protokolle enthalten vollständige Pfade.

### Use-Case-Logik

Je Job: Source aus `inputs`, Analyse (`streams`, `audio`, `black_bars`, `interlace`), dann ein `render`:

- Proxy immer: `proxy_preset` mit `fix("audio", channels=2)`, `fix("black_bars")`, `fix("interlace")`.
- Neuer Master bei einem hörbaren Kanal, entfallenden stummen Kanälen, oberen/unteren schwarzen Balken oder `"ffe_tafel": true`:
  - progressiv: `master_preset` mit `fix("audio", min_audible=2)` und `fix("black_bars")`,
  - interlaced: `master_preset_interlaced` mit Audio-Fix und vertikalem Beschnitt; feldgerecht skalieren, Feldreihenfolge erhalten.
- Dual-Mono erzeugt keinen neuen Master.
- Ist ein neuer Master nötig und das Interlacing unklar, endet der Job ohne Rendern mit `rejected`.
- FFE-Tafel bei `"ffe_tafel": true`: Der Adapter wählt aus `ffe_reference_clips` die eine Tafel mit Bildgröße und fps der Source (keine oder mehrere → `rejected`). Proxy und Master bekommen `merge = [<Tafel>, black(ffe_black_seconds), SOURCE]` und `timecode(anchor=SOURCE)`: Tafel (9 s) plus 1 s Schwarz = 10 s vor dem Film, der Film beginnt beim alten Start-TC.
- Scheitert ein Output des Jobs, löscht der Adapter die übrigen Mediendateien des Jobs (ihre `.render.json` bleiben); Marathon bekommt nur vollständige Ergebnisse.

### Verarbeitung und Fehler

- Nach dem Lesen der Streams zuerst fehlenden Ton und bei FFE-Jobs die passende Tafel prüfen. Nach der Audioanalyse die benötigten Audiobelegungen prüfen. Steht eine Ablehnung bereits fest, keine Rand- oder Interlaceanalyse mehr starten.
- Bei identischen Stichprobeneinstellungen Ränder und Interlace gemeinsam aus einmal decodierten Stichproben bestimmen. Individuelle Einstellungen je Prüfung und `samples.full` bleiben nutzbar. Die Vertrauensschwellen bleiben unverändert; die Balkenauswertung berücksichtigt ausschließlich obere/untere schwarze Ränder.
- Bei erfolgreichem Rendern Proxy und ggf. Master weiterhin in einem gemeinsamen ffmpeg-Durchlauf erzeugen; die Quelle nur einmal einlesen.
- Den Scanmodus des Marathon-Masters an der eindeutigen Interlaceanalyse ausrichten: progressive Ausgabe mit DNxHR HQX, interlaced Ausgabe mit DNxHD 185x und der ermittelten Feldreihenfolge. Veraltete `-top`-Encoderoptionen nicht verwenden.
- Bei erkannten ungültigen Encoderoptionen oder einer ffmpeg-Assertion kein erneutes internes Rendern der Einzeloutputs versuchen. Fehler ausdrücklich protokollieren. Nicht als dauerhaft erkannte Fehler können weiterhin in Einzeloutputs eingegrenzt werden.
- Die Vollständigkeitsregel bleibt bestehen: Nur ein vollständiger Job ist erfolgreich; bei einem fehlgeschlagenen Output werden die anderen Mediendateien weiterhin entfernt. Externe Wiederholungen entscheidet Marathon, nicht der Transcoder.
- ffmpeg-Assertions werden nicht als behoben vorausgesetzt. Für einen konkreten Absturz einen lokal reproduzierenden Quellclip, den verwendeten Build und das Renderprotokoll bereitstellen; einen anderen Build erst nach lokalem Vergleich einsetzen.

### EXE bauen

```
python marathon/build_transcode.py
```

Voraussetzung: `ffmpeg` und `ffprobe` liegen in `res/`, sonst bricht der Build ab. Ergebnis: `dist/transcode_marathon_<app_version>.exe` mit Adapter, Kern, `transcoder/presets/` und `res/`. Die Version steht im Dateinamen und in den Windows-Dateieigenschaften. Änderungen an Einstellungen oder Presets gelten erst mit einem neuen Build.

### Einstellungen in `transcode_marathon.py`

| Eintrag | Wert | Bedeutung |
|---|---|---|
| `app_version` | | Version der EXE |
| `root_path` | `\\10.0.77.11\Ablage KI Proxy_1\Proxy 10 Mbit` | Wurzel der Marathon-Freigabe, wie `root_path` in Marathons `res/config.ini` |
| `work_dir` | `.marathon` | Arbeitsordner von Marathon, wie `work_dir` in Marathons `res/config.ini` |
| `master_dir` | `\\10.0.77.11\Ablage KI Proxy_1\++ Master repariert` | nur zum Kürzen der Pfade, wie `[ingest] master_dir` |
| `stage` | `Transcode` | Stufe |
| `poll_seconds` | `30` | Wartezeit, wenn der Pool leer ist |
| `heartbeat_seconds` | `30` | Abstand der Lebenszeichen an Marathon, 1 bis 60 |
| `proxy_preset` | `h264_10mbit_1080p_nvenc` | NVIDIA-Proxy; `h264_10mbit_1080p` für expliziten CPU-Betrieb |
| `master_preset` | `dnxhr_hqx` | Master aus progressiven Quellen: Quellbildgröße, proportionaler Beschnitt/Zoom, PCM-Einzelspuren 48 kHz/24 Bit |
| `master_preset_interlaced` | `dnxhd_185x` | Master aus interlaced Quellen: 1920×1080, Quell-Feldreihenfolge, 25 Frames/s; andere fps → Fehler ohne Transcode |
| `ffe_black_seconds` | `1` | Schwarz zwischen Tafel und Film |
| `settings` | `{"samples": {"per_minute": 5, "seconds": 2}, "black_bars": {"extra_crop": 10}}` | Marathon-Vorgaben: 5 Stichproben pro Minute à 2 Sekunden, zusätzlicher Beschnitt 10 Pixel; QC-Schwellen unverändert |
| `log_progress_seconds` | `30` | Abstand der Fortschrittswerte im Log; laufende Konsolenanzeige bleibt erhalten |

Die Halbierung betrifft nur die Marathon-Vorgabe `settings.samples.per_minute`: 5 statt 10. `seconds = 2`, `full = false`, Vertrauensschwellen, Audio-Grenzwerte und `black_bars.extra_crop = 10` bleiben unverändert. Die Standardwerte des Kerns und anderer Apps werden nicht halbiert. Bei kurzen Clips können weniger Stichproben häufiger zu einer unsicheren Bewertung führen; die QC-Schwellen werden dafür nicht abgesenkt. Es werden keine Quellen automatisch auf lokale Laufwerke kopiert.

### Ergebnis eines Jobs

- Im `output_folder` des Jobs: Proxy `proxy_name`, ggf. neuer Master `<identifier>__<title>.mov` (unzulässige Zeichen `<>:"/\|?*` → `_`), je Datei `<Datei>.render.json`.
- Report an Marathon in `report_folder`: `<job_id>.json` mit `status`, `result`, `preset`, `new_master`, `worker` und `versions`.

| Ergebnis | `status` | `result` |
|---|---|---|
| alle Outputs `ok` | `ok` | Kurztext |
| `error` aus Analyse, Fix, Segmenten oder Tonlayout (`step` `fix:…`, `merge`, `audio`, `source`), keine passende Tafel, neuer Master bei unklarem Interlacing | `rejected` | `Manuelle Prüfung: <Fehlerbericht>` |
| `error` mit `step` `ffmpeg`, `file`, `preset` oder `timecode`, Source nicht lesbar, Job ungültig | `failed` | Fehlerbericht bzw. `Job ungültig: …` |

Marathon 1.18.0 behandelt Transcode-`rejected` als manuell blockiert, ohne erneuten Transcode-Versuch. Ältere Marathon-Versionen behandeln es noch wie `failed`. `preset` im Report: Proxy-Preset, mit neuem Master `<proxy_preset>+<master_preset>`. `new_master` nur, wenn ein neuer Master im Ausgang liegt; Marathon verschiebt ihn nach `master_dir`.

Ungültig sind Jobs ohne `inputs` (nicht leere Liste), ohne `proxy_name`, ohne `ffe_tafel` (`true`/`false`), mit `"ffe_tafel": true` ohne `ffe_reference_clips` und Jobs ohne `identifier` oder `title`. Der Adapter verarbeitet nur Jobs mit `schema_version = 6`.

### Dateien auf der Marathon-Freigabe

| Pfad | Verwendung |
|---|---|
| `<work_dir>/transcode/offen/` | Jobs abholen |
| `<work_dir>/transcode/laufend/<Rechnername>-Transcode/` | übernommene Jobs |
| `<work_dir>/transcode/fertig/` | Reports an Marathon |
| `<work_dir>/worker/<Rechnername>-Transcode.json` | Heartbeat |
| `<work_dir>/worker/FFE Filmerbe/*.mov` (`ffe_reference_clips`) | FFE-Tafeln, werden nur gelesen |
| `inputs` aus dem Job | Master, werden nur gelesen |
| `output_folder` aus dem Job | Proxy, neuer Master, Protokolle |

## Lokale Regressionstests

Die Tests erzeugen ausschließlich temporäre lokale Testdateien; keine Produktionsablage und keine Zugangsdaten werden verwendet. Im jeweiligen Repository starten:

```bash
python -m unittest discover -s tests -v
```

Für die Medienprüfungen müssen `ffmpeg`, `ffprobe` und `numpy` verfügbar sein. Es werden künstliche Bilder und Töne verwendet.
