# Änderung: OP-Atom-Verweis-Streams im Marathon-Transcode ignorieren

Stand: `main` @ `2eb9b4b` · nur `marathon/transcode_marathon.py` ändern, `transcoder/` bleibt unverändert.

## Problem

Marathon weist Clips mit `Master nicht kompatibel: Zusätzliche Quellstreams nicht sicher übertragbar: data, data, …` ab
(58 von 384 Abweisungen im Bericht vom 2026-10-30).

Ursache: Bei MXF OP-Atom (je Spur eine Datei) listet jede Datei im Material Package alle Spuren des Clips.
ffprobe meldet die Spuren der **anderen** Dateien als leere `data`-Streams. 5 Dateien × 4 Verweise = 20, 3 Dateien × 2 = 6.

Geprüft an 3 Clips (722578, 413006, 412031) mit `probe_data_streams.py`, alle Verweis-Streams:

- `codec_type = data`, kein `codec_name`, `codec_tag_string = [0][0][0][0]`
- `tags.data_type` = `video` oder `audio`
- `tags.file_package_umid` = UMID der echten Spur in einer anderen Datei desselben Clips
- 0 Pakete, 0 Bytes, Dauer = Clipdauer
- `format.tags.operational_pattern_ul` = `060e2b34.04010102.0d010201.1002…` bzw. `….1003…` (OP-Atom)

Echte Video-/Audiospuren tragen ebenfalls `tags.file_package_umid` (ihre eigene UMID).
Dateinamen sind **nicht** zur Zuordnung geeignet (412031: A01 und A02 haben denselben Namensteil).

## Ablauf heute (Marathon)

1. `run_job` → `source = _render_source(...)` (`transcode_marathon.py` ~Z. 288)
2. `_render.check_source_encoder(source, master)` (Z. 302) → Regel in `_render.py` Z. 463–467
3. `transcoder.render(source, items)` → `_render.prepare` → **erneut** `check_source_encoder` (`_render.py` Z. 181),
   weil das Master-Preset `preserve_metadata = true` hat

Beide Prüfungen lesen `source.probes`. Deshalb wird die Quelle **vor** Schritt 2 bereinigt; dann greifen beide Prüfungen ohne Änderung am Kern.

## Umsetzung

In `marathon/transcode_marathon.py`:

- Neue Funktion `_without_opatom_refs(source: Source) -> tuple[Source, int]`
  - Eigene UMIDs: `tags.file_package_umid` aller Streams mit `codec_type` `video`/`audio`, je Datei-Index.
  - Ein Stream ist Verweis, wenn **alle** Bedingungen zutreffen:
    - Datei ist OP-Atom: `format.tags.operational_pattern_ul` enthält `.0d010201.10` (Groß-/Kleinschreibung egal)
    - `codec_type == "data"`
    - `tags.data_type` in `video`, `audio`
    - `tags.file_package_umid` gehört zu einer echten Spur einer **anderen** Datei derselben `Source`
  - Rückgabe: `replace(source, probes=...)` mit Probes ohne diese Streams (übrige Felder der Probe-Dicts unverändert, keine Mutation des Originals) und Anzahl entfernter Streams.
- Aufruf in `run_job` direkt nach `_render_source(...)`, vor der Master-Entscheidung:
  `source, refs = _without_opatom_refs(source)`; bei `refs > 0` ein Logeintrag über `adapter.log`, z. B. `OP-Atom-Verweise ignoriert: 20`.
- `app_version` von `0.3.1` auf `0.3.2`.

Nicht ändern:

- `transcoder/_render.py`, `transcoder/_source.py`, `transcoder/_analysis.py`, Presets
- Verweise, deren UMID zu keiner Datei im Clip passt, bleiben drin → weiterhin `Master nicht kompatibel` (fehlende Spur = echtes Problem)
- Alle anderen `data`-/Zusatz-Streams bleiben drin → Verhalten wie bisher

## Unbedenklichkeit (geprüft im Code @ `2eb9b4b`)

`source.probes` wird sonst nur gelesen für: Kapitel (`_render.py` Z. 178, 526), Stream-Bericht (`_analysis.py` Z. 162, Analysen laufen vor der Bereinigung) und die Regel selbst (Z. 463).
Stream-Zuordnung nutzt `StreamRef.ordinal` je Typ (`0:v:n`/`0:a:n`), schon beim Erzeugen der `Source` berechnet; keine Nutzung absoluter Stream-Indizes gefunden.

## Erfolgskriterien

- Clips 722578, 413006, 412031: kein `Master nicht kompatibel` mehr, Log zeigt 20 / 20 / 6 ignorierte Verweise.
- Fehlt eine Partnerdatei (z. B. A04 entfernt): Verweis auf A04 bleibt → weiterhin `Master nicht kompatibel`.
- Nicht-OP-Atom-Quelle mit `data`-Stream: unverändert abgewiesen.
- Tests in `tests/` für `_without_opatom_refs` mit synthetischen Probe-Dicts: 5-Datei-Clip, 3-Datei-Clip, fehlende Partnerdatei, Nicht-OP-Atom, Original-`Source` unverändert.
