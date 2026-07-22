"""Environment and library health checks for BookBot."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config.models import Config
from .dedupe import DedupeEngine
from .discovery import (
    QUARANTINE_DIRNAME,
    AudioFileScanner,
    iter_files_excluding_quarantine,
)
from .planning import PlanBuilder


@dataclass
class DoctorCheck:
    """One health check result."""

    status: str
    message: str

    @property
    def icon(self) -> str:
        return {
            "ok": "✓",
            "warn": "⚠",
            "fail": "✕",
        }[self.status]


@dataclass
class DoctorReport:
    """Doctor output for CLI rendering."""

    checks: list[DoctorCheck] = field(default_factory=list)

    def add(self, status: str, message: str) -> None:
        self.checks.append(DoctorCheck(status=status, message=message))

    @property
    def has_failures(self) -> bool:
        return any(check.status == "fail" for check in self.checks)


@dataclass
class QuarantineTransactionRecord:
    """Quarantine transaction metadata recovered from local transaction files."""

    transaction_id: str
    timestamp: str | None
    present_in_current_logs: bool


@dataclass
class QuarantineSummary:
    """Summarized quarantine state for doctor messaging."""

    transaction_count: int
    total_size: int
    transactions: list[QuarantineTransactionRecord] = field(default_factory=list)


class LibraryDoctor:
    """Run environment and library checks."""

    AUDIOISH_EXTENSIONS = {
        ".aa",
        ".aax",
        ".aaxc",
        ".alac",
        ".mka",
        ".m4p",
        ".wma",
    }
    COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "cover.webp"}
    FORBIDDEN_FILENAME_CHARS = set('<>:"\\|?*')

    def __init__(self, config: Config, config_dir: Path):
        self.config = config
        self.config_dir = config_dir
        self.scanner = AudioFileScanner(recursive=True, max_depth=8)

    def run(
        self, library_path: Path | None = None, profile_name: str | None = None
    ) -> DoctorReport:
        """Run the configured checks."""
        report = DoctorReport()
        self._check_python(report)
        ffmpeg_ready = self._check_ffmpeg(report)
        self._check_runtime_mode(report, ffmpeg_ready)
        self._check_writable_path(report, self.config_dir, "Config directory writable")
        self._check_writable_path(
            report, self.config.cache_directory, "Cache directory writable"
        )
        self._check_provider_config(report)

        if library_path is not None:
            self._check_library(report, library_path, profile_name)

        return report

    def _check_python(self, report: DoctorReport) -> None:
        version = sys.version_info
        if version >= (3, 10):
            report.add("ok", f"Python {version.major}.{version.minor}.{version.micro}")
        else:
            report.add("fail", "Python 3.10 or newer is required")

    def _check_ffmpeg(self, report: DoctorReport) -> bool:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            report.add("fail", "FFmpeg not found in PATH")
            return False

        report.add("ok", f"FFmpeg installed at {ffmpeg}")

        try:
            codecs = subprocess.run(
                [ffmpeg, "-hide_banner", "-codecs"],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
            if all(codec in codecs.stdout for codec in ("aac", "mp3", "flac")):
                report.add("ok", "FFmpeg codecs available for AAC/MP3/FLAC")
            else:
                report.add(
                    "warn", "FFmpeg is installed but some expected codecs are missing"
                )
        except (OSError, subprocess.SubprocessError):
            report.add("warn", "FFmpeg codecs could not be inspected")

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                output = Path(temp_dir) / "doctor-smoke.m4b"
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-nostdin",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=22050:cl=mono",
                        "-t",
                        "0.1",
                        "-c:a",
                        "aac",
                        "-y",
                        str(output),
                    ],
                    capture_output=True,
                    check=True,
                    text=True,
                    timeout=15,
                )
                if output.exists():
                    report.add("ok", "Sample M4B conversion succeeded")
                    return True
        except (OSError, subprocess.SubprocessError):
            report.add("warn", "FFmpeg is installed but sample conversion failed")
            return False

        report.add("warn", "FFmpeg conversion smoke test produced no output")
        return False

    def _check_runtime_mode(self, report: DoctorReport, ffmpeg_ready: bool) -> None:
        in_docker = Path("/.dockerenv").exists()
        if (
            in_docker
            and ffmpeg_ready
            and os.environ.get("BOOKBOT_CONFIG_DIR") == "/config"
        ):
            report.add("ok", "Docker install is healthy")
        elif in_docker:
            report.add(
                "warn",
                "Docker install is running, but one or more runtime checks failed",
            )
        else:
            report.add("ok", "Host install detected")

    def _check_writable_path(
        self, report: DoctorReport, path: Path, label: str
    ) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix="bookbot-", delete=True):
                pass
            report.add("ok", label)
        except OSError:
            report.add("fail", label.replace("writable", "not writable"))

    def _check_provider_config(self, report: DoctorReport) -> None:
        if self.config.providers.google_books.api_key:
            report.add("ok", "Google Books API key configured")
        else:
            report.add("warn", "Google Books API key missing")

        if self.config.providers.audible.enabled:
            mkt = self.config.providers.audible.marketplace
            report.add("ok", f"Audible marketplace set to {mkt}")
        else:
            report.add("warn", "Audible provider disabled")

    def _check_library(
        self, report: DoctorReport, library_path: Path, profile_name: str | None
    ) -> None:
        if not library_path.exists() or not library_path.is_dir():
            report.add(
                "fail", f"Library path is not a readable directory: {library_path}"
            )
            return

        if os.access(library_path, os.R_OK):
            report.add("ok", "Library directory readable")
        else:
            report.add("fail", "Library directory is not readable")

        if os.access(library_path, os.W_OK):
            report.add("ok", "Library directory writable")
        else:
            report.add("warn", "Library directory is read-only")

        quarantine_summary = self._quarantine_summary(library_path)
        if quarantine_summary is not None:
            report.add("warn", self._format_quarantine_summary(quarantine_summary))

        audiobook_sets = self.scanner.scan_directory(library_path)
        if audiobook_sets:
            report.add("ok", f"Detected {len(audiobook_sets)} audiobook set(s)")
        else:
            report.add("warn", "No supported audiobooks detected in library")
            return

        plan = PlanBuilder(self.config).create_plan(
            library_root=library_path,
            audiobook_sets=audiobook_sets,
            profile_name=profile_name,
            source_roots=[library_path],
        )
        if plan.conflicts:
            report.add(
                "fail", f"Plan validation found {len(plan.conflicts)} conflict(s)"
            )
        else:
            report.add("ok", "Folder structure compatible")

        duplicate_titles = self._find_duplicate_title_groups(
            library_path,
            audiobook_sets,
        )
        if duplicate_titles:
            report.add(
                "warn",
                self._format_duplicate_title_warning(duplicate_titles),
            )
        else:
            report.add("ok", "No duplicate titles detected")

        missing_narrators = sum(
            1
            for audiobook_set in audiobook_sets
            if not (
                audiobook_set.narrator_guess
                or (
                    audiobook_set.chosen_identity
                    and audiobook_set.chosen_identity.narrator
                )
            )
        )
        if missing_narrators:
            report.add("warn", f"{missing_narrators} books missing narrator")
        else:
            report.add("ok", "Narrator data found")

        broken_chapters = sum(
            1
            for audiobook_set in audiobook_sets
            if audiobook_set.validate_track_order()
        )
        if broken_chapters:
            report.add("fail", f"{broken_chapters} books have broken chapters")
        else:
            report.add("ok", "Chapter ordering looks valid")

        missing_covers = self._count_missing_covers(audiobook_sets)
        if missing_covers:
            report.add("warn", f"{missing_covers} books missing cover art")
        else:
            report.add("ok", "Cover art found")

        suspicious_tracks = sum(
            1
            for audiobook_set in audiobook_sets
            for track in audiobook_set.tracks
            if track.duration is not None
            and track.duration < 60
            and audiobook_set.total_tracks > 1
        )
        if suspicious_tracks:
            report.add(
                "warn", f"{suspicious_tracks} suspiciously short tracks detected"
            )
        else:
            report.add("ok", "Track durations look reasonable")

        broken_metadata = sum(
            1
            for audiobook_set in audiobook_sets
            for track in audiobook_set.tracks
            if track.status == "error" or track.warnings
        )
        if broken_metadata:
            report.add(
                "warn", f"{broken_metadata} tracks have broken or unreadable metadata"
            )
        else:
            report.add("ok", "Metadata scan completed without file-level errors")

        unsupported_formats = self._find_unsupported_formats(library_path)
        if unsupported_formats:
            report.add(
                "warn",
                f"{len(unsupported_formats)} unsupported audio file(s): "
                + ", ".join(str(path.name) for path in unsupported_formats[:3]),
            )
        else:
            report.add("ok", "No unsupported audio formats detected")

        unsafe_names = self._find_unsafe_filenames(library_path)
        if unsafe_names:
            report.add(
                "warn",
                f"{len(unsafe_names)} files contain Windows/Samba-hostile characters",
            )
        else:
            report.add("ok", "Filenames are Windows/Samba-safe")

        duplicate_files = self._find_duplicate_file_groups(library_path)
        if duplicate_files:
            report.add(
                "warn",
                self._format_duplicate_file_warning(duplicate_files),
            )
        else:
            report.add("ok", "No obvious duplicate files detected")

    def _find_duplicate_title_groups(
        self, library_path: Path, audiobook_sets: list
    ) -> list[list[str]]:
        engine = DedupeEngine(library_path)
        groups = engine.analyze_editions(audiobook_sets)
        return [
            [str(candidate.audiobook_set.source_path) for candidate in group.members]
            for group in groups
        ]

    def _count_missing_covers(self, audiobook_sets: list) -> int:
        missing = 0
        for audiobook_set in audiobook_sets:
            files = {path.name.lower() for path in audiobook_set.source_path.glob("*")}
            if not files.intersection(self.COVER_NAMES):
                missing += 1
        return missing

    def _find_unsupported_formats(self, library_path: Path) -> list[Path]:
        unsupported = []
        for path in iter_files_excluding_quarantine(library_path):
            if path.is_file() and path.suffix.lower() in self.AUDIOISH_EXTENSIONS:
                unsupported.append(path)
        return unsupported

    def _find_unsafe_filenames(self, library_path: Path) -> list[Path]:
        unsafe = []
        for path in iter_files_excluding_quarantine(library_path):
            if path.is_file() and any(
                char in self.FORBIDDEN_FILENAME_CHARS for char in path.name
            ):
                unsafe.append(path)
        return unsafe

    def _quarantine_summary(self, library_path: Path) -> QuarantineSummary | None:
        """Summarize quarantine state under the scanned library root."""
        quarantine_root = library_path / QUARANTINE_DIRNAME
        if not quarantine_root.exists() or not quarantine_root.is_dir():
            return None

        transaction_dirs = [path for path in quarantine_root.iterdir() if path.is_dir()]
        total_size = 0
        for path in quarantine_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                total_size += path.stat().st_size
            except OSError:
                continue

        return QuarantineSummary(
            transaction_count=len(transaction_dirs),
            total_size=total_size,
            transactions=self._quarantine_transaction_records(transaction_dirs),
        )

    def _quarantine_transaction_records(
        self, transaction_dirs: list[Path]
    ) -> list[QuarantineTransactionRecord]:
        """Recover transaction ids and timestamps from quarantine-local logs."""
        log_dir = self.config.log_directory
        records: list[QuarantineTransactionRecord] = []

        for transaction_dir in sorted(transaction_dirs):
            for log_path in sorted(transaction_dir.glob("transaction_*.json")):
                try:
                    log_data = json.loads(log_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

                transaction_id = str(
                    log_data.get("transaction_id") or transaction_dir.name
                )
                timestamp = log_data.get("timestamp")
                records.append(
                    QuarantineTransactionRecord(
                        transaction_id=transaction_id,
                        timestamp=str(timestamp) if timestamp else None,
                        present_in_current_logs=(
                            log_dir / f"transaction_{transaction_id}.json"
                        ).exists(),
                    )
                )
                break

        return records

    def _format_quarantine_summary(self, summary: QuarantineSummary) -> str:
        """Render an actionable quarantine summary for doctor output."""
        message = (
            "Quarantine contains "
            f"{summary.transaction_count} transaction(s) totaling "
            f"{summary.total_size} bytes."
        )

        if not summary.transactions:
            return (
                f"{message} Review the quarantine folders directly because no "
                "transaction record could be read."
            )

        record_descriptions = [
            record.transaction_id
            if record.timestamp is None
            else f"{record.transaction_id} ({record.timestamp})"
            for record in summary.transactions[:3]
        ]
        message += " Transactions: " + ", ".join(record_descriptions) + "."

        missing_from_logs = [
            record for record in summary.transactions
            if not record.present_in_current_logs
        ]
        if missing_from_logs:
            undo_examples = ", ".join(
                f"'bookbot undo {record.transaction_id}'"
                for record in missing_from_logs[:3]
            )
            return (
                f"{message} History for the current config may not list these "
                f"quarantine-local transaction(s). Run {undo_examples} from the "
                "library root, or reuse the original --config-dir."
            )

        return (
            f"{message} Use 'bookbot history' or 'bookbot undo <id>' to review "
            "or restore."
        )

    def _find_duplicate_file_groups(self, library_path: Path) -> list[list[str]]:
        engine = DedupeEngine(library_path)
        groups = engine.analyze_files()
        return [[str(path) for path in group.paths] for group in groups]

    def _format_duplicate_title_warning(self, groups: list[list[str]]) -> str:
        """Render duplicate edition warnings with example paths."""
        return self._format_duplicate_warning(
            groups,
            prefix=f"{len(groups)} duplicate edition group(s) detected",
        )

    def _format_duplicate_file_warning(self, groups: list[list[str]]) -> str:
        """Render duplicate file warnings with example paths."""
        return self._format_duplicate_warning(
            groups,
            prefix=f"{len(groups)} potential duplicate file group(s) detected",
        )

    def _format_duplicate_warning(
        self, groups: list[list[str]], *, prefix: str
    ) -> str:
        """Format up to three duplicate groups with concrete paths."""
        lines = [prefix]
        for index, group in enumerate(groups[:3], 1):
            lines.append(f"  Example {index}:")
            lines.extend(f"    {path}" for path in group)
        return "\n".join(lines)
