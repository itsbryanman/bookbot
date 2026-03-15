"""Chapter detection from multiple sources."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.logging import get_logger
from ..core.models import AudiobookSet
from .models import Chapter

if TYPE_CHECKING:
    from ..providers.audnexus import AudnexusProvider

logger = get_logger("chapter_detector")


class ChapterDetector:
    """Detects chapters from various sources."""

    def detect_from_silence(
        self,
        audio_files: list[Path],
        noise_db: float = -50.0,
        min_silence_sec: float = 2.0,
        min_chapter_gap_sec: float = 300.0,
    ) -> list[Chapter]:
        """Detect chapters from silence gaps in audio files.

        Uses FFmpeg silencedetect filter to find silence boundaries.
        For multi-file books, offsets timestamps by cumulative duration.
        """
        all_silences: list[tuple[float, float]] = []
        cumulative_offset = 0.0
        total_duration = 0.0

        for audio_file in audio_files:
            cmd = [
                "ffmpeg",
                "-i",
                str(audio_file),
                "-af",
                f"silencedetect=n={noise_db}dB:d={min_silence_sec}",
                "-f",
                "null",
                "-",
            ]

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300
                )
                stderr = result.stderr

                # Parse silence_start and silence_end from stderr
                starts = re.findall(r"silence_start:\s*([\d.]+)", stderr)
                ends = re.findall(
                    r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)",
                    stderr,
                )

                for i, start_str in enumerate(starts):
                    start = float(start_str) + cumulative_offset
                    if i < len(ends):
                        end = float(ends[i][0]) + cumulative_offset
                    else:
                        end = start + min_silence_sec
                    all_silences.append((start, end))

                # Get duration of this file for offset calculation
                duration = self._get_file_duration(audio_file)
                cumulative_offset += duration
                total_duration = cumulative_offset

            except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
                logger.warning(f"Silence detection failed for {audio_file}: {e}")
                # Estimate duration from file if ffmpeg fails
                cumulative_offset += 0.0

        if not all_silences:
            return []

        # Filter: prefer silences > 3s, space chapters at least min_chapter_gap_sec apart
        chapter_breaks = []
        last_break = 0.0
        for start, end in all_silences:
            duration = end - start
            if duration >= 3.0 and (start - last_break) >= min_chapter_gap_sec:
                midpoint = (start + end) / 2.0
                chapter_breaks.append(midpoint)
                last_break = midpoint

        # Build chapters from breaks
        chapters = []
        for i, break_time in enumerate(chapter_breaks):
            start_ms = int(
                (chapter_breaks[i - 1] if i > 0 else 0.0) * 1000
            )
            end_ms = int(break_time * 1000)
            chapters.append(
                Chapter(
                    title=f"Chapter {i + 1}",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    source="silence",
                )
            )

        # Add final chapter
        if chapter_breaks and total_duration > 0:
            chapters.append(
                Chapter(
                    title=f"Chapter {len(chapters) + 1}",
                    start_ms=int(chapter_breaks[-1] * 1000),
                    end_ms=int(total_duration * 1000),
                    source="silence",
                )
            )

        return chapters

    def detect_from_tracks(self, audiobook_set: AudiobookSet) -> list[Chapter]:
        """Detect chapters from track boundaries.

        Each track file becomes one chapter.
        """
        chapters = []
        cumulative_ms = 0

        sorted_tracks = sorted(
            audiobook_set.tracks, key=lambda t: (t.disc, t.track_index)
        )

        for i, track in enumerate(sorted_tracks):
            title = (
                track.existing_tags.title
                if track.existing_tags.title
                else f"Chapter {i + 1}"
            )
            duration_ms = int((track.duration or 0) * 1000)

            chapters.append(
                Chapter(
                    title=title,
                    start_ms=cumulative_ms,
                    end_ms=cumulative_ms + duration_ms if duration_ms else None,
                    source="tracks",
                )
            )
            cumulative_ms += duration_ms

        return chapters

    async def detect_from_audnexus(
        self, asin: str, provider: "AudnexusProvider"
    ) -> list[Chapter] | None:
        """Detect chapters from Audnexus API data."""
        chapter_data = await provider.get_chapters(asin)
        if not chapter_data:
            return None

        chapters = []
        for ch in chapter_data:
            start_ms = ch.get("start_ms", 0)
            length_ms = ch.get("length_ms", 0)
            chapters.append(
                Chapter(
                    title=ch.get("title", f"Chapter {len(chapters) + 1}"),
                    start_ms=start_ms,
                    end_ms=start_ms + length_ms if length_ms else None,
                    source="audnexus",
                )
            )

        return chapters if chapters else None

    def detect_from_cue(self, cue_path: Path) -> list[Chapter]:
        """Parse a .cue file for chapter timecodes."""
        chapters = []

        try:
            content = cue_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        current_title = ""
        track_pattern = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO", re.IGNORECASE)
        title_pattern = re.compile(r'^\s*TITLE\s+"([^"]*)"', re.IGNORECASE)
        index_pattern = re.compile(
            r"^\s*INDEX\s+01\s+(\d+):(\d+):(\d+)", re.IGNORECASE
        )

        for line in content.splitlines():
            title_match = title_pattern.match(line)
            if title_match:
                current_title = title_match.group(1)

            index_match = index_pattern.match(line)
            if index_match:
                minutes = int(index_match.group(1))
                seconds = int(index_match.group(2))
                frames = int(index_match.group(3))
                start_ms = (minutes * 60 + seconds) * 1000 + int(
                    frames * 1000 / 75
                )

                title = current_title or f"Chapter {len(chapters) + 1}"
                chapters.append(
                    Chapter(
                        title=title,
                        start_ms=start_ms,
                        end_ms=None,
                        source="cue",
                    )
                )
                current_title = ""

        # Fill in end_ms from next chapter's start
        for i in range(len(chapters) - 1):
            chapters[i].end_ms = chapters[i + 1].start_ms

        return chapters

    async def auto_detect(
        self,
        audiobook_set: AudiobookSet,
        audnexus: "AudnexusProvider | None" = None,
    ) -> list[Chapter]:
        """Auto-detect chapters using strategy chain.

        Order: Audnexus -> cue file -> track boundaries -> silence detection.
        Returns first successful result.
        """
        # Strategy 1: Audnexus (if ASIN available)
        if audnexus:
            asin = None
            if audiobook_set.chosen_identity:
                asin = audiobook_set.chosen_identity.asin
            if not asin:
                for track in audiobook_set.tracks:
                    if track.existing_tags.asin:
                        asin = track.existing_tags.asin
                        break

            if asin:
                chapters = await self.detect_from_audnexus(asin, audnexus)
                if chapters:
                    return chapters

        # Strategy 2: Cue file
        source_path = audiobook_set.source_path
        for cue_file in source_path.glob("*.cue"):
            chapters = self.detect_from_cue(cue_file)
            if chapters:
                return chapters

        # Strategy 3: Track boundaries
        if len(audiobook_set.tracks) > 1:
            chapters = self.detect_from_tracks(audiobook_set)
            if chapters:
                return chapters

        # Strategy 4: Silence detection
        audio_files = sorted(
            [t.src_path for t in audiobook_set.tracks],
            key=lambda p: p.name,
        )
        if audio_files:
            chapters = self.detect_from_silence(audio_files)
            if chapters:
                return chapters

        return []

    def _get_file_duration(self, file_path: Path) -> float:
        """Get duration of an audio file in seconds using ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(file_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                import json

                data = json.loads(result.stdout)
                return float(data.get("format", {}).get("duration", 0))
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        return 0.0
