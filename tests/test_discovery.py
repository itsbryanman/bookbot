"""Tests for audio file discovery functionality."""

import tempfile
from pathlib import Path

import pytest

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudioTags


class TestAudioFileScanner:
    """Test cases for AudioFileScanner."""

    @staticmethod
    def _patch_audio_metadata(
        scanner: AudioFileScanner,
        monkeypatch: pytest.MonkeyPatch,
        tag_map: dict[Path, AudioTags],
        duration_map: dict[Path, float | None] | None = None,
    ) -> None:
        duration_map = duration_map or {}

        def fake_extract_audio_tags(file_path: Path) -> AudioTags:
            return tag_map.get(file_path, AudioTags())

        def fake_extract_audio_properties(
            file_path: Path,
        ) -> tuple[float | None, int | None, int | None, int | None]:
            return duration_map.get(file_path), None, None, None

        monkeypatch.setattr(scanner, "_extract_audio_tags", fake_extract_audio_tags)
        monkeypatch.setattr(
            scanner, "_extract_audio_properties", fake_extract_audio_properties
        )

    def test_supported_extensions(self):
        """Test that all expected audio formats are supported."""
        scanner = AudioFileScanner()

        expected_extensions = {
            ".mp3",
            ".m4a",
            ".m4b",
            ".flac",
            ".ogg",
            ".opus",
            ".aac",
            ".wav",
        }

        assert set(scanner.SUPPORTED_EXTENSIONS.keys()) == expected_extensions

    def test_track_number_extraction_leading_digits(self):
        """Test extraction of track numbers from leading digits."""
        scanner = AudioFileScanner()

        test_cases = [
            ("01 Chapter One.mp3", 1),
            ("001 Introduction.m4a", 1),
            ("23 The Adventure Continues.flac", 23),
            ("99_final_chapter.mp3", 99),
        ]

        for filename, expected_track in test_cases:
            path = Path(filename)
            # Create a mock AudioTags object for testing
            from bookbot.core.models import AudioTags

            tags = AudioTags()

            result = scanner._get_track_number(path, tags)
            assert result == expected_track, f"Failed for {filename}"

    def test_track_number_extraction_patterns(self):
        """Test extraction of track numbers from various patterns."""
        scanner = AudioFileScanner()

        test_cases = [
            ("Track 05 - Something.mp3", 5),
            ("Chapter 12.m4a", 12),
            ("Part 3 - The End.flac", 3),
        ]

        for filename, expected_track in test_cases:
            path = Path(filename)
            from bookbot.core.models import AudioTags

            tags = AudioTags()

            result = scanner._get_track_number(path, tags)
            assert result == expected_track, f"Failed for {filename}"

    def test_disc_number_extraction(self):
        """Test extraction of disc numbers."""
        scanner = AudioFileScanner()

        test_cases = [
            ("CD1/01 Track.mp3", 1),
            ("Disc 2/Track 05.m4a", 2),
            ("Book 3/Chapter 1.flac", 3),
            ("Volume 4/Part 1.mp3", 4),
        ]

        for full_path, expected_disc in test_cases:
            path = Path(full_path)
            from bookbot.core.models import AudioTags

            tags = AudioTags()

            result = scanner._get_disc_number(path, tags)
            assert result == expected_disc, f"Failed for {full_path}"

    def test_metadata_guesses_author_title_pattern(self):
        """Test extraction of metadata from folder names."""
        scanner = AudioFileScanner()

        # Test author - title pattern
        folder_path = Path("Brandon Sanderson - The Way of Kings")
        tracks = []  # Empty tracks for this test

        title, author, series, volume = scanner._extract_metadata_guesses(
            folder_path, tracks
        )

        assert title == "The Way of Kings"
        assert author == "Brandon Sanderson"

    def test_metadata_guesses_series_pattern(self):
        """Test extraction of series information."""
        scanner = AudioFileScanner()

        # Test series pattern
        folder_path = Path("Stormlight Archive Book 1")
        tracks = []

        title, author, series, volume = scanner._extract_metadata_guesses(
            folder_path, tracks
        )

        assert series == "Stormlight Archive"
        assert volume == "1"

    @pytest.fixture
    def temp_audio_structure(self):
        """Create a temporary directory structure with mock audio files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create audiobook structure
            book1_dir = temp_path / "Brandon Sanderson - The Way of Kings"
            book1_dir.mkdir()

            disc1_dir = book1_dir / "CD1"
            disc1_dir.mkdir()

            # Create some mock audio files
            files = [
                disc1_dir / "01 Prologue.mp3",
                disc1_dir / "02 Chapter 1.mp3",
                disc1_dir / "03 Chapter 2.mp3",
            ]

            for file_path in files:
                file_path.touch()  # Create empty file

            yield temp_path

    def test_find_audio_files(self, temp_audio_structure):
        """Test finding audio files in directory structure."""
        scanner = AudioFileScanner(recursive=True, max_depth=3)

        files = scanner._find_audio_files(temp_audio_structure)

        # Should find 3 MP3 files
        assert len(files) == 3
        assert all(f.suffix == ".mp3" for f in files)
        assert all("Chapter" in f.stem or "Prologue" in f.stem for f in files)

    def test_scan_directory_ignores_quarantine_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=4)
        visible_file = tmp_path / "Visible Book.m4b"
        visible_file.touch()

        quarantined_file = (
            tmp_path
            / ".bookbot-quarantine"
            / "tx-1"
            / "Hidden Duplicate.m4b"
        )
        quarantined_file.parent.mkdir(parents=True)
        quarantined_file.touch()

        self._patch_audio_metadata(
            scanner,
            monkeypatch,
            {
                visible_file: AudioTags(album="Visible Book", artist="Author"),
                quarantined_file: AudioTags(album="Hidden Duplicate", artist="Author"),
            },
            {
                visible_file: 8_000,
                quarantined_file: 8_000,
            },
        )

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        assert audiobook_sets[0].source_path == tmp_path
        assert [track.src_path for track in audiobook_sets[0].tracks] == [visible_file]

    def test_group_files_by_audiobook(self, temp_audio_structure):
        """Test grouping files into audiobook sets."""
        scanner = AudioFileScanner()

        files = scanner._find_audio_files(temp_audio_structure)
        groups = scanner._group_files_by_audiobook(files)

        # Should have one group (all files in same CD1 directory)
        assert len(groups) == 1

        # Group should contain all 3 files
        group_files = list(groups.values())[0]
        assert len(group_files) == 3

    def test_disc_directories_collapse_into_one_audiobook(
        self, sample_audiobook_directory
    ):
        """CD1/CD2 folder structures should be grouped under one book."""
        scanner = AudioFileScanner(recursive=True, max_depth=4)

        files = scanner._find_audio_files(sample_audiobook_directory)
        groups = scanner._group_files_by_audiobook(files)

        roots = {group.name for group in groups}
        assert "Brandon Sanderson - The Way of Kings" in roots

    def test_scan_directory_splits_loose_files_by_album_and_keeps_asin_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        ex_file = tmp_path / "Freida McFadden - The Ex.m4b"
        goggins_file = tmp_path / "Can't Hurt Me....mp3"
        ex_file.touch()
        goggins_file.touch()

        self._patch_audio_metadata(
            scanner,
            monkeypatch,
            {
                ex_file: AudioTags(
                    album="The Ex",
                    artist="Freida McFadden",
                    asin="B0ABCDEFGH",
                ),
                goggins_file: AudioTags(
                    album="Can't Hurt Me",
                    artist="David Goggins",
                ),
            },
            {
                ex_file: 8_000,
                goggins_file: 8_100,
            },
        )

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 2

        by_title = {book.raw_title_guess: book for book in audiobook_sets}
        assert set(by_title) == {"The Ex", "Can't Hurt Me"}
        assert by_title["The Ex"].author_guess == "Freida McFadden"
        assert by_title["The Ex"].asin_guess == "B0ABCDEFGH"
        assert by_title["The Ex"].total_tracks == 1
        assert by_title["Can't Hurt Me"].author_guess == "David Goggins"
        assert by_title["Can't Hurt Me"].asin_guess is None
        assert by_title["Can't Hurt Me"].total_tracks == 1

    def test_group_files_by_audiobook_keeps_same_album_mp3s_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        files = []
        tag_map: dict[Path, AudioTags] = {}

        for track_number in range(1, 11):
            file_path = tmp_path / f"{track_number:02d} - Chapter.mp3"
            file_path.touch()
            files.append(file_path)
            tag_map[file_path] = AudioTags(album="Shared Album", artist="Author")

        self._patch_audio_metadata(scanner, monkeypatch, tag_map)

        groups = scanner._group_files_by_audiobook(files)

        assert len(groups) == 1
        assert list(groups.values())[0] == files

    def test_group_files_by_audiobook_keeps_partial_album_tags_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        files = []
        tag_map: dict[Path, AudioTags] = {}

        for track_number in range(1, 13):
            file_path = tmp_path / f"{track_number:02d} - Chapter.mp3"
            file_path.touch()
            files.append(file_path)
            if track_number <= 8:
                tag_map[file_path] = AudioTags(album="Mostly Tagged", artist="Author")
            else:
                tag_map[file_path] = AudioTags(artist="Author")

        self._patch_audio_metadata(scanner, monkeypatch, tag_map)

        groups = scanner._group_files_by_audiobook(files)

        assert len(groups) == 1
        assert list(groups.values())[0] == files

    def test_group_files_by_audiobook_splits_flat_untagged_m4bs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        files = []

        for title in ("Book One", "Book Two", "Book Three"):
            file_path = tmp_path / f"{title}.m4b"
            file_path.touch()
            files.append(file_path)

        self._patch_audio_metadata(scanner, monkeypatch, {})

        groups = scanner._group_files_by_audiobook(files)

        assert len(groups) == 3
        assert {tuple(group) for group in groups.values()} == {
            (files[0],),
            (files[1],),
            (files[2],),
        }

    def test_scan_directory_collapses_suffix_disc_folders_into_one_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=4)
        book_dir = tmp_path / "Author - Title"
        book_dir.mkdir()
        tag_map: dict[Path, AudioTags] = {}
        duration_map: dict[Path, float | None] = {}

        for disc_number in range(1, 16):
            disc_dir = book_dir / f"Title Disc {disc_number}"
            disc_dir.mkdir()
            track = disc_dir / "01 - Chapter.mp3"
            track.touch()
            tag_map[track] = AudioTags(
                album=f"Title Disc {disc_number}",
                artist="Author",
            )
            duration_map[track] = float(disc_number * 60)

        self._patch_audio_metadata(scanner, monkeypatch, tag_map, duration_map)

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        audiobook = audiobook_sets[0]
        assert audiobook.source_path == book_dir
        assert audiobook.raw_title_guess == "Title"
        assert audiobook.author_guess == "Author"
        assert audiobook.disc_count == 15
        assert [track.disc for track in audiobook.tracks] == list(range(1, 16))

    def test_scan_directory_collapses_noncontiguous_disc_folders_with_parent_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=4)
        book_dir = tmp_path / "On Combat - Dave Grossman"
        book_dir.mkdir()
        tag_map: dict[Path, AudioTags] = {}
        duration_map: dict[Path, float | None] = {}

        for disc_number in (1, 4):
            disc_dir = book_dir / f"On Combat Disc {disc_number}"
            disc_dir.mkdir()
            for track_number in (1, 2):
                track = disc_dir / f"{track_number:02d} - Chapter.mp3"
                track.touch()
                tag_map[track] = AudioTags(
                    album=f"On Combat Disc {disc_number}",
                    artist="Dave Grossman",
                )
                duration_map[track] = float(disc_number * 60 + track_number)

        self._patch_audio_metadata(scanner, monkeypatch, tag_map, duration_map)

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        audiobook = audiobook_sets[0]
        assert audiobook.source_path == book_dir
        assert audiobook.raw_title_guess == "On Combat"
        assert audiobook.author_guess == "Dave Grossman"
        assert audiobook.disc_count == 2
        assert [track.disc for track in audiobook.tracks] == [1, 1, 4, 4]
        assert [track.track_index for track in audiobook.tracks] == [1, 2, 1, 2]
        assert not any("has no tracks" in warning for warning in audiobook.warnings)

    def test_single_file_uses_parent_author_when_dash_split_author_is_implausible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=3)
        book_dir = tmp_path / "Donald J. Sobol - Encyclopedia Brown"
        book_dir.mkdir()
        book_file = book_dir / "EB 01 - Encyclopedia Brown and the Case.m4b"
        book_file.touch()

        self._patch_audio_metadata(
            scanner,
            monkeypatch,
            {book_file: AudioTags()},
            {book_file: 8_000},
        )

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        assert audiobook_sets[0].author_guess == "Donald J. Sobol"

    def test_single_file_does_not_warn_about_track_gaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        book_file = tmp_path / "Single Book.m4b"
        book_file.touch()

        self._patch_audio_metadata(
            scanner,
            monkeypatch,
            {book_file: AudioTags(track=14)},
            {book_file: 8_000},
        )

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        assert not any(
            "gaps in track numbering" in warning
            for warning in audiobook_sets[0].warnings
        )

    def test_multi_track_sets_still_warn_about_track_gaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = AudioFileScanner(recursive=True, max_depth=2)
        book_dir = tmp_path / "Gap Book"
        book_dir.mkdir()
        track_one = book_dir / "01 - Chapter.mp3"
        track_three = book_dir / "03 - Chapter.mp3"
        track_one.touch()
        track_three.touch()

        self._patch_audio_metadata(
            scanner,
            monkeypatch,
            {
                track_one: AudioTags(track=1),
                track_three: AudioTags(track=3),
            },
            {
                track_one: 120.0,
                track_three: 120.0,
            },
        )

        audiobook_sets = scanner.scan_directory(tmp_path)

        assert len(audiobook_sets) == 1
        assert any(
            "gaps in track numbering" in warning
            for warning in audiobook_sets[0].warnings
        )
