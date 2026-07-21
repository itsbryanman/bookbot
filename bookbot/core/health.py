"""Library health checking and auditing."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .logging import get_logger
from .models import AudiobookSet

logger = get_logger("health_checker")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wav"}
SIDECAR_EXTENSIONS = {".nfo", ".cue", ".json", ".opf", ".xml", ".txt", ".log"}
COVER_NAMES = {"cover", "folder", "front", "albumart", "artwork", "thumb"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


class HealthReport(BaseModel):
    """Report from library health checks."""

    missing_covers: list[dict[str, Any]] = Field(default_factory=list)
    inconsistent_tags: list[dict[str, Any]] = Field(default_factory=list)
    orphaned_files: list[str] = Field(default_factory=list)
    duplicate_editions: list[list[dict[str, str]]] = Field(default_factory=list)
    series_gaps: list[dict[str, Any]] = Field(default_factory=list)
    format_inconsistencies: list[dict[str, Any]] = Field(default_factory=list)
    bitrate_anomalies: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def total_issues(self) -> int:
        return (
            len(self.missing_covers)
            + len(self.inconsistent_tags)
            + len(self.orphaned_files)
            + len(self.duplicate_editions)
            + len(self.series_gaps)
            + len(self.format_inconsistencies)
            + len(self.bitrate_anomalies)
        )

    def to_summary(self) -> dict[str, int]:
        """Return a summary dict of issue counts by category."""
        return {
            "missing_covers": len(self.missing_covers),
            "inconsistent_tags": len(self.inconsistent_tags),
            "orphaned_files": len(self.orphaned_files),
            "duplicate_editions": len(self.duplicate_editions),
            "series_gaps": len(self.series_gaps),
            "format_inconsistencies": len(self.format_inconsistencies),
            "bitrate_anomalies": len(self.bitrate_anomalies),
            "total": self.total_issues,
        }


class LibraryHealthChecker:
    """Audits an audiobook library for common issues."""

    def check_missing_covers(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[dict[str, Any]]:
        """Find books with no embedded cover art and no cover sidecar."""
        issues = []

        for ab_set in audiobook_sets:
            has_sidecar_cover = False
            source = ab_set.source_path

            if source.is_dir():
                for f in source.iterdir():
                    if f.suffix.lower() in IMAGE_EXTENSIONS:
                        if f.stem.lower() in COVER_NAMES:
                            has_sidecar_cover = True
                            break

            if not has_sidecar_cover:
                issues.append({
                    "path": str(ab_set.source_path),
                    "title": ab_set.raw_title_guess or ab_set.source_path.name,
                    "tracks": ab_set.total_tracks,
                })

        return issues

    def check_inconsistent_tags(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[dict[str, Any]]:
        """Find books where tracks have mismatched album/artist/albumartist tags."""
        issues = []

        for ab_set in audiobook_sets:
            if len(ab_set.tracks) < 2:
                continue

            albums = set()
            artists = set()
            albumartists = set()

            for track in ab_set.tracks:
                tags = track.existing_tags
                if tags.album:
                    albums.add(tags.album)
                if tags.artist:
                    artists.add(tags.artist)
                if tags.albumartist:
                    albumartists.add(tags.albumartist)

            mismatches = []
            if len(albums) > 1:
                mismatches.append(f"album: {albums}")
            if len(artists) > 1:
                mismatches.append(f"artist: {artists}")
            if len(albumartists) > 1:
                mismatches.append(f"albumartist: {albumartists}")

            if mismatches:
                issues.append({
                    "path": str(ab_set.source_path),
                    "title": ab_set.raw_title_guess or ab_set.source_path.name,
                    "mismatches": mismatches,
                })

        return issues

    def check_orphaned_files(self, library_path: Path) -> list[Path]:
        """Find non-audio files that are not covers, NFO, cue, or metadata sidecars."""
        orphaned = []

        for f in library_path.rglob("*"):
            if not f.is_file():
                continue

            suffix = f.suffix.lower()

            # Skip audio files
            if suffix in AUDIO_EXTENSIONS:
                continue

            # Skip known sidecar types
            if suffix in SIDECAR_EXTENSIONS:
                continue

            # Skip cover images
            if suffix in IMAGE_EXTENSIONS and f.stem.lower() in COVER_NAMES:
                continue

            # Skip hidden files
            if f.name.startswith("."):
                continue

            orphaned.append(f)

        return orphaned

    def check_duplicate_editions(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[list[AudiobookSet]]:
        """Find groups of sets that are edition-duplicates.

        Uses the dedupe engine for consistent clustering with the dedupe command.
        """
        from .dedupe import DedupeEngine

        if not audiobook_sets:
            return []

        # Use a dummy library root; we only need the analysis, not the plan
        library_root = audiobook_sets[0].source_path.parent
        engine = DedupeEngine(library_root)
        edition_groups = engine.analyze_editions(audiobook_sets)

        return [
            [c.audiobook_set for c in g.members]
            for g in edition_groups
        ]

    def check_series_gaps(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[dict[str, Any]]:
        """Detect missing volumes in a series."""
        series_map: dict[str, list[tuple[str, float]]] = {}

        for ab_set in audiobook_sets:
            series = ab_set.series_guess
            volume = ab_set.volume_guess
            if not series or not volume:
                continue

            try:
                vol_num = float(volume)
            except ValueError:
                continue

            series_key = series.lower().strip()
            if series_key not in series_map:
                series_map[series_key] = []
            series_map[series_key].append((series, vol_num))

        issues = []
        for series_key, volumes in series_map.items():
            if len(volumes) < 2:
                continue

            vol_nums = sorted(set(v[1] for v in volumes))
            series_name = volumes[0][0]

            # Check for gaps in integer sequences
            int_vols = [int(v) for v in vol_nums if v == int(v)]
            if len(int_vols) >= 2:
                expected = set(range(min(int_vols), max(int_vols) + 1))
                missing = expected - set(int_vols)
                if missing:
                    issues.append({
                        "series": series_name,
                        "found_volumes": sorted(int_vols),
                        "missing_volumes": sorted(missing),
                    })

        return issues

    def check_format_consistency(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[dict[str, Any]]:
        """Find books with mixed audio formats."""
        issues = []

        for ab_set in audiobook_sets:
            if len(ab_set.tracks) < 2:
                continue

            formats = set()
            for track in ab_set.tracks:
                formats.add(track.audio_format.value)

            if len(formats) > 1:
                issues.append({
                    "path": str(ab_set.source_path),
                    "title": ab_set.raw_title_guess or ab_set.source_path.name,
                    "formats": sorted(formats),
                })

        return issues

    def check_bitrate_anomalies(
        self, audiobook_sets: list[AudiobookSet]
    ) -> list[dict[str, Any]]:
        """Find tracks with significantly different bitrates within the same book."""
        issues = []

        for ab_set in audiobook_sets:
            if len(ab_set.tracks) < 2:
                continue

            bitrates = [t.bitrate for t in ab_set.tracks if t.bitrate]
            if len(bitrates) < 2:
                continue

            avg_bitrate = sum(bitrates) / len(bitrates)
            if avg_bitrate == 0:
                continue

            anomalies = []
            for track in ab_set.tracks:
                if track.bitrate and abs(track.bitrate - avg_bitrate) / avg_bitrate > 0.25:
                    anomalies.append({
                        "file": track.filename,
                        "bitrate": track.bitrate,
                        "expected": int(avg_bitrate),
                    })

            if anomalies:
                issues.append({
                    "path": str(ab_set.source_path),
                    "title": ab_set.raw_title_guess or ab_set.source_path.name,
                    "average_bitrate": int(avg_bitrate),
                    "anomalies": anomalies,
                })

        return issues

    def run_all_checks(
        self, library_path: Path, audiobook_sets: list[AudiobookSet]
    ) -> HealthReport:
        """Run all health checks and return a comprehensive report."""
        report = HealthReport(
            missing_covers=self.check_missing_covers(audiobook_sets),
            inconsistent_tags=self.check_inconsistent_tags(audiobook_sets),
            orphaned_files=[str(p) for p in self.check_orphaned_files(library_path)],
            duplicate_editions=[
                [
                    {
                        "path": str(ab.source_path),
                        "title": ab.raw_title_guess or ab.source_path.name,
                    }
                    for ab in group
                ]
                for group in self.check_duplicate_editions(audiobook_sets)
            ],
            series_gaps=self.check_series_gaps(audiobook_sets),
            format_inconsistencies=self.check_format_consistency(audiobook_sets),
            bitrate_anomalies=self.check_bitrate_anomalies(audiobook_sets),
        )

        logger.info(
            f"Health check complete: {report.total_issues} issues found",
            summary=report.to_summary(),
        )

        return report
