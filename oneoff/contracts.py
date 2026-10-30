from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Literal


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
    candidate_names: frozenset[str] | None
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
    raw: dict[str, Any]


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
    status: Literal[
        "clean",
        "unclear",
        "unsupported",
        "analysis_error",
        "repair_error",
        "verification_error",
        "published",
    ]
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
    packet_timestamps: tuple[tuple[int | None, int | None, int | None], ...]


ClipCollector = Callable[[Path], dict[str, Any]]
FfmpegRunner = Callable[
    [tuple[str, ...], Callable[[dict[str, str]], None] | None], CommandResult
]
AudioFingerprinter = Callable[[Path], tuple[AudioFingerprint, ...]]
NoReplaceRename = Callable[[Path, Path], None]
