"""Tests for Phase 4: deduplication engine and quarantine workflow."""

import json
from pathlib import Path

import pytest

from bookbot.core.dedupe import DedupeEngine, DedupePlan, QuarantineOp
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    ProviderIdentity,
    Track,
    TrackStatus,
)

# ── Helpers ──


def _make_track(
    path: Path,
    fmt: AudioFormat = AudioFormat.MP3,
    index: int = 1,
    bitrate: int = 128,
    duration: float = 300.0,
) -> Track:
    return Track(
        src_path=path,
        track_index=index,
        audio_format=fmt,
        bitrate=bitrate,
        duration=duration,
        file_size=path.stat().st_size if path.exists() else 1000,
        status=TrackStatus.VALID,
    )


def _make_audiobook(
    source_path: Path,
    title: str,
    author: str = "",
    fmt: AudioFormat = AudioFormat.MP3,
    track_count: int = 1,
    bitrate: int = 128,
    duration: float = 300.0,
    has_identity: bool = False,
) -> AudiobookSet:
    tracks = []
    total_dur = 0.0
    for i in range(track_count):
        tp = source_path / f"track{i + 1}.{fmt.value}"
        if not tp.exists():
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_bytes(b"\x00" * 100)
        t = _make_track(tp, fmt=fmt, index=i + 1, bitrate=bitrate, duration=duration)
        tracks.append(t)
        total_dur += duration

    ab = AudiobookSet(
        source_path=source_path,
        raw_title_guess=title,
        author_guess=author or None,
        tracks=tracks,
        total_tracks=len(tracks),
        total_duration=total_dur,
    )

    if has_identity:
        ab.chosen_identity = ProviderIdentity(
            provider="test",
            external_id="x",
            title=title,
            authors=[author] if author else [],
            isbn_13="9780451524935",
        )

    return ab


# ── Fuzzy edition clustering ──


class TestEditionClustering:
    def test_exact_match_grouped(self, tmp_path: Path) -> None:
        d1 = tmp_path / "The Stand"
        d2 = tmp_path / "The Stand (Unabridged)"
        d1.mkdir()
        d2.mkdir()

        ab1 = _make_audiobook(d1, "The Stand", "Stephen King")
        ab2 = _make_audiobook(d2, "The Stand (Unabridged)", "Stephen King")

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab1, ab2])

        # Should cluster into one group
        assert len(groups) == 1
        assert len(groups[0].members) == 2

    def test_article_reorder_grouped(self, tmp_path: Path) -> None:
        """'The Stand' and 'Stand, The (Unabridged)' should cluster."""
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab1 = _make_audiobook(d1, "The Stand", "Stephen King")
        ab2 = _make_audiobook(d2, "Stand, The (Unabridged)", "Stephen King")

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab1, ab2])

        assert len(groups) == 1

    def test_different_books_not_grouped(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab1 = _make_audiobook(d1, "The Stand", "Stephen King")
        ab2 = _make_audiobook(d2, "It", "Stephen King")

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab1, ab2])

        assert len(groups) == 0


# ── Edition scoring / keeper selection ──


class TestEditionScoring:
    def test_m4b_beats_mp3(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab_m4b = _make_audiobook(d1, "The Stand", "SK", fmt=AudioFormat.M4B)
        ab_mp3 = _make_audiobook(d2, "The Stand", "SK", fmt=AudioFormat.MP3)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab_mp3, ab_m4b])

        assert len(groups) == 1
        assert groups[0].keeper is not None
        assert groups[0].keeper.audiobook_set.source_path == d1

    def test_identity_with_isbn_wins(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab_with_id = _make_audiobook(
            d1, "The Stand", "SK", has_identity=True
        )
        ab_no_id = _make_audiobook(d2, "The Stand", "SK")

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab_no_id, ab_with_id])

        assert len(groups) == 1
        assert groups[0].keeper is not None
        assert groups[0].keeper.audiobook_set.source_path == d1

    def test_higher_bitrate_wins(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab_hi = _make_audiobook(d1, "The Stand", "SK", bitrate=320)
        ab_lo = _make_audiobook(d2, "The Stand", "SK", bitrate=64)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab_lo, ab_hi])

        assert len(groups) == 1
        assert groups[0].keeper is not None
        assert groups[0].keeper.audiobook_set.source_path == d1

    def test_longer_duration_wins(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()

        ab_long = _make_audiobook(d1, "The Stand", "SK", duration=36000.0)
        ab_short = _make_audiobook(d2, "The Stand", "SK", duration=18000.0)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions([ab_short, ab_long])

        assert len(groups) == 1
        assert groups[0].keeper is not None
        assert groups[0].keeper.audiobook_set.source_path == d1


# ── Staged hashing / byte dedup ──


class TestStagedHashing:
    def test_finds_byte_duplicates(self, tmp_path: Path) -> None:
        """Two identical files should be grouped."""
        content = b"\xff" * 200
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        (d1 / "track.mp3").write_bytes(content)
        (d2 / "track.mp3").write_bytes(content)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_files()

        assert len(groups) == 1
        assert len(groups[0].paths) == 2

    def test_different_files_not_grouped(self, tmp_path: Path) -> None:
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        (d1 / "track.mp3").write_bytes(b"\x00" * 200)
        (d2 / "track.mp3").write_bytes(b"\xff" * 200)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_files()

        assert len(groups) == 0

    def test_skips_full_hash_when_partial_differs(self, tmp_path: Path) -> None:
        """Files with same size but different content diverge at partial hash."""
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        # Same size, different content
        (d1 / "track.mp3").write_bytes(b"\x00" * 500)
        (d2 / "track.mp3").write_bytes(b"\xff" * 500)

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_files()
        assert len(groups) == 0


# ── Plan quarantine and undo ──


class TestPlanAndUndo:
    def test_quarantine_and_restore(self, tmp_path: Path) -> None:
        """Plan quarantines files; undo restores them to original paths."""
        d1 = tmp_path / "lib" / "a"
        d2 = tmp_path / "lib" / "b"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        content = b"identical_content_here"
        f1 = d1 / "track.mp3"
        f2 = d2 / "track.mp3"
        f1.write_bytes(content)
        f2.write_bytes(content)

        engine = DedupeEngine(tmp_path / "lib")
        groups = engine.analyze_files()
        assert len(groups) == 1

        plan = engine.build_plan(file_groups=groups)
        assert len(plan.operations) == 1

        # Execute
        engine.execute_plan(plan)

        # One file should be quarantined
        quarantined = plan.operations[0]
        assert quarantined.destination.exists()
        assert not quarantined.source.exists()

        # The keeper should still exist
        keeper = groups[0].keeper
        assert keeper is not None
        assert keeper.exists()

        # Undo: read transaction log and reverse
        log_dir = tmp_path / "lib" / ".bookbot-quarantine" / plan.plan_id
        log_file = log_dir / f"transaction_{plan.plan_id}.json"
        assert log_file.exists()

        log_data = json.loads(log_file.read_text())
        for op in reversed(log_data["operations"]):
            old = Path(op["old_path"])
            new = Path(op["new_path"])
            if new.exists():
                old.parent.mkdir(parents=True, exist_ok=True)
                new.rename(old)

        # Both files should be back
        assert f1.exists()
        assert f2.exists()
        assert f1.read_bytes() == content
        assert f2.read_bytes() == content

    def test_apply_refused_if_conflicts(self, tmp_path: Path) -> None:
        """Execution should fail if destinations already exist."""
        d1 = tmp_path / "lib" / "a"
        d1.mkdir(parents=True)
        f1 = d1 / "track.mp3"
        f1.write_bytes(b"data")

        engine = DedupeEngine(tmp_path / "lib")

        # Manually create a plan with a conflicting destination
        dest = tmp_path / "lib" / ".bookbot-quarantine" / "test" / "a" / "track.mp3"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"conflict")

        plan = DedupePlan(
            plan_id="test",
            created_at="2024-01-01T00:00:00",
            library_root=str(tmp_path / "lib"),
            quarantine_root=str(tmp_path / "lib" / ".bookbot-quarantine" / "test"),
            operations=[QuarantineOp(source=f1, destination=dest, reason="test")],
        )

        assert plan.has_conflicts()
        with pytest.raises(ValueError, match="conflicts"):
            engine.execute_plan(plan)

    def test_dry_run_touches_no_files(self, tmp_path: Path) -> None:
        """Dry-run (just building the plan) should not modify any files."""
        d1 = tmp_path / "lib" / "a"
        d2 = tmp_path / "lib" / "b"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        content = b"same_bytes"
        (d1 / "track.mp3").write_bytes(content)
        (d2 / "track.mp3").write_bytes(content)

        engine = DedupeEngine(tmp_path / "lib")
        groups = engine.analyze_files()
        plan = engine.build_plan(file_groups=groups)

        # Plan exists but no execution
        assert len(plan.operations) == 1
        # Both files still exist
        assert (d1 / "track.mp3").exists()
        assert (d2 / "track.mp3").exists()
