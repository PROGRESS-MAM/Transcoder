# Pflichtenheft – Transcoder-Verbesserungen

Stand: 4. November 2026 · Status: Entwurf, nicht zur Implementierung freigegeben.

## Ziel und Umfang
- Fehlerkategorien der Transcoder-Auswertung analysieren und daraus prüfbare Änderungen ableiten; zunächst Kategorie 3.
- Eingaben: `auswertung/auswertung_detailberichte.txt` (hochgeladener Stand, 24. Oktober bis 3. November 2026) und bestehender Transcoder-Code auf `main`.
- Ergebnis: dokumentierte Befunde und abgestimmte Änderungen an Analyse, Marathon-Verarbeitung und betroffenen Master-/Proxy-Ausgaben. Noch keine Codeänderungen.

## Kategorie 3: „Analyse audio: Source hat keinen Ton“
- Befund: 277 Einträge mit unterschiedlichen Job-Kennungen, aber nur 83 unterschiedliche Clip-IDs; keine 277 unterschiedlichen Clips.

| Herkunft laut Berichtspfad | Clipbezeichnung | Einträge | Unterschiedliche Clips |
|---|---|---:|---:|
| LYNXarchive | `LA…` | 58 | 18 |
| DEFA Videoarchive B-Roll | `DEFA…` | 184 | 56 |
| DEFA Der Augenzeuge | `DEFA…` | 35 | 9 |

- Die Kategorie ist nicht auf LYNX oder `LA…` beschränkt. Gegenbeispiel: `DEFA18458 Der Augenzeuge 1961/40 (Restmaterial) - Pioniere, Blumen für Grenzsoldaten` (Berichtszeilen 2681–2684).
- Grenzen: Der Bericht belegt Workflow-Pfade und Clipbezeichnungen, nicht die tatsächlichen Quelldateinamen oder eine separat geprüfte MAM-Sammlungszuordnung. Originalmedien wurden nicht geprüft.
- Technischer Befund: `transcoder/_analysis.py`, Zeilen 329–332, erzeugt die Meldung bei null erkannten Audiokanälen. Vorhandene, aber stille Kanäle sind ein anderer Zustand. `marathon/transcode_marathon.py`, Zeilen 297–298, weist Quellen ohne erkannte Audiokanäle bereits vor der Audioanalyse zur manuellen Prüfung zurück.

## Festgehaltene fachliche Annahme
- Für Clips ist fehlender Ton plausibel, wenn **beide Bedingungen** erfüllt sind: Kollektion exakt `LYNXarchive` **und** Clipname beginnt mit `LA`.
- Dies ist eine fachliche Plausibilitätsannahme, kein Nachweis, dass alle so bezeichneten Clips tatsächlich tonlos sind. Sie bezieht sich auf den Clipnamen, nicht den Dateinamen.
- Daraus folgt noch keine Freigabe zur automatischen Transcodierung oder zum Überspringen von Fehlerprüfungen. Die tatsächliche Kollektion muss vor Anwendung einer entsprechenden Regel geprüft werden; der Auswertungsbericht enthält nur Workflow-Pfade.

## Festgehalten: selektive Wiederfreigabe für den Transcode
- Ausgewählte Clips sollen erneut für den Transcode freigegeben werden; keine pauschale Wiederfreigabe aller Clips einer Kategorie oder Kollektion.
- Die Wiederfreigabe erfolgt an Marathon vorbei über das vorhandene Seitenscript. Martin schaltet Marathon dafür ab.
- Das Seitenscript schreibt die Marathon-JSON entsprechend um, damit die ausgewählten Clips wieder für den Transcode freigegeben sind.
- Konkrete Clipauswahl, JSON-Felder und Zielstatus sind noch nicht festgelegt. Diese Festlegung betrifft die Wiederfreigabe, nicht die noch offene Behandlung tonloser Quellen im Transcoder.

## Daraus abgeleitete Änderungsvorschläge – noch nicht freigegeben
- Die Meldung in Kern und Marathon eindeutig als „Source hat keine erkannten Audiokanäle“ formulieren; fehlende Kanäle, vorhandene stille Kanäle und Analyse-/Decodierfehler nicht gleichsetzen.
- Eine mögliche Zulassung tonloser Quellen nicht allein aus `LA` ableiten: Die festgehaltene Plausibilitätsannahme verlangt zusätzlich die Kollektion `LYNXarchive`. Ob daraus eine automatische Zulassung folgt und welche Audioausgabe Master/Proxy erhalten sollen, vor einer Verhaltensänderung festlegen.
- Nach Festlegung der Regel nur die betroffenen Analyse-, Audio-Fix-, Render- und Nachprüfungswege anpassen; andere Fehlerprüfungen unverändert lassen.

## Kategorien „Ton unsicher“ (4, 6, 7, 10, 12, 14, 17, 26, 101)
- Hörprobe (Martin, 2 Stichproben): jeweils 2 Kanäle deutlich hörbar, 2 Kanäle stumm. Die Kanalerkennung muss verbessert werden.
- Befund: 525 Jobs mit „Proxy fix:audio: Ton unsicher“; in 493 haben alle genannten Kanäle denselben trust. 23 Clips haben zwischen Läufen unterschiedliche trust-Werte, 22 davon im Verhältnis von etwa 2:1 zur Zahl der Messfenster. Der trust hängt damit von der Stichprobenmenge ab, nicht vom Ton.
- Ursache: „stumm“ gilt erst als sicher, wenn statistisch belegt ist, dass der Tonanteil unter 1 % liegt. Ohne ein einziges Fenster mit Ton braucht das mindestens 224 Messfenster für 0.90; mit den aktuellen Marathon-Einstellungen (10 Fenster je Minute) erst ab etwa 23 Minuten Laufzeit. Kürzere Clips mit stummen Kanälen landen deshalb zwangsläufig in der manuellen Prüfung.

### Änderung Kanalerkennung – Ansatz festgelegt, Details offen, nicht freigegeben
- Festgelegt: Je Messfenster den Pegel aller Kanäle messen und die Abstände zwischen den Kanälen über viele Zeitpunkte vergleichen. Ein hörbarer Kanal liegt auch in leisen Passagen durchweg über einem stummen; der Abstand bleibt über die Laufzeit groß.
- Heute: Jeder Kanal wird einzeln gegen −60 dB geprüft; je 1-s-Fenster bleibt nur „Ton ja/nein“, der Pegel selbst wird verworfen. Kanäle werden nur für die Dublettenprüfung verglichen, nicht für hörbar/stumm.
- Ergänzungen für sichere Umsetzung: Pegel in dB statt linearer Anteile; Messpunkte ohne Ton auf allen Kanälen nicht werten; „stumm“ nur bei großem Abstand zum lautesten Kanal und zusätzlich unter einer absoluten Grenze.
- Entscheidung je Kanal per Mehrheit über die Messpunkte; trust aus der Übereinstimmung der Messpunkte statt aus dem Nachweis „Tonanteil < 1 %“.
- Grenzwerte (Abstand in dB, absolute Grenze) noch festzulegen; Risiko: leisere, aber echte Spuren (z. B. IT-/M&E-Ton) dürfen nicht als stumm gelten.

## Überprüfbare Erfolgskriterien
- Die Auswertung reproduziert 277 Einträge und 83 unterschiedliche Clips mit der obigen Verteilung.
- Lokale Tests unterscheiden Quellen ohne erkannte Audiokanäle, Quellen mit ausschließlich stillen Kanälen und Audio-Analysefehler; kein Zustand wird unbeabsichtigt als erfolgreicher Audiobefund gewertet.
- Zulässige und unzulässige tonlose Quellen werden nach der ausdrücklich vereinbarten Regel verarbeitet; gegebenenfalls erzeugte Master/Proxy erfüllen das vereinbarte Audioformat.

## Offene Festlegung und Freigabe
- Noch offen: Sollen Quellen ohne erkannte Audiokanäle bei erfüllter LYNX-Plausibilitätsannahme regulär transcodiert oder weiterhin manuell geprüft werden? Die Plausibilitätsannahme selbst ist festgehalten, aber noch keine Verarbeitungsregel.
- Bis zur Entscheidung keine automatische Ausnahme. Nach Klärung: Pflichtenheft ausdrücklich freigeben, anschließend README prüfen und freigeben; erst danach implementieren.
