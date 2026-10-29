# Pflichtenheft: Analyse und Reparatur der 1,5-Mbit-Proxies

**Status:** Zur Freigabe. Dieses Dokument beschreibt das Sollverhalten; Analyse und Reparatur wurden noch nicht durchgeführt.

## 1. Ziel und Umfang

- Umsetzung mit Python und FFmpeg einschließlich ffprobe: zunächst ein kleines separates Analyse-Script, anschließend Anpassung des One-Off-Transcoders im Repo Transcoder für den Betrieb als Windows-EXE.
- Der One-Off-Transcoder darf vollständig verändert werden; Abwärtskompatibilität ist nicht erforderlich. Gegenstand ist ausschließlich der beschriebene B-Frame-Zeitstempel-/Reihenfolgefehler, nicht die bisherigen Bitraten-, Bild- oder Audiokorrekturen.

## 2. Vorab-Analyse der Referenzclips

| Quelldatei | Von Martin vorgegebene Referenzeinstufung |
|---|---|
| `58238241.mp4` | Fehler vorhanden |
| `58238527.mp4` | Fehler vorhanden |
| `53287289.mp4` | Fehler nicht vorhanden; keine B-Frames |

- Das Analyse-Script untersucht ausschließlich diese drei Originalclips, vollständig über ihre Laufzeit und ohne sie zu verändern. Es erzeugt keine reparierten Videos.
- Erfassen: B-Frame-Vorkommen und I/P/B-Sequenzen; Decodier- und Präsentationsreihenfolge; originale Paket-PTS/DTS, Zeitbasis, Zeitabstände, Dauern, Paket-/Bildpositionen sowie Reorderinformationen und `PTS − DTS`. Doppelte, fehlende oder widersprüchliche Zeitstempel und periodische Muster mit konkreten Fundstellen dokumentieren.
- Gespeicherte Paketzeitstempel und decoderseitig rekonstruierte bzw. geschätzte Bildzeitstempel getrennt ausweisen. Reparierte Decoderzeitstempel dürfen den ursprünglichen Fehler nicht verdecken. B-Frames allein oder rückwärts springende Paket-PTS allein sind kein ausreichendes Fehlerkriterium.
- Technisches Inventar protokollieren: Container und sämtliche Streams; Codec, Profil, verfügbare Encoderparameter, Bildformat, Pixelformat, Bildraten, Zeitbasen, Bitraten, Laufzeiten und Bildanzahl; Audioeigenschaften, Timecodes, Kapitel und Metadaten. Nicht enthaltene oder nicht eindeutig rekonstruierbare Exportparameter als unbekannt kennzeichnen.
- Dauerhafte Ausgaben: ein lesbarer Markdown-Vergleich mit technischen Befunden, relevanten Bild-/Paketsequenzen und begründeter Einstufung sowie vollständige JSON-Rohdaten je Clip. Dateizuordnung, Analysezeitpunkt, Werkzeugversionen, verwendete Aufrufe und Diagnosemeldungen festhalten; ältere Berichte nicht überschreiben.

## 3. Prüfmuster und funktionierende Ausgabekonfiguration

- Aus dem Vergleich ein technisches Prüfmuster ableiten, das beide bekannten Fehlerclips erkennt und den fehlerfreien Clip nicht beanstandet. Die Namen dienen als Referenztests, nicht als hartcodierte Fehlerliste für die spätere Serie.
- Beobachtete Unterschiede, nachgewiesene Fehlermerkmale und verbleibende Unsicherheiten getrennt dokumentieren. Eine funktionierende Datei allein belegt nicht die vollständige ursprüngliche Exportkonfiguration oder die Ursache jedes anderen Fehlers.
- Die fehlerfreie Datei ohne B-Frames dient als funktionierende Referenz. Der besprochene Verzicht auf B-Frames wird bei der Festlegung des Ausgabeprofils berücksichtigt; konkrete Encoderwerte, Sollbildrate und Zeitstempelaufbau werden erst anhand der Analyse festgelegt, nicht vorab geraten.
- Vor der Serienverarbeitung das Prüfmuster und Ausgabeprofil anhand des Analyseberichts bestätigen und die Reparatur beider Fehler-Samples verifizieren. Analysebefunde, Prüfmuster und konkrete Reparaturkonfiguration dauerhaft dokumentieren.

## 4. Konfiguration und Serienverarbeitung

- Quell- und Zielpfad werden aus einer `config.ini` direkt neben der EXE gelesen, unabhängig vom aktuellen Arbeitsverzeichnis. Pfadänderungen erfordern keinen neuen EXE-Build. Das Analyse-Script verwendet die Quellpfad-Angabe derselben Konfiguration für seine drei Referenzclips.

```ini
[paths]
source_path = \\10.0.77.102\FiMu 1.5 Mbit Proxy
target_path = \\10.0.77.102\FiMu 1.5 Mbit Proxy\fixed
```

- Der One-Off-Transcoder prüft Dateien unmittelbar im Quellordner, **nicht rekursiv**. Unterordner einschließlich `fixed` werden nicht gescannt; vorhandene `.part`-Dateien sind keine Eingabeclips.
- Das bestätigte Prüfmuster auf alle gefundenen Clips anwenden. Nur bestätigte Fehlerfälle neu encodieren und im Zielordner ablegen. Unauffällige Clips unverändert lassen und nicht ausgeben; unklare oder nicht auswertbare Dateien gesondert melden.
- Jeden Clip mit Diagnose, Entscheidung, Reparaturkonfiguration und Ergebnis protokollieren. Einzelfehler dokumentieren und die übrigen Clips weiter prüfen. Die Quelldateien bleiben unverändert.

## 5. Neukodierung und Erhaltung der Quelldaten

- Fehlerfälle mit dem aus der Analyse abgeleiteten und verifizierten Ausgabeprofil neu encodieren. Die korrekte Bildreihenfolge und Zeitachse herstellen; defekte Zeitstempel nicht ungeprüft übernehmen. Die ursprüngliche verlustfreie Reparaturvorgabe wird durch die ausdrücklich gewünschte Videoneukodierung ersetzt.
- Quellbitrate als Zielbitrate verwenden, kein pauschales anderes Bitratenpreset. Die tatsächlich resultierende Bitrate protokollieren; Neu-Encoding garantiert weder identische komprimierte Videodaten noch eine exakt identische resultierende Bitrate und kann zusätzliche Qualitätsminderung verursachen.
- Bildinhalte, Bildanzahl, Auflösung, Seitenverhältnis und Sollbildrate erhalten. Keine zusätzlichen Skalierungs-, Beschnitt-, Deinterlacing- oder Bildkorrekturen und kein stillschweigendes Duplizieren oder Verwerfen von Bildern.
- Sämtliche Audio-, Zusatz- und Datenstreams, Metadaten auf Container-/Streamebene, Timecodes, Kapitel und weiteren Quellinformationen erhalten. Audio unverändert kopieren, nicht neu encodieren, zusammenmischen, umordnen oder entfernen. Abweichungen außerhalb der ausdrücklich vorgesehenen Videoneukodierung/Zeitstempelreparatur nicht stillschweigend zulassen; nicht sicher erhaltbare Informationen verhindern die Veröffentlichung.

## 6. Sichere Veröffentlichung

- Der endgültige Zieldateiname entspricht **1:1 dem Quelldateinamen einschließlich Erweiterung**.
- Verarbeitung zunächst ausschließlich in eine `.part`-Datei im Zielordner. Nach Abschluss die Ausgabe prüfen und alle Dateizugriffe schließen; erst anschließend atomar innerhalb dieses Zielordners auf den endgültigen Namen umbenennen. Das geforderte Abschlussverhalten ist auf dem verwendeten SMB-Ziel zu prüfen.
- Vorhandene Ziel- oder `.part`-Dateien nicht überschreiben; Konflikte melden. Bei Reparatur- oder Prüfungsfehlern keine endgültige Datei veröffentlichen; etwaige unvollständige `.part`-Dateien als solche melden.

## 7. Abnahmekriterien und Grenzen

- Die Analyse beschreibt alle drei Referenzen nachvollziehbar und vollständig, unterscheidet die vorgegebenen zwei Fehlerfälle vom fehlerfreien Fall und hinterlässt wiederverwendbare Berichte und Rohdaten.
- Beide reparierten Fehler-Samples bestehen die erneute Prüfung auf Bildreihenfolge und Zeitstempel; Bildanzahl, Bildinhalte, Laufzeit, A/V-Synchronität, Metadaten, Timecodes und Stream-Erhaltung werden gegen die Quelle geprüft. Bei einem Ausgabeprofil ohne B-Frames ist deren Abwesenheit zusätzlich nachzuweisen.
- Die Serie scannt nur die konfigurierte Ordnerebene, produziert ausschließlich geprüfte Reparaturen unter identischen Dateinamen und verändert keine Quellen oder vorhandenen Zieldateien.
- Die drei Originalclips müssen für den tatsächlichen Test vorliegen. Nicht rekonstruierbare Exportparameter, unsichere Sollbildrate oder nicht erfüllbarer Informationserhalt dürfen nicht durch Annahmen ersetzt werden. Referenztests ersetzen keine erneute Prüfung jeder erzeugten Ausgabe.
- Dieses Pflichtenheft ist noch keine Implementierungsfreigabe: Nach seiner ausdrücklichen Freigabe folgt zunächst die README zur praktischen Nutzung und deren Freigabe. Erst danach wird implementiert. Prüfungen durch den Agenten erfolgen ausschließlich lokal und ungefährlich; keine Tests oder Veränderungen an Produktivsystemen.
