#!/usr/bin/env python3
"""Summarize failed Transcode detail reports in reports/archive."""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Number of calendar days to include; None processes every matching report.
TAGE = None
REPO_ROOT = Path(__file__).resolve().parent
STATE_PATH = REPO_ROOT / "state" / "marathon.json"

TRANSCODE_REPORT_NAME = re.compile(
    r"details_transcode_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}\.txt"
)

BLOCK_START = re.compile(r"^#\s*\d+\s+(FAILED|REJECTED|BLOCKIERT)\b(.*)$", re.I)
FIELD = re.compile(
    r"^\s*(clip_id|Job|Zeit|Versuch|Ergebnis|Fehler|Ist|Info)\s+(.*?)\s*$",
    re.I,
)
DATE_IN_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2})_")
DATE_IN_TEXT = re.compile(r"Zeitraum\s+(.+?)\s+(?:–|—|-)\s+(.+?)\s*$", re.I)
DATE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}")
VARIABLES = (
    (re.compile(r"\bdata(?:\s*,\s*data)+\b", re.I), "<repeated-data>"),
    (re.compile(r"\b\d+(?:[.,]\d+)\b"), "<decimal>"),
    (re.compile(r"\b\d{8}T\d{6}\d+\b", re.I), "<job-time>"),
    (re.compile(r"\bclip_id\s*\d+\b", re.I), "clip_id <id>"),
    (re.compile(r"\b(?:clip|id|job)\s*#?\d+\b", re.I), "<identifier>"),
    (re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:bytes?|mb|gb|tb|streams?|kanäle?)\b", re.I), "<amount>"),
    (re.compile(r"\b\d+\s+von\s+\d+\b", re.I), "<attempt>"),
    (re.compile(r"\b(?:\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\b"), "<timecode>"),
    (re.compile(r"\b\d+\b"), "<number>"),
)


@dataclass
class ReportCase:
    status: str
    title: str
    clip_id: str = ""
    collection: str = ""
    job_id: str = ""
    result: str = ""
    extra_info: str = ""
    folder: str = ""
    stage: str = ""
    category: str = ""


@dataclass
class Report:
    path: Path
    cases: list[ReportCase]
    report_start: date | None
    report_end: date | None
    report_date: date | None


def parse_date(value: str) -> date | None:
    match = DATE_VALUE.search(value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group())
    except ValueError:
        return None


def report_dates(text: str) -> tuple[date | None, date | None]:
    for line in text.splitlines():
        match = DATE_IN_TEXT.match(line)
        if match:
            return parse_date(match.group(1)), parse_date(match.group(2))
    return None, None


def report_filename_date(path: Path) -> date | None:
    match = DATE_IN_NAME.search(path.name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def extract_stage(path: Path, text: str) -> str:
    match = re.match(r"details_([a-z]+)_", path.name, re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"Detailbericht\s+([\w-]+)", text, re.I)
    return match.group(1).lower() if match else ""


def parse_cases(text: str, stage: str) -> list[ReportCase]:
    cases = []
    current = None
    fields = {}

    def finish_case():
        if current is None:
            return
        current.clip_id = fields.get("clip_id", "")
        fact_segments = current.clip_id.split("·")
        current.clip_id = fact_segments[0].strip()
        for segment in fact_segments[1:]:
            collection_match = re.match(r"\s*Kollektion\s+(.+?)\s*$", segment, re.I)
            if collection_match:
                current.collection = collection_match.group(1).strip()
        current.job_id = fields.get("job", "").strip()
        current.result = fields.get("ergebnis", "").strip()
        current.extra_info = " ".join(
            fields.get(key, "").strip()
            for key in ("fehler", "ist", "info")
            if fields.get(key)
        )
        current.stage = stage
        current.category = categorize(current)
        cases.append(current)

    for line in text.splitlines():
        block_match = BLOCK_START.match(line)
        if block_match:
            finish_case()
            current = ReportCase(status=block_match.group(1).upper(), title=block_match.group(2).strip())
            fields = {}
            continue
        if current is None:
            continue
        field_match = FIELD.match(line)
        if not field_match:
            continue
        label, value = field_match.groups()
        label = label.lower()
        if label == "clip_id":
            fields[label] = value
        else:
            fields[label] = value

    finish_case()
    return cases


def categorize(case: ReportCase) -> str:
    source = case.result or case.extra_info
    text = normalize_category_text(source)
    if "analyse audio" in text and "source hat keinen ton" in text:
        return "audio_source_without_tone"
    if "zusätzliche quellstreams" in text and "nicht sicher übertragbar" in text:
        return "source_streams_not_transferable"
    if "ton unsicher" in text and "trust" in text:
        return "audio_channel_uncertain"
    if "hörbar und nicht doppelt" in text:
        return "audio_channel_not_duplicate"
    if "dublette mit kanal" in text:
        return "audio_duplicate_uncertain"
    if "kein hörbarer kanal" in text:
        return "no_audible_audio_channel"
    if "keine ffe-tafel" in text:
        return "missing_ffe_test_card"
    if "interlacing unsicher" in text:
        return "interlacing_uncertain"

    # Preserve the previous fallbacks for formats without a usable result field.
    if not case.result:
        text = normalize_category_text(
            f"{case.stage} {case.status} {case.title} {case.extra_info}"
        )
    if any(term in text for term in (
        "restore", "tape", "prüfbericht fehlt", "prüfbericht fehlt",
        "größenabweich", "groessenabweich", "doppelte einträge", "fehlende datei",
    )):
        return "restore_incomplete"
    if any(term in text for term in (
        "proxy", "mastername", "master name", "proxy-datei", "proxy-dateien",
        "ausgang enthält", "ausgang enthaelt",
    )):
        return "transcode_output_contract"
    if case.stage == "qc" or any(term in text for term in ("aqc", "kriterium", "stream", "kanal")):
        return "qc_rejected"
    if case.status == "BLOCKIERT" or any(term in text for term in (
        "kollision", "ablage", "ausliefer", "master",
    )):
        return "delivery_or_master_blocked"
    return "worker_failed"


CATEGORY_LABELS = {
    "audio_channel_not_duplicate": "Hörbarer, nicht doppelter Tonkanal",
    "audio_channel_uncertain": "Tonkanal unsicher",
    "audio_duplicate_uncertain": "Audiodublette unsicher",
    "audio_source_without_tone": "Quellmaterial ohne Ton",
    "delivery_or_master_blocked": "Ablage oder Master blockiert",
    "interlacing_uncertain": "Interlacing unsicher",
    "missing_ffe_test_card": "FFE-Testtafel fehlt",
    "no_audible_audio_channel": "Kein hörbarer Tonkanal",
    "qc_rejected": "Qualitätskontrolle abgelehnt",
    "restore_incomplete": "Wiederherstellung unvollständig",
    "source_streams_not_transferable": "Quellstreams nicht sicher übertragbar",
    "transcode_output_contract": "Transcode-Ausgabe fehlerhaft",
    "worker_failed": "Worker-Fehler",
}
MISSING_FOLDER = "nicht in marathon.json gefunden"


def normalize_clip_id(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    clip_id = str(value)
    return clip_id if clip_id else None


def build_clip_folder_index(state) -> dict[str, str]:
    if not isinstance(state, dict):
        return {}
    clips = state.get("clips")
    if isinstance(clips, dict):
        entries = clips.items()
    elif isinstance(clips, list):
        entries = ((None, clip) for clip in clips)
    else:
        return {}

    folders = {}
    for key, clip in entries:
        if not isinstance(clip, dict):
            continue
        clip_id = normalize_clip_id(clip.get("clip_id"))
        if clip_id is None:
            clip_id = normalize_clip_id(key)
        files = clip.get("files")
        master = files.get("master") if isinstance(files, dict) else None
        folder = master.get("folder") if isinstance(master, dict) else None
        if clip_id is not None and isinstance(folder, str) and folder:
            folders[clip_id] = folder
    return folders


def load_clip_folder_index(path: Path) -> tuple[dict[str, str], str | None]:
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {}, f"Warnung: {path.name} nicht gefunden; Ordner werden als „{MISSING_FOLDER}“ ausgegeben."
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {}, f"Warnung: {path.name} nicht lesbar; Ordner werden als „{MISSING_FOLDER}“ ausgegeben. ({error})"
    return build_clip_folder_index(state), None


def read_report_text(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return None, f"Warnung: Bericht nicht lesbar, übersprungen: {path.name} ({error})"
    if not text.strip():
        return None, f"Warnung: leeren Bericht übersprungen: {path.name}"
    return text, None


def parse_report(path: Path, text: str) -> Report:
    start, end = report_dates(text)
    report_date = report_filename_date(path) or start
    stage = extract_stage(path, text)
    return Report(path, parse_cases(text, stage), start, end, report_date)


def read_report(path: Path) -> Report | None:
    text, warning = read_report_text(path)
    if warning:
        print(warning, file=sys.stderr)
        return None
    return parse_report(path, text)


def normalize_category_text(text: str) -> str:
    normalized = text.lower()
    for pattern, replacement in VARIABLES:
        normalized = pattern.sub(replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def date_in_window(report: Report, days: int, today: date) -> bool:
    if report.report_date is None:
        return False
    return today - timedelta(days=days - 1) <= report.report_date <= today


def collect_cases(reports: list[Report], clip_folders: dict[str, str]) -> list[ReportCase]:
    unique = {}
    for report in reports:
        for index, case in enumerate(report.cases):
            case.folder = clip_folders.get(case.clip_id, MISSING_FOLDER)
            key = case.job_id or f"{report.path.name}:{index}"
            previous = unique.get(key)
            if previous is None or (
                previous.folder == MISSING_FOLDER and case.folder != MISSING_FOLDER
            ):
                unique[key] = case
    return list(unique.values())


def period_label(reports: list[Report]) -> str:
    dates = []
    for report in reports:
        dates.extend(value for value in (report.report_start, report.report_end, report.report_date) if value)
    if not dates:
        return "nicht bestimmbar"
    return f"{min(dates).isoformat()} bis {max(dates).isoformat()}"


def write_summary(path: Path, reports: list[Report], cases: list[ReportCase]) -> None:
    counts = {}
    grouped = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
        grouped.setdefault(case.category, []).append(case)
    categories = sorted(
        counts,
        key=lambda category: (-counts[category], CATEGORY_LABELS.get(category, category).casefold()),
    )
    lines = [
        "Auswertung der Detailberichte",
        "=============================",
        f"Zeitraum: {period_label(reports)}",
        f"Anzahl Berichte: {len(reports)}",
        f"Gesamtzahl Fälle: {len(cases)}",
        "",
        "Kategorien nach Häufigkeit",
        "--------------------------",
    ]
    if categories:
        lines.extend(
            f"  {CATEGORY_LABELS.get(category, category)}: {counts[category]}"
            for category in categories
        )
    else:
        lines.append("  Keine Fälle gefunden.")
    lines.extend(("", "Fälle je Kategorie", "------------------"))
    for category in categories:
        label = CATEGORY_LABELS.get(category, category)
        lines.extend(("", f"{label} ({counts[category]})"))
        for case in sorted(grouped[category], key=lambda item: (item.job_id, item.title)):
            job_id = case.job_id or "Job-ID fehlt"
            clip_id = case.clip_id or "Clip-ID fehlt"
            lines.append(
                f"  Job: {job_id} | Clip-ID: {clip_id} | Status: {case.status} | {case.title}"
            )
            lines.append(f"    Ordner: {case.folder}")
            if case.extra_info:
                lines.append(f"    Zusatzinfo: {case.extra_info}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcode-Detailberichte aus reports/archive zusammenfassen.")
    parser.add_argument(
        "--archiv",
        type=Path,
        default=REPO_ROOT / "reports" / "archive",
        help="Archivverzeichnis (Standard: reports/archive im Repo-Root)",
    )
    parser.add_argument(
        "--ausgabe",
        type=Path,
        default=REPO_ROOT / "auswertung_detailberichte.txt",
        help="Zieldatei (Standard: auswertung_detailberichte.txt im Repo-Root)",
    )
    parser.add_argument("--leise", action="store_true", help="Fortschritts- und Warnmeldungen unterdrücken")
    return parser


def discover_reports(archive: Path) -> tuple[list[Path], str | None]:
    try:
        with os.scandir(archive) as entries:
            paths = [
                Path(entry.path)
                for entry in entries
                if TRANSCODE_REPORT_NAME.fullmatch(entry.name)
                and entry.is_file()
            ]
    except OSError as error:
        return [], f"Warnung: Archivverzeichnis nicht gefunden: {archive} ({error})"
    paths.sort(key=lambda path: path.name)
    return paths, None


def show_progress(quiet: bool, message: str) -> None:
    if not quiet:
        sys.stderr.write(f"\r{message}")
        sys.stderr.flush()


def finish_progress(quiet: bool) -> None:
    if not quiet:
        sys.stderr.write("\n")
        sys.stderr.flush()


def print_warning(quiet: bool, message: str) -> None:
    if not quiet:
        sys.stderr.write(f"\n{message}\n")
        sys.stderr.flush()


def load_reports(paths: list[Path], quiet: bool, days: int | None) -> tuple[list[Report], int]:
    reports = []
    failed_reports = 0
    today = date.today()
    if not paths:
        return reports, failed_reports

    worker_count = min(8, len(paths))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        pending = deque()
        next_path = 0
        for _ in range(worker_count):
            pending.append(executor.submit(read_report_text, paths[next_path]))
            next_path += 1

        for index, path in enumerate(paths, start=1):
            show_progress(quiet, f"Lesen [{index}/{len(paths)}] {path.name}")
            text, warning = pending.popleft().result()
            if next_path < len(paths):
                pending.append(executor.submit(read_report_text, paths[next_path]))
                next_path += 1
            if warning:
                failed_reports += 1
                print_warning(quiet, warning)
                continue

            show_progress(quiet, f"Auswerten [{index}/{len(paths)}] {path.name}")
            report = parse_report(path, text)
            if days is None or date_in_window(report, days, today):
                reports.append(report)
    finish_progress(quiet)
    return reports, failed_reports


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    started = time.perf_counter()
    show_progress(args.leise, f"Suchen: {args.archiv}")
    paths, search_warning = discover_reports(args.archiv)
    if search_warning:
        print_warning(args.leise, search_warning)
    show_progress(args.leise, f"Suchen: {len(paths)} Detailberichte gefunden")
    finish_progress(args.leise)

    clip_folders, state_warning = load_clip_folder_index(STATE_PATH)
    if state_warning:
        print_warning(args.leise, state_warning)

    reports, failed_reports = load_reports(paths, args.leise, TAGE)
    show_progress(args.leise, "Auswerten: Fehlerfälle zusammenführen")
    cases = collect_cases(reports, clip_folders)
    finish_progress(args.leise)

    show_progress(args.leise, f"Schreiben: {args.ausgabe}")
    write_summary(args.ausgabe, reports, cases)
    finish_progress(args.leise)

    if not args.leise:
        elapsed = time.perf_counter() - started
        print(
            f"Fertig in {elapsed:.2f}s — Berichte: {len(reports)}, "
            f"Fehlerfälle: {len(cases)}, fehlerhafte Berichte: {failed_reports}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
