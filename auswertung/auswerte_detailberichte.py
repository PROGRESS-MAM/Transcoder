#!/usr/bin/env python3
"""Group failed jobs of the Transcode detail reports by reason pattern."""

# --------- IMPORTS ---------
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


# --------- CONFIG ---------
app_name = "Auswertung Detailberichte"
app_version = "2.0.0"

marathon_root = Path(r"C:\Users\Martin\OneDrive - LOOKS Film & TV Produktionen GmbH\Desktop\Code\Marathon")
archive_dir = marathon_root / "reports" / "archive"
state_path = marathon_root / "state" / "marathon.json"
output_path = Path(__file__).resolve().parent / "auswertung_detailberichte.txt"

# Number of calendar days to include; None processes every matching report.
TAGE = None


# --------- STATIC ---------
report_prefix = "details_transcode_"
missing_folder = "nicht in marathon.json gefunden"
manual_prefix = re.compile(r"^\s*manuelle prüfung:\s*", re.I)
# Marathon adds this row to every rejected job because it looks for an AQC test report; meaningless for Transcode.
noise_rows = {"prüfbericht fehlt"}
row_labels = {"job", "zeit", "versuch", "ergebnis", "fehler", "ist", "info", "ablage"}

block_start = re.compile(r"^#\s*\d+\s+([A-ZÄÖÜ]+)\b\s*(.*)$")
row_line = re.compile(r"^\s{2,}(\S+)\s+(.*?)\s*$")
date_in_name = re.compile(r"(\d{4}-\d{2}-\d{2})_")
period_line = re.compile(r"^Zeitraum\s+(.+?)\s+[–—-]\s+(.+?)\s*$", re.I)
date_value = re.compile(r"\d{4}-\d{2}-\d{2}")

# Order matters: specific patterns first, plain numbers last.
placeholders = (
    (re.compile(r"\d{8}T\d+__\d+__\w+"), "<job>"),
    # Two separators at least, so that "Kanal 1/2" stays readable.
    (re.compile(r"[A-Za-z]:\\[^\s;]+|(?:[A-Za-z]:)?[\\/]*(?:[\w.-]+[\\/]){2,}[\w.-]*"), "<pfad>"),
    (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{1,4}\b"), "<datei>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[:;.,]\d+)?\b"), "<tc>"),
    (re.compile(r"\d+(?:[.,]\d+)?"), "#"),
)
parentheses = re.compile(r"\s*\([^()]*\)")
repeated_items = re.compile(r"(?P<item>[^\s,]+)(?:\s*,\s*(?P=item))+(?![^\s,])")


# --------- PARSE ---------
@dataclass
class Case:
    status: str
    title: str
    rows: dict[str, list[str]] = field(default_factory=dict)
    clip_id: str = ""
    collection: str = ""
    folder: str = ""

    def value(self, label: str) -> str:
        return " ".join(self.rows.get(label, []))

    @property
    def job_id(self) -> str:
        return self.value("job")

    @property
    def result(self) -> str:
        return self.value("ergebnis")

    @property
    def extras(self) -> list[str]:
        return [value for label in ("fehler", "ist", "info") for value in self.rows.get(label, [])
                if value.casefold() not in noise_rows]


@dataclass
class Report:
    path: Path
    cases: list[Case]
    start: date | None
    end: date | None
    day: date | None


def parse_date(value: str) -> date | None:
    match = date_value.search(value)
    try:
        return date.fromisoformat(match.group()) if match else None
    except ValueError:
        return None


def parse_facts(case: Case, line: str) -> None:
    for segment in line.split("·"):
        label, _, value = segment.strip().partition(" ")
        if label.casefold() == "clip_id":
            case.clip_id = value.strip()
        elif label.casefold() == "kollektion":
            case.collection = value.strip()


def parse_cases(text: str) -> list[Case]:
    cases, current, last_label = [], None, None
    for line in text.splitlines():
        if match := block_start.match(line):
            current, last_label = Case(match.group(1), match.group(2).strip()), None
            cases.append(current)
            continue
        if current is None or not line.strip():
            last_label = None
            continue
        if line.strip().casefold().startswith("clip_id "):
            parse_facts(current, line)
            continue
        match = row_line.match(line)
        if match and match.group(1).casefold() in row_labels:
            last_label = match.group(1).casefold()
            current.rows.setdefault(last_label, []).append(match.group(2))
        elif last_label:
            # Multi-line values (e.g. exception texts) continue without row label.
            current.rows[last_label][-1] += f" {line.strip()}"
    return cases


def parse_report(path: Path, text: str) -> Report:
    start = end = None
    for line in text.splitlines():
        if match := period_line.match(line):
            start, end = parse_date(match.group(1)), parse_date(match.group(2))
            break
    match = date_in_name.search(path.name)
    return Report(path, parse_cases(text), start, end, (parse_date(match.group(1)) if match else None) or start)


def read_report_text(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        return None, f"Warnung: Bericht nicht lesbar, übersprungen: {path.name} ({error})"
    return (text, None) if text.strip() else (None, f"Warnung: leeren Bericht übersprungen: {path.name}")


def discover_reports(archive: Path) -> tuple[list[Path], str | None]:
    try:
        paths = [path for path in archive.iterdir()
                 if path.name.startswith(report_prefix) and path.suffix.casefold() == ".txt" and path.is_file()]
    except OSError as error:
        return [], f"Warnung: Archivverzeichnis nicht lesbar: {archive} ({error})"
    return sorted(paths, key=lambda path: path.name), None


# --------- STATE ---------
def build_clip_folder_index(state) -> dict[str, str]:
    clips = state.get("clips") if isinstance(state, dict) else None
    entries = clips.items() if isinstance(clips, dict) else ((None, clip) for clip in clips) if isinstance(clips, list) else ()
    folders = {}
    for key, clip in entries:
        if not isinstance(clip, dict):
            continue
        clip_id = next((str(value) for value in (clip.get("clip_id"), key)
                        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value)), None)
        files = clip.get("files")
        master = files.get("master") if isinstance(files, dict) else None
        folder = master.get("folder") if isinstance(master, dict) else None
        if clip_id and isinstance(folder, str) and folder:
            folders[clip_id] = folder
    return folders


def load_clip_folder_index(path: Path) -> tuple[dict[str, str], str | None]:
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, f"Warnung: {path} nicht gefunden; Ordner werden als „{missing_folder}“ ausgegeben."
    except (OSError, UnicodeError, ValueError) as error:
        return {}, f"Warnung: {path} nicht lesbar; Ordner werden als „{missing_folder}“ ausgegeben. ({error})"
    return build_clip_folder_index(state), None


# --------- CATEGORIZE ---------
def reasons(case: Case) -> list[str]:
    """Single reasons of a case: the result text without 'Manuelle Prüfung:', split at ';'."""
    text = manual_prefix.sub("", case.result) if case.result else "; ".join(case.extras)
    return [part.strip() for part in text.split(";") if part.strip()] or ["Ohne Angabe im Bericht"]


def pattern(reason: str) -> str:
    """Category key: variable parts replaced by placeholders, details in parentheses dropped."""
    text = reason
    while (stripped := parentheses.sub("", text)) != text:
        text = stripped
    for regex, replacement in placeholders:
        text = regex.sub(replacement, text)
    text = repeated_items.sub(r"\g<item>", text)
    return re.sub(r"\s+", " ", text).strip(" .;:") or reason


def categories(case: Case) -> list[str]:
    return list(dict.fromkeys(pattern(reason) for reason in reasons(case)))


# --------- COLLECT ---------
def date_in_window(report: Report, days: int, today: date) -> bool:
    return report.day is not None and today - timedelta(days=days - 1) <= report.day <= today


def collect_cases(reports: list[Report], clip_folders: dict[str, str]) -> list[Case]:
    """Unique cases per job; reports are sorted by time, so the latest entry of a job wins."""
    unique = {}
    for report in reports:
        for index, case in enumerate(report.cases):
            case.folder = clip_folders.get(case.clip_id, missing_folder)
            unique[case.job_id or f"{report.path.name}:{index}"] = case
    return list(unique.values())


def period_label(reports: list[Report]) -> str:
    dates = [value for report in reports for value in (report.start, report.end, report.day) if value]
    return f"{min(dates).isoformat()} bis {max(dates).isoformat()}" if dates else "nicht bestimmbar"


# --------- OUTPUT ---------
def case_lines(case: Case) -> list[str]:
    head = " · ".join(value for value in (case.clip_id or "Clip-ID fehlt", case.title, case.status, case.value("zeit")) if value)
    rows = [("Pfad", case.folder), ("Grund", case.result), *(("Zusatz", extra) for extra in case.extras), ("Job", case.job_id)]
    return [f"  {head}", *(f"    {label:<6} {value}" for label, value in rows if value)]


def render_summary(reports: list[Report], cases: list[Case]) -> str:
    grouped: dict[str, list[Case]] = {}
    for case in cases:
        for category in categories(case):
            grouped.setdefault(category, []).append(case)
    ranked = sorted(grouped, key=lambda category: (-len(grouped[category]), category.casefold()))
    width = len(str(len(ranked)))
    title = f"{app_name} {app_version} · Transcode"
    lines = [title, "=" * len(title), f"Zeitraum: {period_label(reports)}", f"Anzahl Berichte: {len(reports)}",
             f"Gesamtzahl Fälle: {len(cases)}", f"Kategorien: {len(ranked)}",
             "Fälle mit mehreren Gründen stehen in jeder passenden Kategorie.", "",
             "Kategorien nach Häufigkeit", "--------------------------"]
    lines += [f"  [{number:>{width}}] {len(grouped[category]):>5}  {category}" for number, category in enumerate(ranked, 1)]
    lines += [] if ranked else ["  Keine Fälle gefunden."]
    lines += ["", "Fälle je Kategorie", "------------------"]
    for number, category in enumerate(ranked, 1):
        lines += ["", f"[{number}] {category} ({len(grouped[category])})"]
        for case in sorted(grouped[category], key=lambda item: (item.job_id, item.title)):
            lines += case_lines(case)
    return "\n".join(lines) + "\n"


# --------- CONSOLE ---------
def show_progress(quiet: bool, message: str) -> None:
    if not quiet:
        sys.stderr.write(f"\r{message}")
        sys.stderr.flush()


def print_warning(quiet: bool, message: str) -> None:
    if not quiet:
        sys.stderr.write(f"\n{message}\n")
        sys.stderr.flush()


def load_reports(paths: list[Path], quiet: bool, days: int | None) -> tuple[list[Report], int]:
    reports, failed, today = [], 0, date.today()
    with ThreadPoolExecutor(max_workers=8) as executor:
        for index, (path, (text, warning)) in enumerate(zip(paths, executor.map(read_report_text, paths)), 1):
            show_progress(quiet, f"Auswerten [{index}/{len(paths)}] {path.name}")
            if warning:
                failed += 1
                print_warning(quiet, warning)
                continue
            report = parse_report(path, text)
            if days is None or date_in_window(report, days, today):
                reports.append(report)
    return reports, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fehlerfälle aus Transcode-Detailberichten nach Grund kategorisieren.")
    parser.add_argument("--archiv", type=Path, default=archive_dir, help=f"Archivverzeichnis (Standard: {archive_dir})")
    parser.add_argument("--ausgabe", type=Path, default=output_path, help=f"Zieldatei (Standard: {output_path})")
    parser.add_argument("--leise", action="store_true", help="Fortschritts- und Warnmeldungen unterdrücken")
    return parser


# --------- MAIN ---------
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()

    paths, warning = discover_reports(args.archiv)
    if warning:
        print_warning(args.leise, warning)
    clip_folders, warning = load_clip_folder_index(state_path)
    if warning:
        print_warning(args.leise, warning)

    reports, failed = load_reports(paths, args.leise, TAGE)
    cases = collect_cases(reports, clip_folders)
    show_progress(args.leise, f"Schreiben: {args.ausgabe}")
    args.ausgabe.parent.mkdir(parents=True, exist_ok=True)
    args.ausgabe.write_text(render_summary(reports, cases), encoding="utf-8")

    if not args.leise:
        print(f"\nFertig in {time.perf_counter() - started:.2f}s — Berichte: {len(reports)}/{len(paths)}, "
              f"Fehlerfälle: {len(cases)}, fehlerhafte Berichte: {failed}", file=sys.stderr)
    return 0


# --------- EXEC ---------
if __name__ == "__main__":
    raise SystemExit(main())
