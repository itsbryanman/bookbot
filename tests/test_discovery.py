"""Tests for audio file discovery functionality."""

import tempfile
from pathlib import Path

import pytest

from bookbot.core.discovery import AudioFileScanner


class TestAudioFileScanner:
    """Test cases for AudioFileScanner."""

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

        assert title == "Brandon Sanderson - The Way of Kings"
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
