# Implementierungsplan: One-Off-Proxyreparatur

## Grundlage und Repo-Bestand

Analysiert wurden das vollständige Pflichtenheft sowie `main` des Repos `mrtnRitter/Transcoder` (aufgelöster Stand: `aaea03ab967be8b05e3a9a09a658d263e96a6f33`). Dies ist eine Analyse und ein Schnittstellenplan, keine Freigabe zur Implementierung.

- `pyproject.toml` verlangt Python `>=3.11`; PyInstaller ist ein optionales Build-Extra. `oneoff/build.py` baut die bestehende `oneoff/one-off_transcode.py` als Windows-Onefile-EXE und erwartet `ffmpeg`/`ffprobe` unter `res/`. Außerdem liest es bisher `qc_list`/`done_lists` aus dem Skript und verlangt diese Dateien beim Build; diese Altlast muss beim Umbau entfallen, statt die Listen weiter in die neue EXE zu übernehmen.
- Das bestehende Transcode-Programm hat fest im Skript definierte Pfade, QC-Listen, Regeln und Presets; es liest keine externe `config.ini`. Es protokolliert über `toolbox.tb_write_log` in eine Logdatei neben Skript/EXE. Die Konsolenfunktion `say()` schreibt zusätzlich ins Log, die Fortschrittszeile ist nur Konsole.
- `transcoder._media` löst Werkzeuge zuerst aus dem gebündelten `res/`, dann über `PATH` auf und bietet `run_text()`, `read_chunks()` sowie einen ffprobe-Aufruf für Format, Streams und Kapitel. Das vorhandene Skript ruft FFmpeg zusätzlich mit `subprocess.Popen` samt Fortschrittsausgabe auf.
- Für den Referenzlauf existiert bereits `oneoff/analysis/probe.py`: `collect_clip()` ruft ffprobe für Format, Streams, Kapitel, Pakete und Decoder-Bilder auf, sammelt Werkzeugversionen, Argumente und Diagnosen und liefert JSON-fähige Rohdaten. `oneoff/analysis/evaluate.py` wertet Zeitstempel und Paket/Bild-Zuordnung aus; `report.py` und `progress.py` schreiben Referenzberichte bzw. Fortschritt.
- `evaluate_clip()` klassifiziert allgemeine Anomalien und liefert Befunde, aber nicht die für dieses Pflichtenheft erforderliche eigenständige, streng bestätigte Reparaturentscheidung. Insbesondere sind B-Frames oder rückwärts laufende Paket-PTS allein dort kein ausreichender Beleg. Den neuen Entscheidungsmaßstab deshalb separat in `diagnosis.py` implementieren; `collect_clip()` und dessen Rohdaten dagegen wiederverwenden und nur bei fehlenden Prüffeldern gezielt erweitern.
- Vorhanden sind `oneoff/tests/test_probe.py`, `test_evaluate_report.py`, `test_progress.py`, `test_tool_resolution.py` sowie Repo-Tests unter `tests/`. Die One-Off-Tests decken vor allem synthetische Clips, Rohdatensammlung, Diagnosebefunde, Berichte, Fortschritt und Werkzeugauflösung ab. Die drei realen Referenzclips und SMB-/Player-Abnahmen sind keine hier nachgewiesenen lokalen Testressourcen; die reale Referenzabnahme muss daher separat mit den Originalclips erfolgen.

## Modulaufteilung und eindeutige Spec-Zuordnung

Sechs Runtime-Module entsprechen den Abschnitten 2–7. `contracts.py` enthält ausschließlich gemeinsam genutzte Typen und Protokolle, keine eigene Spec-Funktionalität. Die vorhandene `one-off_transcode.py` wird zum dünnen CLI-/Orchestrierungs-Einstiegspunkt umgebaut; alte QC-/Bitraten-/Bild-/Audioregeln werden nicht als Rückwärtskompatibilität weitergeführt. Jede Zeile der Spec-Zuordnung hat genau einen fachlichen Eigentümer; andere Module konsumieren dessen Ergebnis über die unten stehenden Verträge.

| Modul / Abschnitt | Verantwortung und zugeordnete Anforderungen |
|---|---|
| `oneoff/config.py` — Abschnitt 2 | `config.ini` neben EXE/Skript unabhängig vom Arbeitsverzeichnis laden; Schlüssel und Werte strikt validieren; Quell-, Ziel- und Reportpfad sowie Quell-/Zielgleichheit vor Verarbeitung prüfen; `repair-test` als einzigen Betriebsmodus-Schalter ohne Ersatzpfad oder stillen Moduswechsel auswerten. Liefert eine unveränderliche Modus-Policy: Testmodus liefert ausschließlich die Allowlist `58238241.mp4` und `58238527.mp4`; der Vergleichsclip `53287289.mp4` ist kein Reparaturkandidat. Serienmodus erlaubt alle Clips und nur bestätigte Fälle. |
| `oneoff/inventory.py` — Abschnitt 3 | Quell- und Zielordner nichtrekursiv inventarisieren; `.part` ignorieren; fertige Ziele per vollständigem Dateinamen inkl. Erweiterung und Windows-Groß-/Kleinschreibung vor Analyse ausschließen; Testquellen-Abwesenheit und übersprungene Ziele erfassen; verbleibende Kandidaten vollständig mit `oneoff.analysis.probe.collect_clip()` analysieren. Rohdaten und gespeicherte Zeitstempel bleiben von Decoder-Schätzwerten unterscheidbar. Dieses Modul entscheidet nur Auswahl/Erfassung, nicht die Reparaturklasse. |
| `oneoff/diagnosis.py` — Abschnitt 4 | Aus vollständigen Rohdaten die bekannte Fehlerklasse explizit und konservativ nachweisen: H.264, B-Bilder, bestätigte konstante Bildrate, regelmäßige DTS, eindeutige Bild-/Paketzuordnung, wiederholte PTS verschiedener B-/P-Bilder, Kollision/Rückschritt in tatsächlicher Decoder-Ausgabereihenfolge und das B-Bild-Muster `PTS = DTS`. Ganzzahlige Ticks und rationale Zeitbasen verwenden. Liefert getrennt `confirmed`, `clean`, `unclear` oder `unsupported`; andere/fehlende/widersprüchliche Evidenz wird nie automatisch repariert. Dateinamen sind niemals Diagnosebeleg; die drei Referenzen prüfen den Inhaltsalgorithmus. |
| `oneoff/repair.py` — Abschnitt 5 | Nur einen bestätigten Fall decodieren und aus regulärer Decoder-Ausgabe eine gleichmäßige Präsentationszeitachse ableiten; H.264 ohne B-Frames mit Quellbitrate und Quellbildformat/-rate/Farbe/Auflösung encodieren. Alle Audiostreams unverändert mit Zeitbezug sowie Metadaten, Kapitel und tatsächliche Timecode-/Datenspuren kopieren. Discard-Markierungen nicht vor dem Decoder entfernen; keine zusätzliche Synchronisations-Duplizierung/-Verwerfung. Schreibt ausschließlich in den vom Veröffentlichungsmodul gelieferten `.part`-Pfad und verändert die Quelle nicht. |
| `oneoff/publication.py` — Abschnitt 6 | Decoder-Bildanzahl und Ausgabe gegen Quelle/Diagnose prüfen: Reihenfolge, Zeitabstände, Kollisionen/Rückschritte, B-Frames, Videoformat/-dauer, A/V-Bezug, Audio-Payload und Eigenschaften, Metadaten, Kapitel und Timecode-/Datenspuren. Den einzigen erlaubten Discard-Endbild-Unterschied explizit kennzeichnen. `.part` im Zielordner ohne Überschreiben vorhandener Teildateien erzeugen; nach vollständiger Prüfung und geschlossenen Handles ausschließlich atomar und ohne Überschreiben auf den exakten Quellnamen veröffentlichen. Konflikt/Fehler hinterlässt keine finale Datei, löscht keine Teildatei und meldet Ursache; Quelle bleibt unangetastet. |
| `oneoff/run_report.py` — Abschnitt 7 | Für jeden Start einen neuen `reports`-Laufordner neben der EXE anlegen; Config, Werkzeugversionen, Aufrufe, Fortschritt und Meldungen sichern; pro Clip Roh-JSON, technische Befunde, Entscheidung und Ausgabekontrolle persistieren; lesbaren Markdown-Bericht und getrennte Ergebnisübersicht schreiben. Dieses Modul persistiert außerdem getrennte Erfassungs-, Diagnose-, Klassen- und Reparaturentscheidungs-Ereignisse (Abschnitt 3). Es besitzt die per-Clip-Fehlergrenze: ein Kandidatenfehler wird protokolliert und die Verarbeitung wird mit dem nächsten fortgesetzt. Abnahme-Status darf nur geprüfte Veröffentlichungen als Erfolg zählen; README-Freigabe und reale Probe-/Player-/SMB-Abnahmen bleiben explizite Freigabegates, keine automatische Erfolgsmeldung. |

## Gemeinsame Verträge (`oneoff/contracts.py`)

Nur Standardbibliothekstypen (`pathlib.Path`, `fractions.Fraction`, `typing.Any`, `Literal`, `Callable`) verwenden. Die Typen sind unveränderlich, bis auf die enthaltenen JSON-Dictionaries. `contracts.py` darf keine der sechs Fachmodule importieren, damit keine Importzyklen entstehen.

```python
@dataclass(frozen=True)
class AppConfig:
    exe_dir: Path
    source_dir: Path
    target_dir: Path
    reports_dir: Path
    repair_test: bool

@dataclass(frozen=True)
class RunPolicy:
    mode: Literal["test", "series"]
    candidate_names: frozenset[str] | None  # fixed two names in test mode; None in series mode
    repair_confirmed_only: bool

@dataclass(frozen=True)
class Candidate:
    source_path: Path
    target_path: Path

@dataclass(frozen=True)
class SkippedClip:
    source_path: Path
    reason: str

@dataclass(frozen=True)
class Selection:
    candidates: tuple[Candidate, ...]
    skipped: tuple[SkippedClip, ...]
    missing_test_sources: tuple[Path, ...]

@dataclass(frozen=True)
class ClipAnalysis:
    candidate: Candidate
    raw: dict[str, Any]  # JSON-compatible collect_clip() result

@dataclass(frozen=True)
class Diagnosis:
    kind: Literal["confirmed", "clean", "unclear", "unsupported"]
    reason: str
    evidence: dict[str, Any]
    frame_rate: Fraction | None
    decoder_frame_count: int | None
    accepted_terminal_discard: bool

@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stderr: str

@dataclass(frozen=True)
class RepairArtifact:
    part_path: Path
    expected_video_frames: int
    command_result: CommandResult

@dataclass(frozen=True)
class Verification:
    passed: bool
    differences: tuple[str, ...]
    accepted_terminal_discard: bool
    output_raw: dict[str, Any] | None

@dataclass(frozen=True)
class PublicationResult:
    published: bool
    final_path: Path | None
    part_path: Path
    reason: str | None

@dataclass(frozen=True)
class RunReport:
    run_dir: Path
    log_path: Path
    markdown_path: Path

@dataclass(frozen=True)
class ClipOutcome:
    candidate: Candidate
    clip: ClipAnalysis | None
    diagnosis: Diagnosis | None
    artifact: RepairArtifact | None
    verification: Verification | None
    status: Literal["clean", "unclear", "unsupported", "analysis_error", "repair_error", "verification_error", "published"]
    reason: str | None

@dataclass(frozen=True)
class RunSummary:
    counts: dict[str, int]

@dataclass(frozen=True)
class ReportEvent:
    stage: str
    status: str
    source_path: Path | None
    message: str
    details: dict[str, Any]

@dataclass(frozen=True)
class AudioFingerprint:
    stream_index: int
    properties: dict[str, Any]
    packet_hashes: tuple[str, ...]
    packet_timestamps: tuple[tuple[int | None, int | None, int | None], ...]  # PTS, DTS, duration; time base in properties
```

Shared callable aliases: `ClipCollector = Callable[[Path], dict[str, Any]]`; `FfmpegRunner = Callable[[tuple[str, ...], Callable[[dict[str, str]], None] | None], CommandResult]`; `AudioFingerprinter = Callable[[Path], tuple[AudioFingerprint, ...]]`; `NoReplaceRename = Callable[[Path, Path], None]`. Production defaults live in their owning modules; tests inject fakes. FFmpeg invocation and no-replace rename failures become typed/clear module errors, never success-shaped empty results.

### Public interfaces

```python
# config.py
def resolve_exe_dir(script_file: Path, executable: Path | None = None) -> Path: ...
def load_config(exe_dir: Path) -> AppConfig: ...  # raises ConfigError before processing
def build_policy(config: AppConfig) -> RunPolicy: ...

# inventory.py
def select_candidates(config: AppConfig, policy: RunPolicy) -> Selection: ...
def collect_candidate(candidate: Candidate, collector: ClipCollector = collect_clip) -> ClipAnalysis: ...

# diagnosis.py
def classify(clip: ClipAnalysis) -> Diagnosis: ...  # pure; no subprocess or filesystem writes

# repair.py
def encode_repair(
    clip: ClipAnalysis, diagnosis: Diagnosis, part_path: Path,
    runner: FfmpegRunner = run_ffmpeg,
) -> RepairArtifact: ...

# publication.py
def new_part_path(target_path: Path) -> Path: ...
def verify_output(
    clip: ClipAnalysis, diagnosis: Diagnosis, artifact: RepairArtifact,
    collector: ClipCollector = collect_clip,
    audio_fingerprinter: AudioFingerprinter = fingerprint_audio,
) -> Verification: ...
def publish_verified(
    artifact: RepairArtifact, target_path: Path, verification: Verification,
    rename_no_replace: NoReplaceRename = atomic_rename_no_replace,
) -> PublicationResult: ...

# run_report.py
def start_run(config: AppConfig, tool_versions: dict[str, str], argv: tuple[str, ...]) -> RunReport: ...
def record_event(report: RunReport, event: ReportEvent) -> None: ...
def record_clip(report: RunReport, outcome: ClipOutcome) -> None: ...
def process_candidates(
    report: RunReport, candidates: tuple[Candidate, ...],
    process_one: Callable[[Candidate], ClipOutcome],
) -> RunSummary: ...  # catches/logs each clip error and continues
def finish_run(report: RunReport, summary: RunSummary) -> Path: ...
```

The classifier is pure and independently fixture-testable. `inventory` may accept a fake `ClipCollector`; `repair` accepts a fake process runner; `publication` accepts fake probe/audio-hash/rename functions. This keeps unit tests local and prevents tests from touching real media, SMB shares, or production paths.

## Abhängigkeiten und Parallelisierung

```text
contracts.py
  ├── config.py ──> inventory.py ──> diagnosis.py ──> repair.py ──> publication.py
  ├── run_report.py (consumes records from every stage; can be built in parallel)
  └── one-off_transcode.py (integrator; final wiring after module contracts settle)
```

`contracts.py` und unveränderliche Signaturen zuerst festlegen. Danach können sechs Feature-Worker parallel arbeiten: je ein Worker für `config.py`, `inventory.py`, `diagnosis.py`, `repair.py`, `publication.py` und `run_report.py`, jeweils mit exklusivem Besitz an genau einer neuen Moduldatei und der zugehörigen neuen Testdatei (`oneoff/tests/test_<module>.py`). Der Integrator besitzt ausschließlich `contracts.py`, `oneoff/one-off_transcode.py`, `oneoff/tests/test_workflow.py` und nötige Build-Anpassungen. Bestehende `oneoff/analysis/*`- und bestehende Tests bleiben für alle Worker schreibgeschützt.

Im Testmodus enthält `RunPolicy.candidate_names` genau die zwei Reparaturclips; `inventory.py` meldet jedes dort fehlende Quelldatei-Element und inventarisiert den Vergleichsclip nicht als Kandidat. Serienmodus liefert `candidate_names=None`. Beim Build müssen die importierten neuen `oneoff`-Module und Werkzeuge verfügbar sein; `config.ini` bleibt extern und darf nicht in die EXE eingebettet werden. Der Diagnose-Worker ergänzt `test_diagnosis.py` um inhaltsbasierte Prüfungen für beide Fehlerreferenzen (je `confirmed`) und die saubere Referenz (nicht `confirmed`); ein reiner Dateinamentest reicht nicht. Die Testmodus-Auswahl und Zielkonflikte werden separat in `test_inventory.py` abgedeckt. Die Modulimplementierung kann nach Festlegung der Verträge parallel starten; die Laufzeitintegration folgt dieser Reihenfolge: Config-Preflight → Auswahl/Erfassung → Diagnose → bestätigte Reparatur → Ausgabeprüfung/Veröffentlichung → Laufabschluss. `run_report.py` kann unabhängig implementiert werden, wird aber vom Einstiegspunkt vor der ersten Clipverarbeitung gestartet und an jeder Stufe aufgerufen. Der Build/EXE-Test kommt nach Integration; dabei muss `config.ini` extern neben der EXE bleiben, während die importierten `oneoff`-Module sowie die gebündelten Werkzeuge im PyInstaller-Artefakt verfügbar sind.

## Offene Fragen an Martin

Keine Spec-Frage blockiert den Plan. Für nicht näher geregelte Grenzfälle gilt bewusst fail-closed: unbekannte Eingabedateien werden als Analysefehler protokolliert statt still gefiltert; nicht sicher bestätigte CFR-/Paket-/Bildbelege, nicht sicher kopierbare Streams/Metadaten oder nicht prüfbare Audio-Payload führen zu `unclear`/fehlgeschlagener Prüfung und verhindern Reparatur bzw. Veröffentlichung. In Serienmodus ist jede unmittelbare reguläre Datei zunächst Kandidat; `.part`-Dateien und Unterordner bleiben ausgeschlossen. Die praktische Bestätigung der beiden Probe-Reparaturen sowie Windows-/SMB-Atomizität und Player-Abnahme sind laut Spec noch ausstehende Freigabegates. Vor Implementierung sind zuerst die ausdrückliche Freigabe dieser Pflichtenheftfassung und danach die Freigabe der praktischen README erforderlich.
