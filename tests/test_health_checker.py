"""Tests for the library health checker."""

import tempfile
from pathlib import Path

import pytest

from bookbot.core.health import HealthReport, LibraryHealthChecker
from bookbot.core.models import AudiobookSet, AudioFormat, AudioTags, Track


def _make_track(path: Path, fmt: str = "mp3", bitrate: int = 128, tags: AudioTags | None = None) -> Track:
    return Track(
        src_path=path,
        track_index=1,
        audio_format=AudioFormat(fmt),
        bitrate=bitrate,
        duration=300.0,
        existing_tags=tags or AudioTags(),
    )


def _make_set(
    path: Path,
    title: str = "Test Book",
    author: str | None = None,
    tracks: list[Track] | None = None,
    series: str | None = None,
    volume: str | None = None,
) -> AudiobookSet:
    return AudiobookSet(
        source_path=path,
        raw_title_guess=title,
        author_guess=author,
        series_guess=series,
        volume_guess=volume,
        tracks=tracks or [],
        total_tracks=len(tracks) if tracks else 0,
    )


@pytest.fixture
def checker():
    return LibraryHealthChecker()


class TestMissingCovers:
    def test_detects_missing_cover(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            book_dir = Path(tmp) / "book1"
            book_dir.mkdir()
            (book_dir / "track.mp3").touch()

            ab = _make_set(book_dir, "Book Without Cover")
            issues = checker.check_missing_covers([ab])
            assert len(issues) == 1
            assert issues[0]["title"] == "Book Without Cover"

    def test_no_issue_with_cover(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            book_dir = Path(tmp) / "book1"
            book_dir.mkdir()
            (book_dir / "cover.jpg").touch()

            ab = _make_set(book_dir, "Book With Cover")
            issues = checker.check_missing_covers([ab])
            assert len(issues) == 0


class TestInconsistentTags:
    def test_detects_mismatch(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            t1 = _make_track(
                path / "t1.mp3", tags=AudioTags(album="Album A", artist="Artist 1")
            )
            t2 = _make_track(
                path / "t2.mp3", tags=AudioTags(album="Album B", artist="Artist 1")
            )
            ab = _make_set(path, tracks=[t1, t2])
            issues = checker.check_inconsistent_tags([ab])
            assert len(issues) == 1

    def test_no_issue_consistent(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            t1 = _make_track(
                path / "t1.mp3", tags=AudioTags(album="Same", artist="Same")
            )
            t2 = _make_track(
                path / "t2.mp3", tags=AudioTags(album="Same", artist="Same")
            )
            ab = _make_set(path, tracks=[t1, t2])
            issues = checker.check_inconsistent_tags([ab])
            assert len(issues) == 0


class TestOrphanedFiles:
    def test_finds_orphans(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "track.mp3").touch()
            (path / "notes.doc").touch()
            (path / "random.pdf").touch()
            (path / "cover.jpg").touch()  # not orphaned
            (path / "metadata.nfo").touch()  # not orphaned

            orphans = checker.check_orphaned_files(path)
            names = {p.name for p in orphans}
            assert "notes.doc" in names
            assert "random.pdf" in names
            assert "cover.jpg" not in names
            assert "metadata.nfo" not in names
            assert "track.mp3" not in names


class TestDuplicateEditions:
    def test_detects_duplicates(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            ab1 = _make_set(Path(tmp) / "a", "The Hobbit", "J.R.R. Tolkien")
            ab2 = _make_set(Path(tmp) / "b", "The Hobbit", "JRR Tolkien")
            dupes = checker.check_duplicate_editions([ab1, ab2])
            assert len(dupes) == 1
            assert len(dupes[0]) == 2

    def test_no_duplicates(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            ab1 = _make_set(Path(tmp) / "a", "The Hobbit", "Tolkien")
            ab2 = _make_set(Path(tmp) / "b", "Dune", "Herbert")
            dupes = checker.check_duplicate_editions([ab1, ab2])
            assert len(dupes) == 0


class TestSeriesGaps:
    def test_detects_gap(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            ab1 = _make_set(
                Path(tmp) / "a", "Book 1", series="My Series", volume="1"
            )
            ab3 = _make_set(
                Path(tmp) / "b", "Book 3", series="My Series", volume="3"
            )
            gaps = checker.check_series_gaps([ab1, ab3])
            assert len(gaps) == 1
            assert 2 in gaps[0]["missing_volumes"]

    def test_no_gap(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            ab1 = _make_set(
                Path(tmp) / "a", "Book 1", series="Series", volume="1"
            )
            ab2 = _make_set(
                Path(tmp) / "b", "Book 2", series="Series", volume="2"
            )
            gaps = checker.check_series_gaps([ab1, ab2])
            assert len(gaps) == 0


class TestFormatConsistency:
    def test_detects_mixed(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            t1 = _make_track(path / "t1.mp3", fmt="mp3")
            t2 = _make_track(path / "t2.m4a", fmt="m4a")
            ab = _make_set(path, tracks=[t1, t2])
            issues = checker.check_format_consistency([ab])
            assert len(issues) == 1

    def test_no_issue_same_format(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            t1 = _make_track(path / "t1.mp3")
            t2 = _make_track(path / "t2.mp3")
            ab = _make_set(path, tracks=[t1, t2])
            issues = checker.check_format_consistency([ab])
            assert len(issues) == 0


class TestBitrateAnomalies:
    def test_detects_anomaly(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            t1 = _make_track(path / "t1.mp3", bitrate=128)
            t2 = _make_track(path / "t2.mp3", bitrate=128)
            t3 = _make_track(path / "t3.mp3", bitrate=320)  # outlier
            ab = _make_set(path, tracks=[t1, t2, t3])
            issues = checker.check_bitrate_anomalies([ab])
            assert len(issues) == 1


class TestHealthReport:
    def test_total_issues(self):
        report = HealthReport(
            missing_covers=[{"path": "a"}],
            inconsistent_tags=[{"path": "b"}, {"path": "c"}],
        )
        assert report.total_issues == 3

    def test_empty_report(self):
        report = HealthReport()
        assert report.total_issues == 0

    def test_to_summary(self):
        report = HealthReport(missing_covers=[{"path": "a"}])
        summary = report.to_summary()
        assert summary["missing_covers"] == 1
        assert summary["total"] == 1


class TestRunAllChecks:
    def test_full_check(self, checker):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            book_dir = path / "book"
            book_dir.mkdir()
            (book_dir / "track.mp3").touch()

            ab = _make_set(book_dir, "Test")
            report = checker.run_all_checks(path, [ab])
            assert isinstance(report, HealthReport)
