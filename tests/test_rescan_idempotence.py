"""Regression tests for apply→rescan idempotence root causes.

The user-flow round against v1.0.0 showed a just-applied library proposing
95 fresh operations on the very next scan. These tests pin the individual
root-cause fixes; the full-cycle behavior is covered by the user-flow
protocol (scan → apply → rescan must yield zero operations).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudioFormat, AudioTags, Track
from bookbot.core.planning import PlanBuilder
from bookbot.core.templates import TemplateEngine

FIXTURE_MP3 = (
    Path(__file__).parent
    / "fixtures"
    / "messy_library"
    / "Brandon Sanderson - The Way of Kings"
    / "CD1"
    / "01 - Prelude.mp3"
)


@pytest.fixture
def scanner() -> AudioFileScanner:
    return AudioFileScanner()


class TestAlbumDiscSplit:
    def test_disc_suffix_recognized(self, scanner: AudioFileScanner) -> None:
        assert scanner._album_disc_split("On Combat Disc 3") == ("On Combat", 3)
        assert scanner._album_disc_split("On Combat CD 12") == ("On Combat", 12)
        assert scanner._album_disc_split("Dune Part 2") == ("Dune", 2)

    def test_plain_album_untouched(self, scanner: AudioFileScanner) -> None:
        assert scanner._album_disc_split("The Stand") == ("The Stand", None)
        assert scanner._album_disc_split(None) == (None, None)

    def test_album_disc_feeds_disc_number(
        self, scanner: AudioFileScanner, tmp_path: Path
    ) -> None:
        """Album 'X Disc 3' provides disc evidence when no disc tag exists."""
        file_path = tmp_path / "01.mp3"
        file_path.touch()
        tags = AudioTags(album="On Combat Disc 3")
        assert scanner._get_disc_number(file_path, tags) == 3

    def test_explicit_disc_tag_wins(
        self, scanner: AudioFileScanner, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "01.mp3"
        file_path.touch()
        tags = AudioTags(album="On Combat Disc 3", disc="2")
        assert scanner._get_disc_number(file_path, tags) == 2


class TestParentAuthorLayout:
    def test_last_first_parent_accepted(self, scanner: AudioFileScanner) -> None:
        """Bookbot's own {AuthorLastFirst}/{Title} output must round-trip."""
        assert (
            scanner._parent_author_guess(Path("/lib/Sobol, Donald J."))
            == "Sobol, Donald J."
        )
        assert (
            scanner._parent_author_guess(Path("/lib/Grossman, Dave"))
            == "Grossman, Dave"
        )

    def test_series_artifact_rejected(self, scanner: AudioFileScanner) -> None:
        """'05, Encyclopedia Brown' style names never read as authors."""
        assert scanner._parent_author_guess(Path("/lib/05, Encyclopedia Brown")) is None

    def test_plain_container_folders_rejected(
        self, scanner: AudioFileScanner
    ) -> None:
        assert scanner._parent_author_guess(Path("/lib/audiobooks")) is None
        assert scanner._parent_author_guess(Path("/lib/My Cool Library")) is None


class TestAuthorLastFirstPassthrough:
    def test_already_flipped_name_unchanged(self) -> None:
        engine = TemplateEngine()
        assert (
            engine._format_author_last_first("Sobol, Donald J.")
            == "Sobol, Donald J."
        )

    def test_plain_name_flipped(self) -> None:
        engine = TemplateEngine()
        assert engine._format_author_last_first("Dave Grossman") == "Grossman, Dave"


class TestCompanionOperations:
    def _make_set(self, folder: Path, config_manager) -> tuple:
        scanner = AudioFileScanner()
        sets = scanner.scan_directory(folder)
        config = config_manager.load_config()
        return sets, PlanBuilder(config)

    def test_sidecars_travel_with_book(self, tmp_path: Path, config_manager) -> None:
        book = tmp_path / "Author Name - Some Book"
        book.mkdir()
        shutil.copy(FIXTURE_MP3, book / "01 - Track.mp3")
        (book / "cover.jpg").write_bytes(b"jpeg")
        (book / "metadata.opf").write_text("<opf/>")

        sets, builder = self._make_set(tmp_path, config_manager)
        plan = builder.create_plan(tmp_path, sets, source_roots=[tmp_path])

        sidecar_ops = [op for op in plan.operations if op.track is None]
        moved_names = {op.old_path.name for op in sidecar_ops}
        assert "cover.jpg" in moved_names
        assert "metadata.opf" in moved_names
        audio_destinations = {
            op.new_path.parent for op in plan.operations if op.track is not None
        }
        for op in sidecar_ops:
            assert op.new_path.parent in audio_destinations

    def test_shared_folder_sidecars_not_claimed(
        self, tmp_path: Path, config_manager
    ) -> None:
        """A folder holding audio from another set keeps its sidecars."""
        book = tmp_path / "Loose"
        book.mkdir()
        shutil.copy(FIXTURE_MP3, book / "01 - Track.mp3")
        # Foreign audio the scanner won't parse into the same set's tracks:
        # simulate by planning with a set whose tracks exclude one audio file.
        foreign = book / "other.mp3"
        shutil.copy(FIXTURE_MP3, foreign)
        (book / "cover.jpg").write_bytes(b"jpeg")

        config = config_manager.load_config()
        builder = PlanBuilder(config)
        track = Track(
            src_path=book / "01 - Track.mp3",
            disc=1,
            track_index=1,
            audio_format=AudioFormat.MP3,
        )
        from bookbot.core.models import AudiobookSet

        one_track_set = AudiobookSet(
            source_path=book,
            raw_title_guess="Loose",
            disc_count=1,
            total_tracks=1,
            tracks=[track],
        )
        ops = builder._companion_operations(one_track_set, tmp_path / "dest")
        assert ops == []
