"""Pytest configuration and fixtures."""

import tempfile
from pathlib import Path

import pytest

from bookbot.config.manager import ConfigManager


@pytest.fixture
def temp_config_dir():
    """Provide a temporary configuration directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def config_manager(temp_config_dir):
    """Provide a ConfigManager with temporary directory."""
    return ConfigManager(temp_config_dir)


@pytest.fixture
def sample_audiobook_directory():
    """Create a sample audiobook directory structure."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create a multi-disc audiobook
        book_dir = temp_path / "Brandon Sanderson - The Way of Kings"
        book_dir.mkdir()

        # Disc 1
        disc1_dir = book_dir / "CD1"
        disc1_dir.mkdir()

        disc1_files = [
            "01 - Prologue.mp3",
            "02 - Chapter 1 - Honor is Dead.mp3",
            "03 - Chapter 2 - The Way of Kings.mp3",
        ]

        for filename in disc1_files:
            (disc1_dir / filename).touch()

        # Disc 2
        disc2_dir = book_dir / "CD2"
        disc2_dir.mkdir()

        disc2_files = [
            "01 - Chapter 3 - City of Bells.mp3",
            "02 - Chapter 4 - The Shattered Plains.mp3",
        ]

        for filename in disc2_files:
            (disc2_dir / filename).touch()

        # Single file audiobook
        single_book_dir = temp_path / "Ready Player One"
        single_book_dir.mkdir()
        (single_book_dir / "Ready Player One.m4b").touch()

        # Mixed naming audiobook
        mixed_dir = temp_path / "Dune"
        mixed_dir.mkdir()

        mixed_files = [
            "Dune_Part01_Track001.flac",
            "Dune_Part01_Track002.flac",
            "Dune_Part02_Track001.flac",
        ]

        for filename in mixed_files:
            (mixed_dir / filename).touch()

        yield temp_path


@pytest.fixture(scope="session")
def mock_ffmpeg():
    """Mock FFmpeg for testing conversion functionality."""

    class MockFFmpeg:
        def __init__(self):
            self.available = True

        def probe_file(self, file_path):
            return {
                "format": {"duration": "3600.0"},
                "streams": [
                    {"codec_type": "audio", "codec_name": "mp3", "duration": "3600.0"}
                ],
            }

        def get_duration(self, file_path):
            return 3600.0

        def can_stream_copy(self, file_path):
            return file_path.suffix.lower() == ".aac"

        def convert_to_aac(self, input_path, output_path, **kwargs):
            # Mock conversion by copying file
            import shutil

            try:
                shutil.copy2(input_path, output_path)
                return True
            except Exception:
                return False

        def concatenate_files(self, input_files, output_path, **kwargs):
            # Mock concatenation
            output_path.touch()
            return True

        def add_chapters(self, file_path, chapters):
            return True

        def embed_cover_art(self, file_path, cover_path):
            return True

        def set_metadata(self, file_path, metadata):
            return True

    return MockFFmpeg()


@pytest.fixture
def mock_openlibrary_response():
    """Mock Open Library API response."""
    return {
        "docs": [
            {
                "key": "/works/OL12345W",
                "title": "The Way of Kings",
                "author_name": ["Brandon Sanderson"],
                "first_publish_year": 2010,
                "isbn": ["9780765326355", "0765326353"],
                "cover_i": 12345,
                "publisher": ["Tor Books"],
                "language": ["eng"],
            }
        ]
    }
