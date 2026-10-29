# Befund: Zeitstempelfehler der FiMu-Proxies

**Stand:** 29. Oktober 2026. **Grundlage:** die drei vollständigen JSON-Protokolle aus Martins Analyse-ZIP; keine Original-MP4s und keine reparierten Videos wurden hier geprüft.

Es wurden 112.755 Videopakete und 112.753 ausgegebene Videobilder ausgewertet. Die Zählungen wurden unabhängig nachgerechnet und die Zuordnung der Bilder zu den Originalpaketen über die Dateiposition geprüft. Der große Markdown-Bericht wurde nicht als alleinige Diagnosegrundlage verwendet.

## 1. Gesicherter Vergleich

| Clip | Profil / Level | Raster | Videopakete | Ausgegebene Bilder | B-Bilder | Doppelt belegte PTS-Werte | PTS-Rückschritte in Decoder-Ausgabereihenfolge |
|---|---|---|---:|---:|---:|---:|---:|
| `58238241.mp4` | Main / 5.1 | 2048 × 1080 | 28.090 | 28.089 | 18.726 | 5.618 | 5.618 |
| `58238527.mp4` | Main / 5.1 | 2048 × 1080 | 52.338 | 52.337 | 34.891 | 10.467 | 10.467 |
| `53287289.mp4` | Constrained Baseline / 4.0 | 1920 × 1080 | 32.327 | 32.327 | 0 | 0 | 0 |

- Alle drei Videos sind als progressive H.264-Videos mit 24 Bildern/s, yuv420p, BT.709 und quadratischen Pixeln protokolliert. Zeitbasis: 1/2400; regulärer Schritt: 100 Ticks = ein Bild.
- Bei beiden Fehlerclips haben sämtliche ausgegebenen B-Bilder im zugehörigen Originalpaket `PTS = DTS`. Jede doppelte PTS-Zuordnung betrifft ein B- und ein P-Bild. Die Zahl der Rückschritte und der aufeinanderfolgenden gleichen PTS in der Decoder-Ausgabe entspricht jeweils der Zahl der doppelt belegten PTS-Werte.
- Die DTS steigen bei allen drei Clips gleichmäßig um 100 Ticks. Die Fehlerclips haben also eine gleichmäßige Decodierzeitachse, aber eine widersprüchliche Präsentationszeitachse.
- Beim sauberen Clip sind alle 32.327 Bild-PTS unterschiedlich und steigen in der Decoder-Ausgabe genau um 100 Ticks. Dort gilt auch bei allen Paketen `PTS = DTS`; ohne Bild-Reordering ist diese Gleichheit kein Fehler.
- B-Frames, ein Main-Profil oder `PTS = DTS` bei B-Bildern allein beweisen keinen Fehler. Entscheidend sind die tatsächlichen Kollisionen und die widersprüchliche Zuordnung zur ausgegebenen Bildreihenfolge.

## 2. Konkrete Fehlstelle

Beide Fehlerclips beginnen in derselben Decoder-Ausgabereihenfolge. Die Soll-Spalte ist das gleichmäßige 24-fps-Raster ab PTS 0, nicht eine bereits getestete Reparatur:

| Bildindex | Typ | Zugehöriger Originalpaketindex | Gespeicherte PTS | Soll-PTS bei 24 fps |
|---:|---|---:|---:|---:|
| 0 | I | 0 | 0 | 0 |
| 1 | B | 3 | 200 | 100 |
| 2 | B | 2 | 100 | 200 |
| 3 | B | 4 | 300 | 300 |
| 4 | P | 1 | 300 | 400 |

Die gespeicherte Folge `0, 200, 100, 300, 300` läuft zurück und ordnet zwei unterschiedlichen Bildern denselben Zeitpunkt zu. Das ist nicht bloß der normale Unterschied zwischen Paket- und Anzeigereihenfolge.

Die beiden Fehlerclips zeigen außerdem identische Schlüsselbildabstände von 15 ausgegebenen Bildern. Die Decoder-Ausgabe enthält Gruppen mit drei B-Bildern. Aus den Protokollen lässt sich eine gemeinsame Fehlerklasse ableiten, aber weder der ursprüngliche Encoder noch dessen vollständige Konfiguration sicher identifizieren; der Encoder-Tag lautet lediglich „H.264“. Direkte SPS-/POC-Header und vollständige MP4-Editlisten sind nicht enthalten.

## 3. Decoder-Schätzwerte nicht als Wahrheit verwenden

Bei `58238241.mp4` weicht `best_effort_timestamp` in 22.469 Bildern von den gespeicherten PTS ab; bei `58238527.mp4` in 41.868 Bildern. Im Vergleich zum gleichmäßigen Raster liegt dieser Schätzwert bei fast allen ausgegebenen Bildern um ein Bild höher. Das Anfangs- und Endverhalten ist ebenfalls auffällig. Beim sauberen Clip stimmt er durchgehend mit den PTS überein.

Damit können Decoder-Schätzwerte die gespeicherten Defekte verdecken, liefern hier aber keinen ausreichenden Abnahmenachweis für die richtige Zeitachse. Diese Protokolle testen auch nicht das Verhalten von VLC, Flow, One oder QuickTime selbst.

## 4. Randfall am Clipende: vor Serienreparatur klären

| Clip | Videopakete | Decoder-Ausgabe | Discard-markierte Pakete | Dauer aus ausgegebenen Bildern bei 24 fps | Protokollierte Videodauer |
|---|---:|---:|---:|---:|---:|
| `58238241.mp4` | 28.090 | 28.089 | 1 | 1170.375000 s | 1170.416667 s |
| `58238527.mp4` | 52.338 | 52.337 | 1 | 2180.708333 s | 2180.750000 s |
| `53287289.mp4` | 32.327 | 32.327 | 0 | 1346.958333 s | 1346.958333 s |

- Bei beiden Fehlerclips ist genau ein Paket mit `_D_`/Discard markiert. Genau dieses Paket fehlt in der ausgegebenen Bildliste; seine PTS liegt am oder hinter dem angegebenen Videoende. Kein Paket ist als korrupt markiert.
- Die FFmpeg-Definition von Discard bedeutet „nach dem Decodieren nicht ausgeben“, nicht „Videodaten sind beschädigt“. Ein Zusammenhang mit den MP4-Editlisten und den falschen PTS ist plausibel, anhand dieser Logs aber noch nicht bewiesen.
- Würde man nur die bisher ausgegebenen Bilder auf ein gleichmäßiges Raster setzen, wäre die Videodauer bei beiden Fehlerclips um ein Bild, rund 41,667 ms, kürzer als angegeben. Die Differenz darf nicht ungeprüft als gewünschter Schnitt, Datenverlust oder Aufforderung zum Duplizieren eines Schlussbildes behandelt werden.
- Nächste lokale Gegenprobe: Decoder-Ausgabe mit normaler Editlistenbehandlung und mit `-ignore_editlist 1` vergleichen. Prüfen, ob das betreffende Bild dann erscheint und ob es zum gewünschten Clip gehört. Editlisten nicht pauschal für alle Dateien und insbesondere nicht ungeprüft für Audio ignorieren.

## 5. Konservatives Prüfmuster für die Serie

1. Alle Clips auf der konfigurierten Quellordnerebene vollständig auf Videopakete und Zeitstempel prüfen. Roh-PTS/DTS als Ganzzahlen mit rationaler Zeitbasis auswerten, nicht gerundete Sekundenwerte.
2. Bei B-Frames oder auffälligen Paketen die komplette Decoder-Ausgabereihenfolge samt Zuordnung zu den Originalpaketen prüfen. Für die bekannte Fehlerklasse: bestätigte konstante Bildrate, regelmäßige DTS, wiederholte PTS-Kollisionen zwischen unterschiedlichen ausgegebenen Bildern und PTS-Rückschritte in der Decoder-Ausgabe; das B-Frame-/Offsetmuster dient als zusätzliche Bestätigung.
3. Drei Ergebnisse unterscheiden: kein Nachweis dieses Fehlers → unverändert lassen; bestätigte bekannte Fehlerklasse → Reparaturkandidat; andere oder unklare Auffälligkeit → gesonderter Bericht, keine automatische Reparatur.
4. Raster, Profil, Dateiname und das bloße Vorhandensein von B-Frames sind keine Fehler-Whitelist. 24 fps und 100 Ticks gelten für diese Referenzen; andere Bildraten, variable Bildraten, mehrere Videostreams und andere Schnitt-/Startzeitmodelle müssen separat beurteilt werden.

## 6. Reparaturkandidat und zwingende Abnahme

- Nach Klärung des Discard-/Clipendenfalls die Bilder durch den H.264-Decoder in dessen Bild-Ausgabereihenfolge bereitstellen; nicht nach den fehlerhaften PTS sortieren.
- Für die bestätigten CFR-Referenzen die Zeitstempel aus der Bildnummer neu erzeugen: `setpts=N/(24*TB)`; bei anderen bestätigten Bildraten den jeweiligen rationalen Wert verwenden und den erforderlichen Start-/Audio-Bezug erhalten.
- Video neu als H.264 ohne B-Frames encodieren (`-bf 0`). Nach dem neuen Raster unerwünschtes Duplizieren oder Verwerfen durch die Ausgabesynchronisation verhindern; `-fps_mode passthrough` ist hierfür eine zu testende Einstellung, kein Ersatz für den Zeitstempelaufbau.
- Ein einfaches Remux, nur `-bf 0`, nur Erzwingen einer Bildrate oder `+genpts` allein wird nicht als ausreichender Fix behandelt. Hier fehlen die PTS nicht, sie sind vorhanden und falsch.
- Pro Clip Auflösung, Seitenverhältnis, Bildrate, Pixelformat und Farbkennzeichnung erhalten. Die Fehlerclips sind 2048 × 1080; das 1920-×-1080-Raster bzw. Level 4.0 des sauberen Clips nicht blind übernehmen.
- Quell-Videobitrate als Zielwert übernehmen: 1.200.179 bzw. 1.200.750 bit/s bei den Fehlerclips und 1.198.099 bit/s beim sauberen Vergleich. „1,5 Mbit Proxy“ ist keine Vorgabe, die Videobitrate pauschal auf 1,5 Mbit/s zu ändern. Neu-Encoding bleibt verlustbehaftet und erzeugt keine exakt identischen Videodaten oder garantierte identische resultierende Bitrate.
- Sämtliche Audiostreams unverändert kopieren und deren Zeitbezug prüfen. Die Fehlerclips haben jeweils eine AAC-Stereospur mit 48 kHz, der saubere Clip zwei. Keine Zusammenlegung, kein Resampling und kein Erzwingen identischer Audio-/Videodauern.
- Metadaten und die tatsächlichen `tmcd`-Timecode-Datenspuren samt Tags erhalten und nachprüfen. Die protokollierten „Unsupported codec …“-Warnungen betreffen diese Datenspuren, nicht allein deshalb einen H.264-Decoderfehler. Ein kopierter Timecode-Tag allein belegt keinen vollständigen Erhalt.
- Zuerst beide Fehler-Samples lokal reparieren und vergleichen: erwartete Bilder samt Anfang/Ende, Reihenfolge, PTS/DTS, Bildrate, Dauer, keine B-Frames, Audio-Payload/Timing, Timecodes und Metadaten. Ergänzend Wiedergabe in den tatsächlich betroffenen Playern prüfen. Diese Tests wurden hier noch nicht durchgeführt.
- Erst nach erfolgreichen Pilotprüfungen die Serie freigeben. Wie vereinbart: `config.ini` neben der EXE, nicht rekursiver Scan, identischer Dateiname, Quellen unverändert, Ausgabe zuerst als `.part` im Zielordner und atomare Veröffentlichung nur nach bestandener Einzelprüfung. Konflikte und ungeklärte Fälle nicht überschreiben oder stillschweigend überspringen.

## 7. Ergebnis

**Die bekannte Fehlerklasse ist anhand der hochgeladenen Protokolle eindeutig unterscheidbar. Ein konservativer Prüfer lässt sich daraus ableiten. Eine sichere Reparaturstrategie ist plausibel, aber noch nicht als fertiger Serien-Fix nachgewiesen: Der Discard-/Clipendenfall, tatsächlicher Bild-/Audioerhalt und Timecode-/Metadatenerhalt müssen an Originalclips geprüft werden.**

## Technische Primärquellen

- FFmpeg API: `AV_PKT_FLAG_DISCARD` — `https://ffmpeg.org/doxygen/trunk/packet_8h.html`
- FFmpeg Formate: `ignore_editlist`, `advanced_editlist`, `genpts` — `https://ffmpeg.org/ffmpeg-formats.html`
- FFmpeg Filter: `setpts` — `https://ffmpeg.org/ffmpeg-filters.html`
- FFmpeg Codecs: `bf` — `https://ffmpeg.org/ffmpeg-codecs.html`
- FFmpeg Handbuch: `fps_mode`, Streamcopy — `https://ffmpeg.org/ffmpeg.html`

Die numerischen Befunde stammen ausschließlich aus `58238241.json`, `58238527.json` und `53287289.json` im hochgeladenen ZIP, nicht aus den Internetquellen.
