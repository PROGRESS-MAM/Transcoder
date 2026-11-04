# Pflichtenheft – Ton: erwartete Stille LYNX, Kanalerkennung, QC-Ausnahme

Stand: 4. November 2026 · Status: Entwurf, Freigabe ausstehend · Umsetzung: Mastazilla
Basis: Online-Stand `main` von Transcoder (transcoder 0.3.1, transcode_marathon 0.3.4) und AQC (qc_marathon 2.2.2). Der lokale Stand weicht ab: Funktionen sind per Namen genannt, vor der Umsetzung abgleichen. Ersetzt `Pflichtenheft_Umsetzung.md`.

## Hintergrund (Auswertung 24.10.–03.11.2026)
- „Source hat keinen Ton“: 277 Einträge, 83 Clips, davon 18 LYNX-Clips `LA…`. Weitere 5 LYNX-Clips `LA…` haben Kanäle, aber keinen hörbaren.
- „Ton unsicher“: 525 Jobs. Ursache: „stumm“ gilt erst bei statistischem Nachweis Tonanteil < 1 % als sicher, für trust 0.90 also ab 224 Messfenstern (ab ca. 23 min). Hörprobe: je 2 Kanäle deutlich hörbar, 2 stumm.

## Ziel und Umfang
- **A – Erwartete Stille (Transcoder):** Clips der Kollektion `LYNXarchive` mit Identifier `LA…` ohne Ton regulär als Proxy transcodieren statt manuell prüfen.
- **B – Kanalerkennung (Transcoder):** hörbar/stumm je Kanal über den Pegelvergleich der Kanäle je Messfenster; trust aus der Übereinstimmung der Messpunkte.
- **C – QC-Ausnahme (AQC):** Für dieselben Clips entfällt in QC die Stilleprüfung.
- Nicht im Umfang: Wiederfreigabe (Martin per Marathon `retry-transcode` nach dem Rollout, alle inaktiven Transcode-Clips, akzeptiert), Dublettenprüfung, Master-Erzeugung, andere Kollektionen, `marathon_adapter.py` (beide Repos).

## Gemeinsame Regel (A und C)
- CONFIG in `transcode_marathon.py` und `qc_marathon.py`: `expected_silence = {"LYNXarchive": ("LA",)}` (Kollektion → Identifier-Präfixe).
- Greift, wenn `job["collection"]` exakt passt und `job["identifier"]` (ohne Randleerzeichen) mit einem Präfix beginnt; Groß-/Kleinschreibung beachten. Beide Felder liefert Marathon in jedem Job (schema 6). Fehlt ein Feld, greift die Regel nicht.

## A – Erwartete Stille (`transcode_marathon.py`, `transcoder.py`, `_render.py`)
- `run_job`, Source ohne Kanäle + Regel: Audioanalyse und Audio-Fix entfallen, Proxy mit stiller Stereospur. Ohne Regel unverändert: `Analyse audio: Source hat keinen Ton.` → manuell.
- `run_job`, Kanäle vorhanden, `audible` leer + Regel (unabhängig vom trust): statt Audio-Fix `audio_map([1, 2])`, bei einem Kanal `[1, 1]`. Ohne Regel unverändert.
- Regel + `ffe_tafel` true: unverändert manuell.
- Logzeile „Ton fehlt erwartungsgemäß (LYNXarchive, LA…)“ mit der gewählten Tonlösung; `_analysis_text` läuft auch ohne Audiobericht. Report `ok`.
- `transcoder.output(..., silent_audio=True)` (neu, Standard `False`): Source ohne Kanäle → stille Stereospur über die volle Länge statt Fehler „Source hat keinen Ton“; nur bei Layout `stereo`, sonst Fehler. Ohne Option unverändert.

## B – Kanalerkennung (`_analysis.py`, `check_audio`)
- Je Messfenster (`silence_window`) RMS je Kanal in dBFS; digitale Null als fester Boden (−120 dB).
- Wertbar ist ein Fenster, in dem mindestens ein Kanal ≥ `silence_threshold` liegt; andere Fenster zählen nicht.
- Stimme je Kanal und wertbarem Fenster: „stumm“, wenn der Kanal < `silence_threshold` **und** mindestens `silence_gap` unter dem lautesten Kanal liegt; sonst „hörbar“.
- Entscheidung je Kanal: hörbar bei Anteil Hörbar-Stimmen ≥ 0.5; trust über `_rated(votes, points, 0.5, full)` wie bei Interlace und Dubletten; `samples.full` → 1.0.
- Kein wertbares Fenster: alle Kanäle stumm; dann zählen alle Messfenster als Stimmen „stumm“.
- Bericht je Kanal zusätzlich `points` (wertbare Messpunkte) und `votes` (Stimmen „hörbar“); übrige Felder unverändert.
- Einstellungen `audio`: `silence_threshold` bleibt `"-60dB"` (wie AQC), `silence_window` bleibt `"1s"`, neu `silence_gap` = `"20dB"` (größer 0), `silence_min_active` entfällt, `trust` bleibt 0.9.
- Unverändert: Stichproben (`spans`), Paarvergleich/Dubletten, Regeln und Meldungen von `fix_audio`, Rundung `_below`, Felder `active` und `peak_db`.

## C – QC-Ausnahme (Repo AQC, `qc_marathon.py`, `run_job`)
- Regel erfüllt: Prüfplan ohne die Tools `digital_silence` (aus `qc_plan` und ggf. `ffe_plan`); Planname im Report `<Planname>-stille_erwartet`, z. B. `proxy_standard-stille_erwartet`.
- Logzeile „Stille erwartet (LYNXarchive, LA…): digital_silence entfällt“. Alle anderen Tools, z. B. `stream_specs(audio_aac_stereo)`, laufen unverändert.
- Ohne Regel unverändert.

## Erfolgskriterien (lokale Tests mit synthetischen Signalen, keine Produktivdaten)
- A: `LYNXarchive` + `LA000674` + keine Kanäle → `ok`, Proxy mit genau einer AAC-Stereospur 48 kHz, digitale Stille, Länge wie das Video.
- A: `LYNXarchive` + `LA…` + nur stumme Kanäle → `ok`, Tonspur aus Kanal 1/2 bzw. 1/1.
- A/C: andere Kollektion (z. B. `DEFA Der Augenzeuge`, `LYNXarchive2`), Identifier `PROB1051` oder fehlende `collection` → unverändert; A zusätzlich `ffe_tafel` true → unverändert `rejected`.
- A: `transcoder.output` ohne `silent_audio` bei Source ohne Kanäle → unverändert Fehler.
- B: 4 Kanäle, 2 min: Kanal 1/2 Ton mit Pausen, Kanal 3/4 digitale Null bzw. Rauschen bei −80 dBFS → `audible` `[1, 2]`, jeder Kanal trust ≥ 0.9.
- B: leise echte Spur 25 dB unter Kanal 1, in 30 % der Fenster unter −60 dBFS → hörbar.
- B: Ton nur in 30 % der Zeit auf Kanal 1/2 → beide hörbar; Pausen ohne Einfluss.
- B: alle Kanäle durchgehend unter −60 dBFS → `audible` leer, `fix_audio` meldet „kein hörbarer Kanal“.
- C: stiller Stereo-Proxy + Regel → QC `ok` mit Planname `proxy_standard-stille_erwartet`; ohne Regel → `rejected` durch `digital_silence`.
- Bestehende Tests beider Repos grün; angepasst werden nur Tests der 1-%-Regel. READMEs (Einstellungen, Stille-Messung, LYNX-Regel, Prüfplan) und Versionen (`transcoder` 0.4.0, `transcode_marathon` 0.4.0, `qc_marathon` 2.3.0) aktualisiert.

## Grenzen
- Reihenfolge im Betrieb: Transcode- und QC-Worker neu ausrollen, dann `retry-transcode`.
- `silence_gap` ist ein Startwert; die Kalibrierung erfolgt über die nächste Auswertung.
- Risiko: Eine echte Spur, die überwiegend unter −60 dBFS und ≥ 20 dB unter dem Hauptton liegt, gilt als stumm.
- Abschätzung: Alle 525 Jobs „Ton unsicher“ hatten laut zurückgerechnetem trust ≥ 12 Messfenster. Übereinstimmende wertbare Messpunkte ergeben trust 0.94 ab 3 und 0.99 ab 6 Punkten.
