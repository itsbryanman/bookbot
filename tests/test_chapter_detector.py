"""Tests for chapter detection."""

import tempfile
from pathlib import Path

import pytest

from bookbot.chapters.detector import ChapterDetector
from bookbot.chapters.models import Chapter
from bookbot.chapters.writer import ChapterWriter
from bookbot.core.models import AudiobookSet, AudioFormat, AudioTags, Track


@pytest.fixture
def detector():
    return ChapterDetector()


@pytest.fixture
def writer():
    return ChapterWriter()


def _make_audiobook_set(num_tracks: int = 3) -> AudiobookSet:
    tracks = []
    for i in range(1, num_tracks + 1):
        tracks.append(
            Track(
                src_path=Path(f"/tmp/track{i:02d}.mp3"),
                track_index=i,
                audio_format=AudioFormat.MP3,
                duration=600.0,
                existing_tags=AudioTags(title=f"Chapter {i}"),
            )
        )
    return AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="Test Book",
        tracks=tracks,
        total_tracks=num_tracks,
    )


class TestDetectFromTracks:
    def test_basic(self, detector):
        ab = _make_audiobook_set(3)
        chapters = detector.detect_from_tracks(ab)
        assert len(chapters) == 3
        assert chapters[0].title == "Chapter 1"
        assert chapters[0].start_ms == 0
        assert chapters[0].source == "tracks"
        assert chapters[1].start_ms == 600000
        assert chapters[2].start_ms == 1200000

    def test_single_track(self, detector):
        ab = _make_audiobook_set(1)
        chapters = detector.detect_from_tracks(ab)
        assert len(chapters) == 1

    def test_no_tag_title(self, detector):
        ab = _make_audiobook_set(2)
        ab.tracks[0].existing_tags.title = None
        chapters = detector.detect_from_tracks(ab)
        assert chapters[0].title == "Chapter 1"


class TestDetectFromCue:
    def test_basic_cue(self, detector):
        with tempfile.TemporaryDirectory() as tmp:
            cue_path = Path(tmp) / "chapters.cue"
            cue_path.write_text(
                'FILE "audio.m4b" M4A\n'
                "  TRACK 01 AUDIO\n"
                '    TITLE "Prologue"\n'
                "    INDEX 01 00:00:00\n"
                "  TRACK 02 AUDIO\n"
                '    TITLE "Chapter One"\n'
                "    INDEX 01 05:30:00\n"
                "  TRACK 03 AUDIO\n"
                '    TITLE "Chapter Two"\n'
                "    INDEX 01 12:15:00\n"
            )
            chapters = detector.detect_from_cue(cue_path)

        assert len(chapters) == 3
        assert chapters[0].title == "Prologue"
        assert chapters[0].start_ms == 0
        assert chapters[1].title == "Chapter One"
        assert chapters[1].start_ms == 330000  # 5 minutes 30 seconds
        assert chapters[0].end_ms == chapters[1].start_ms
        assert chapters[0].source == "cue"

    def test_empty_cue(self, detector):
        with tempfile.TemporaryDirectory() as tmp:
            cue_path = Path(tmp) / "empty.cue"
            cue_path.write_text("")
            chapters = detector.detect_from_cue(cue_path)
        assert len(chapters) == 0

    def test_nonexistent_cue(self, detector):
        chapters = detector.detect_from_cue(Path("/nonexistent/file.cue"))
        assert len(chapters) == 0


class TestChapterModel:
    def test_basic(self):
        ch = Chapter(title="Test", start_ms=0, end_ms=60000, source="test")
        assert ch.title == "Test"
        assert ch.start_ms == 0
        assert ch.end_ms == 60000
        assert ch.source == "test"

    def test_defaults(self):
        ch = Chapter(title="Test", start_ms=0)
        assert ch.end_ms is None
        assert ch.source == "unknown"


class TestChapterWriter:
    def test_ffmetadata(self, writer):
        chapters = [
            Chapter(title="Prologue", start_ms=0, end_ms=60000, source="test"),
            Chapter(title="Chapter 1", start_ms=60000, end_ms=360000, source="test"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "chapters.txt"
            success = writer.write_to_ffmetadata(out_path, chapters)
            assert success
            assert out_path.exists()

            content = out_path.read_text()
            assert ";FFMETADATA1" in content
            assert "START=0" in content
            assert "END=60000" in content
            assert "title=Prologue" in content

    def test_cue(self, writer):
        chapters = [
            Chapter(title="Prologue", start_ms=0, end_ms=60000, source="test"),
            Chapter(title="Chapter 1", start_ms=60000, end_ms=360000, source="test"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "chapters.cue"
            success = writer.write_to_cue(out_path, chapters)
            assert success
            assert out_path.exists()

            content = out_path.read_text()
            assert "TRACK 01 AUDIO" in content
            assert 'TITLE "Prologue"' in content
            assert "INDEX 01 00:00:00" in content

    def test_empty_chapters(self, writer):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "empty.txt"
            assert not writer.write_to_ffmetadata(out_path, [])
            assert not writer.write_to_cue(out_path, [])
