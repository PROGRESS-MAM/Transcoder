# Pflichtenheft: One-Off-Proxyreparatur

**Stand:** 29. Oktober 2026. **Status:** Pflichtenheft zur Freigabe. Referenzanalyse und Befunde liegen vor; der neue Reparaturweg ist noch nicht durch Probe-Reparaturen bestätigt. Kein Implementierungscode wird mit diesem Dokument freigegeben.

## 1. Ziel und Grundlage

- One-Off-Transcoder im Repo Transcoder mit Python, FFmpeg und ffprobe auf die Proxy-Zeitstempelreparatur ausrichten und anschließend als Windows-EXE bauen. Keine Abwärtskompatibilität; bisherige QC-Listen, Bitraten-, Bild- und Audiokorrekturen sind für diesen Ablauf nicht erforderlich.
- Datenerfassung und Auswertung aus `oneoff/analyse_referenzclips.py` und `oneoff/analysis` wiederverwenden. Die allgemeine Anomaliemeldung der vorhandenen Auswertung ist nicht gleichbedeutend mit dem hier zu reparierenden Fehlerbild; dafür ist eine eigene, nachvollziehbare Entscheidung erforderlich.
- Referenztests: `58238241.mp4` und `58238527.mp4` müssen das bekannte Fehlerbild zeigen; `53287289.mp4` muss als unauffälliger Vergleich ohne B-Frames erkannt werden. Die Referenznamen dürfen außerhalb der Testauswahl keine Diagnose ersetzen.

## 2. Externe Konfiguration und Betriebsmodi

- `config.ini` direkt neben der EXE lesen, unabhängig vom aktuellen Arbeitsverzeichnis. Änderungen erfordern keinen neuen EXE-Build. Vorgesehene Vorlage:

```ini
[paths]
source_path = \\10.0.77.102\FiMu 1.5 Mbit Proxy
target_path = \\10.0.77.102\FiMu 1.5 Mbit Proxy\fixed

[repair]
repair-test = true
```

- Fehlende oder ungültige Config, unlesbarer Quellpfad, nicht nutzbarer Ziel-/Berichtspfad oder identische Quell- und Zielordner: vor Verarbeitung abbrechen und konkret melden. `true` und `false` sind die zulässigen Schalterwerte; keine stillschweigenden Ersatzpfade oder automatische Umschaltung auf Serienbetrieb.
- **`repair-test = true`:** ausschließlich `58238241.mp4` und `58238527.mp4` als Kandidaten auswählen, vollständig analysieren und anhand des Fehlerbilds reparieren. Bereits vorhandene Zielclips auch hier überspringen; fehlende Testquellen melden. Keine anderen Clips verarbeiten. `53287289.mp4` bleibt Referenz für die Erkennungsprüfung, gehört nicht zur Reparatur-Testauswahl.
- **`repair-test = false`:** alle nach dem Zielabgleich verbleibenden Clips unmittelbar im Quellordner vollständig analysieren und **nur bestätigte Fälle des bekannten Fehlerbilds** reparieren. Unauffällige Clips unverändert lassen und nicht ausgeben; unklare oder abweichende Fehlerbilder gesondert melden und nicht automatisch reparieren.

## 3. Auswahl, Zielabgleich und Analyse

- Quellordner ausschließlich auf seiner unmittelbaren Ebene scannen; nicht rekursiv, keine Unterordner und keine `.part`-Dateien verarbeiten. Der Unterordner `fixed` ist somit keine Eingabe.
- Vor der Analyse die endgültigen Clipdateien unmittelbar im Zielordner erfassen. Bei gleichem vollständigem Dateinamen einschließlich Erweiterung, unter Beachtung der Windows-Groß-/Kleinschreibung, den zugehörigen Quellclip überspringen. Dies gilt in beiden Modi; übersprungene Clips werden protokolliert, nicht erneut analysiert oder überschrieben. Eine `.part`-Datei zählt nicht als abgeschlossener Zielclip.
- Jeden übrigen Kandidaten vollständig wie im Referenzlauf analysieren: Container, sämtliche Streams, Metadaten, Timecodes, Audioeigenschaften, Bildformat/-rate, Zeitbasen, Bitraten, Dauern; alle Videopakete und ausgegebenen Bilder mit PTS/DTS, Positionen, Flags und I/P/B-Typen. Gespeicherte Zeitstempel und Decoder-Schätzwerte getrennt halten.
- Erfassung, technische Diagnose, bekannte Fehlerklasse und Reparaturentscheidung getrennt protokollieren. Unklare, nicht analysierbare oder nicht sicher unterstützte Dateien nicht stillschweigend als gesund oder repariert behandeln.

## 4. Nachgewiesenes Fehlerbild

- Für die bekannte Klasse gemeinsam nachweisen: H.264 mit B-Bildern und bestätigter konstanter Bildrate; regelmäßige DTS; eindeutige Bild-/Paketzuordnung; wiederholte identische PTS für unterschiedliche B- und P-Bilder sowie Rückschritte/gleiche PTS in der tatsächlichen Decoder-Ausgabereihenfolge. Das in den Referenzen beobachtete `PTS = DTS` der B-Bilder muss als Bestandteil des Musters mitgeprüft werden.
- In beiden Fehlerreferenzen beginnt die ausgegebene Bildfolge mit `I, B, B, B, P` und gespeicherten PTS `0, 200, 100, 300, 300` statt dem gleichmäßigen Raster `0, 100, 200, 300, 400`. Bei `58238241.mp4` wurden 5.618, bei `58238527.mp4` 10.467 doppelt belegte PTS-Werte nachgewiesen; beim Vergleichsclip keine.
- Ganzzahlige Zeitstempel und rationale Zeitbasen verwenden. 24 fps und 100 Ticks pro Bild sind Referenzwerte, keine globale Vorgabe für andere Clips. Fehlende oder widersprüchliche Bildraten, variable Bildraten, mehrere Videostreams oder abweichende Fehlerbilder gesondert melden, nicht mit geratenen Parametern reparieren.
- B-Frames, Main-Profil, Paket-PTS-Rücksprünge in Decodierreihenfolge, Dateinamen oder Discard-Flags allein reichen nicht aus. Bilder nicht nach fehlerhaften PTS oder nach `best_effort_timestamp` neu sortieren. Allgemeine Anomalien wie fehlende Zeitstempel oder rückwärts laufende DTS sind nicht automatisch dieselbe Fehlerklasse.

## 5. Reparatur und Informationserhalt

- Für bestätigte Fehlerfälle die reguläre Decoder-Ausgabe verwenden, eine gleichmäßige Präsentationszeitachse aus Bildnummer und bestätigter Quellbildrate erzeugen und das Video neu als H.264 **ohne B-Frames** encodieren. Nur `-bf 0`, ein Remux oder `+genpts` allein gelten nicht als Reparatur. Ungewolltes Duplizieren/Verwerfen durch Ausgabesynchronisation verhindern.
- Quell-Videobitrate als Zielwert übernehmen, kein pauschales anderes Preset. Die Referenz-Videobitraten liegen ungefähr bei 1,2 Mbit/s; der Ordnername „1.5 Mbit Proxy“ ersetzt keine Messung. Tatsächliche Ausgabebitrate protokollieren; Neu-Encoding ist nicht verlustfrei und garantiert keine exakt identische Dateigröße/Bitrate.
- Auflösung, Seitenverhältnis, bestätigte Bildrate, Pixelformat und Farbkennzeichnung je Quelle erhalten. Kein Skalieren, Beschneiden, Deinterlacing oder Übertragen des 1920-×-1080-Rasters des sauberen Clips auf die 2048-×-1080-Fehlerclips.
- Sämtliche Audiostreams unverändert kopieren und ihren Zeitbezug erhalten; kein Resampling, Downmix, Umordnen, Entfernen oder Anpassen der Audiodauer an das Video. Sämtliche weiteren Quellinformationen, Container-/Streammetadaten, Kapitel und tatsächlichen Timecode-/Datenspuren einschließlich `tmcd` erhalten. Nicht sicher erhaltbare Informationen verhindern die Veröffentlichung.
- **Akzeptierter Endfall:** In der bekannten Klasse darf genau ein am Clipende bereits als Discard markiertes und vom normalen Decoder nicht ausgegebenes Bild fehlen. Die Referenzen liefern 28.089 bzw. 52.337 Bilder, jeweils eines weniger als im Container angegeben. Eine um ein Bild kürzere Videodauer ist akzeptiert; Audio bleibt unverändert.
- Discard-Pakete regulär decodieren lassen, nicht vor dem Decoder entfernen. Keine pauschale Änderung der Editlisten, kein zusätzliches Abschneiden und keine automatische Schlussbild-Duplikation. Weitere Bildverluste, fehlende Bilder an anderen Positionen oder ein fehlgeschlagener Encoding-Lauf sind kein tolerierter Ein-Bild-Endfall.

## 6. Prüfung und sichere Veröffentlichung

- Erwartete Ausgabebildanzahl aus der vollständigen Decoder-Ausgabe bestimmen. Nach dem Encoding jede Ausgabe erneut prüfen: Bildanzahl/-reihenfolge, konstante Zeitabstände, keine PTS-Kollisionen/-Rückschritte in der ausgegebenen Bildfolge, keine B-Frames, Bildformat, erwartete Videodauer und A/V-Zeitbezug.
- Audio-Payload und Streameigenschaften, sämtliche geforderten Metadaten, Kapitel und tatsächliche Timecode-/Datenspuren gegen die Quelle prüfen. Keine allgemeine Fehlermeldung oder ein erfolgreiches FFmpeg-Ende ersetzt diese Nachprüfung. Akzeptierte Ein-Bild-Differenz explizit ausweisen; sonstige ungeklärte Abweichungen verhindern den Abschluss.
- Zieldateiname **1:1 wie die Quelle einschließlich Erweiterung**. Zunächst ausschließlich in eine `.part`-Datei im Zielordner schreiben. Erst nach erfolgreicher Prüfung und geschlossenen Dateizugriffen atomar auf den endgültigen Namen umbenennen; das Verhalten auf dem SMB-Ziel prüfen.
- Vorhandene Ziel- und `.part`-Dateien nicht überschreiben oder automatisch löschen. Auch eine während der Verarbeitung neu erschienene Zieldatei darf nicht überschrieben werden. Bei Konflikt, Abbruch oder fehlgeschlagener Prüfung keine endgültige Datei veröffentlichen; die unvollständige Teildatei und Ursache melden. Quelldateien bleiben unverändert.

## 7. Protokolle, Abnahme und nächste Freigabe

- Für jeden Lauf einen neuen Ordner unter `reports` neben der EXE anlegen. Config-Werte, Werkzeugversionen, Aufrufe und Meldungen sichern; pro analysiertem Clip vollständige JSON-Rohdaten, technische Befunde, Entscheidung und Ausgabeprüfung dauerhaft speichern. Lesbarer Markdown-Laufbericht und laufende Fortschrittsanzeige; frühere Berichte nicht überschreiben.
- Übersicht über Auswahl, bereits vorhandene Ziele, unauffällige/unklare Befunde, Analysefehler, Reparaturfehler und erfolgreich geprüfte Ausgaben erzeugen. Einzelfehler getrennt melden und mit den übrigen Kandidaten fortfahren; kein Erfolg für ungeprüfte oder nur teilweise erzeugte Dateien.
- Abnahme: beide Fehlerreferenzen richtig erkannt; Vergleich ohne B-Frames nicht als diese Fehlerklasse eingestuft; `repair-test = true` verarbeitet höchstens die zwei festgelegten Namen und respektiert Zielkonflikte. Serienmodus erst nach erfolgreichen Probe-Reparaturen der beiden Fehlerclips einsetzen; alle verbleibenden Clips analysieren, aber ausschließlich bestätigte Fälle des bekannten Fehlerbilds reparieren.
- Nach ausdrücklicher Freigabe dieser Fassung zuerst die praktische README erstellen und freigeben lassen, danach implementieren. Der Agent führt ausschließlich lokale, ungefährliche Prüfungen aus und verändert weder GitHub noch Produktivsysteme. Die tatsächlichen Probe-Reparaturen und Player-/SMB-Abnahmen sind noch durchzuführen.
