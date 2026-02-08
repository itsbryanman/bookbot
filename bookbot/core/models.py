"""Core data models for BookBot."""

from __future__ import annotations

import hashlib
import platform
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_core import PydanticUndefined


class AudioFormat(str, Enum):
    """Supported audio formats."""

    MP3 = "mp3"
    M4A = "m4a"
    M4B = "m4b"
    FLAC = "flac"
    OGG = "ogg"
    OPUS = "opus"
    AAC = "aac"
    WAV = "wav"


class TrackStatus(str, Enum):
    """Status of a track during processing."""

    PENDING = "pending"
    VALID = "valid"
    MISSING_NUMBER = "missing_number"
    DUPLICATE = "duplicate"
    SUSPICIOUS_DURATION = "suspicious_duration"
    MIXED_FORMAT = "mixed_format"
    ERROR = "error"


class MatchConfidence(str, Enum):
    """Confidence levels for metadata matching."""

    HIGH = "high"  # >0.85 - auto-select
    MEDIUM = "medium"  # 0.65-0.85 - needs confirmation
    LOW = "low"  # <0.65 - manual pick required


class AudioTags(BaseModel):
    """Audio file metadata tags."""

    title: str | None = None
    album: str | None = None
    artist: str | None = None
    albumartist: str | None = None
    track: int | None = None
    disc: int | None = None
    date: str | None = None
    genre: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: str | None = None
    narrator: str | None = None
    comment: str | None = None
    isbn: str | None = None
    asin: str | None = None

    # Raw tag dict for preservation
    raw_tags: dict[str, Any] = Field(default_factory=dict)


class Track(BaseModel):
    """Represents a single audio track/file."""

    src_path: Path
    disc: int = 1
    track_index: int
    duration: float | None = None  # seconds
    bitrate: int | None = None  # kbps
    channels: int | None = None
    sample_rate: int | None = None
    file_size: int = 0  # bytes
    audio_format: AudioFormat
    existing_tags: AudioTags = Field(default_factory=AudioTags)
    proposed_name: str | None = None
    proposed_tags: AudioTags | None = None
    status: TrackStatus = TrackStatus.PENDING
    warnings: list[str] = Field(default_factory=list)

    @field_validator("src_path", mode="before")
    def validate_path(cls, v: str | Path) -> Path:
        return Path(v) if isinstance(v, str) else v

    @property
    def filename(self) -> str:
        """Get the filename without path."""
        return self.src_path.name

    @property
    def stem(self) -> str:
        """Get the filename without extension."""
        return self.src_path.stem

    def get_content_hash(self) -> str:
        """Generate a hash of the file content for integrity checking."""
        hasher = hashlib.sha256()
        with open(self.src_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()


class ProviderIdentity(BaseModel):
    """Canonical identity from a metadata provider."""

    provider: str
    external_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    series_name: str | None = None
    series_index: str | None = None
    year: int | None = None
    language: str | None = None
    narrator: str | None = None
    edition: str | None = None
    publisher: str | None = None
    isbn_10: str | None = None
    isbn_13: str | None = None
    asin: str | None = None
    description: str | None = None
    cover_urls: list[str] = Field(default_factory=list)

    # Raw provider data for reference
    raw_data: dict[str, Any] = Field(default_factory=dict)


class MatchCandidate(BaseModel):
    """A potential match from a metadata provider."""

    identity: ProviderIdentity
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: MatchConfidence
    match_reasons: list[str] = Field(default_factory=list)

    @field_validator("confidence_level", mode="before")
    @classmethod
    def set_confidence_level(cls, v: Any, info: ValidationInfo) -> MatchConfidence:
        if v is not None and v is not PydanticUndefined:
            return MatchConfidence(v)

        confidence = info.data.get("confidence", 0.0)
        if confidence > 0.85:
            return MatchConfidence.HIGH
        elif confidence >= 0.65:
            return MatchConfidence.MEDIUM
        else:
            return MatchConfidence.LOW


class AudiobookSet(BaseModel):
    """Represents one logical audiobook with its tracks."""

    source_path: Path
    raw_title_guess: str | None = None
    author_guess: str | None = None
    series_guess: str | None = None
    volume_guess: str | None = None
    narrator_guess: str | None = None
    language_guess: str | None = None
    year_guess: int | None = None

    disc_count: int = 1
    total_tracks: int = 0
    total_duration: float | None = None

    tracks: list[Track] = Field(default_factory=list)
    provider_candidates: list[MatchCandidate] = Field(default_factory=list)
    chosen_identity: ProviderIdentity | None = None

    # Validation warnings
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_path", mode="before")
    def validate_path(cls, v: str | Path) -> Path:
        return Path(v) if isinstance(v, str) else v

    @property
    def has_multi_disc(self) -> bool:
        """Check if this set has multiple discs."""
        return self.disc_count > 1

    @property
    def track_count_by_disc(self) -> dict[int, int]:
        """Get track count per disc."""
        counts: dict[int, int] = {}
        for track in self.tracks:
            counts[track.disc] = counts.get(track.disc, 0) + 1
        return counts

    def get_tracks_for_disc(self, disc: int) -> list[Track]:
        """Get all tracks for a specific disc, sorted by track index."""
        disc_tracks = [t for t in self.tracks if t.disc == disc]
        return sorted(disc_tracks, key=lambda t: t.track_index)

    def validate_track_order(self) -> list[str]:
        """Validate track ordering and return any issues."""
        issues = []

        for disc in range(1, self.disc_count + 1):
            disc_tracks = self.get_tracks_for_disc(disc)
            if not disc_tracks:
                issues.append(f"Disc {disc} has no tracks")
                continue

            # Check for gaps in track numbering
            track_numbers = sorted([t.track_index for t in disc_tracks])
            expected = list(range(1, len(track_numbers) + 1))
            if track_numbers != expected:
                issues.append(
                    f"Disc {disc} has gaps in track numbering: {track_numbers}"
                )

            # Check for duplicates
            if len(track_numbers) != len(set(track_numbers)):
                duplicates = [n for n in track_numbers if track_numbers.count(n) > 1]
                issues.append(f"Disc {disc} has duplicate track numbers: {duplicates}")

        return issues


class OperationRecord(BaseModel):
    """Record of a file operation for undo functionality."""

    operation_id: str
    timestamp: datetime
    operation_type: str  # 'rename', 'retag', 'convert'

    # File operations
    old_path: Path | None = None
    new_path: Path | None = None
    old_tags: AudioTags | None = None
    new_tags: AudioTags | None = None

    # Integrity checks
    old_content_hash: str | None = None
    new_content_hash: str | None = None

    # Additional metadata
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("old_path", "new_path", mode="before")
    def validate_paths(cls, v: str | Path | None) -> Path | None:
        return Path(v) if isinstance(v, str) else v


class RenameOperation(BaseModel):
    """A single rename operation in a transaction."""

    old_path: Path
    new_path: Path
    temp_path: Path | None = None
    track: Track

    @field_validator("old_path", "new_path", "temp_path", mode="before")
    def validate_paths(cls, v: str | Path | None) -> Path | None:
        return Path(v) if isinstance(v, str) else v


class RenamePlan(BaseModel):
    """Complete plan for renaming operations."""

    plan_id: str
    created_at: datetime
    source_path: Path
    operations: list[RenameOperation] = Field(default_factory=list)
    audiobook_sets: list[AudiobookSet] = Field(default_factory=list)
    dry_run: bool = True

    # Plan validation
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("source_path", mode="before")
    def validate_path(cls, v: str | Path) -> Path:
        return Path(v) if isinstance(v, str) else v

    def validate_plan(self) -> bool:
        """Validate the rename plan for conflicts and issues."""
        self.conflicts.clear()
        self.warnings.clear()

        # Check for path conflicts
        new_paths = [op.new_path for op in self.operations]
        if len(new_paths) != len(set(new_paths)):
            duplicates = [p for p in new_paths if new_paths.count(p) > 1]
            self.conflicts.extend(
                [f"Duplicate target path: {p}" for p in set(duplicates)]
            )

        # Check for case-insensitive conflicts on case-insensitive filesystems
        is_case_insensitive = (
            sys.platform in {"darwin", "win32"}
            or platform.system().lower() == "windows"
        )
        if is_case_insensitive:
            lower_paths = [p.as_posix().lower() for p in new_paths]
            if len(lower_paths) != len(set(lower_paths)):
                self.warnings.append(
                    "Potential case-insensitive path conflicts detected"
                )

        return len(self.conflicts) == 0
