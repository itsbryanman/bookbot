"""Tests for Phase 4: deduplication engine and quarantine workflow."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.core.dedupe import (
    DedupeCandidate,
    DedupeEngine,
    DedupePlan,
    EditionGroup,
    FileGroup,
    QuarantineOp,
)
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    OperationRecord,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.operations import TransactionManager

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

    def test_segmented_sibling_disc_folders_are_not_grouped_as_editions(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "On Combat"
        parent.mkdir()

        segments = []
        for disc_number in range(1, 5):
            disc_dir = parent / f"On Combat Disc {disc_number}"
            disc_dir.mkdir()
            segments.append(
                _make_audiobook(
                    disc_dir,
                    "On Combat",
                    "Dave Grossman",
                    duration=3600.0,
                )
            )

        engine = DedupeEngine(tmp_path)
        groups = engine.analyze_editions(segments)

        assert groups == []
        assert any(
            "uncollapsed disc folders" in warning
            for warning in engine.analysis_warnings
        )


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

    def test_analyze_files_ignores_quarantine_tree(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        visible_a = lib / "visible-a"
        visible_b = lib / "visible-b"
        visible_a.mkdir(parents=True)
        visible_b.mkdir(parents=True)
        (visible_a / "track.mp3").write_bytes(b"same-visible")
        (visible_b / "track.mp3").write_bytes(b"same-visible")

        quarantined_a = lib / ".bookbot-quarantine" / "tx-1" / "hidden-a"
        quarantined_b = lib / ".bookbot-quarantine" / "tx-1" / "hidden-b"
        quarantined_a.mkdir(parents=True)
        quarantined_b.mkdir(parents=True)
        (quarantined_a / "track.mp3").write_bytes(b"same-hidden")
        (quarantined_b / "track.mp3").write_bytes(b"same-hidden")

        engine = DedupeEngine(lib)
        groups = engine.analyze_files()

        assert len(groups) == 1
        assert len(groups[0].paths) == 2
        assert all(".bookbot-quarantine" not in path.parts for path in groups[0].paths)

        plan = engine.build_plan(file_groups=groups)
        assert len(plan.operations) == 1
        assert all(
            ".bookbot-quarantine" not in op.source.parts for op in plan.operations
        )

    def test_finds_duplicate_cover_images_within_one_book_root(
        self, tmp_path: Path
    ) -> None:
        lib = tmp_path / "lib"
        book_dir = lib / "Donald J. Sobol - Encyclopedia Brown"
        book_dir.mkdir(parents=True)
        cover_a = book_dir / "EB 01 - cover-a.jpg"
        cover_b = book_dir / "EB 01 - cover-b.jpg"
        cover_a.write_bytes(b"same-cover")
        cover_b.write_bytes(b"same-cover")

        engine = DedupeEngine(lib)
        groups = engine.analyze_files()

        assert len(groups) == 1
        assert groups[0].paths == [cover_a, cover_b]

        plan = engine.build_plan(file_groups=groups)

        assert len(plan.operations) == 1
        assert plan.operations[0].source == cover_b


# ── Plan quarantine and undo ──


class TestPlanAndUndo:
    def test_quarantine_and_restore(
        self, tmp_path: Path, config_manager
    ) -> None:
        """Plan quarantines files; undo restores them via the shared log dir."""
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
        original_bytes = {
            f1: f1.read_bytes(),
            f2: f2.read_bytes(),
        }

        # Execute
        engine.execute_plan(plan, config_manager)

        # One file should be quarantined
        quarantined = plan.operations[0]
        assert quarantined.destination.exists()
        assert not quarantined.source.exists()

        # The keeper should still exist
        keeper = groups[0].keeper
        assert keeper is not None
        assert keeper.exists()

        log_file = config_manager.get_log_dir() / f"transaction_{plan.plan_id}.json"
        provenance_copy = (
            Path(plan.quarantine_root) / f"transaction_{plan.plan_id}.json"
        )
        assert log_file.exists()
        assert provenance_copy.exists()

        history = TransactionManager(config_manager).list_transactions(days=365)
        transaction = next(item for item in history if item["id"] == plan.plan_id)
        assert transaction["transaction_type"] == "dedupe"

        manager = TransactionManager(config_manager)
        assert manager.undo_transaction(plan.plan_id) is True

        # Both files should be back byte-identically.
        assert f1.exists()
        assert f2.exists()
        assert f1.read_bytes() == original_bytes[f1]
        assert f2.read_bytes() == original_bytes[f2]
        assert not Path(plan.quarantine_root).exists()
        assert log_file.with_suffix(".undone").exists()

    def test_undo_falls_back_to_legacy_quarantine_log(
        self,
        tmp_path: Path,
        config_manager,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Undo can recover old dedupe logs that only exist in quarantine."""
        lib = tmp_path / "lib"
        d1 = lib / "a"
        d2 = lib / "b"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)

        f1 = d1 / "track.mp3"
        f2 = d2 / "track.mp3"
        f1.write_bytes(b"same")
        f2.write_bytes(b"same")

        engine = DedupeEngine(lib)
        plan = engine.build_plan(file_groups=engine.analyze_files())
        engine.execute_plan(plan, config_manager)

        standard_log = config_manager.get_log_dir() / f"transaction_{plan.plan_id}.json"
        legacy_log = Path(plan.quarantine_root) / f"transaction_{plan.plan_id}.json"
        assert standard_log.exists()
        assert legacy_log.exists()

        standard_log.unlink()
        monkeypatch.chdir(lib)

        manager = TransactionManager(config_manager)
        assert manager.undo_transaction(plan.plan_id) is True

        captured = capsys.readouterr()
        assert "Notice: using legacy dedupe transaction log" in captured.out
        assert standard_log.with_suffix(".undone").exists()
        assert f1.exists()
        assert f2.exists()
        assert not Path(plan.quarantine_root).exists()

    def test_apply_refused_if_conflicts(
        self, tmp_path: Path, config_manager
    ) -> None:
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
            engine.execute_plan(plan, config_manager)

    def test_execute_plan_rejects_quarantine_sources(
        self, tmp_path: Path, config_manager
    ) -> None:
        lib = tmp_path / "lib"
        quarantined_source = (
            lib / ".bookbot-quarantine" / "old-tx" / "nested" / "track.mp3"
        )
        quarantined_source.parent.mkdir(parents=True)
        quarantined_source.write_bytes(b"already quarantined")

        plan = DedupePlan(
            plan_id="bad-plan",
            created_at="2026-07-22T00:00:00",
            library_root=str(lib),
            quarantine_root=str(lib / ".bookbot-quarantine" / "new-tx"),
            operations=[
                QuarantineOp(
                    source=quarantined_source,
                    destination=(
                        lib
                        / ".bookbot-quarantine"
                        / "new-tx"
                        / ".bookbot-quarantine"
                        / "old-tx"
                        / "nested"
                        / "track.mp3"
                    ),
                    reason="should be rejected",
                )
            ],
        )

        engine = DedupeEngine(lib)

        with pytest.raises(ValueError, match=r"\.bookbot-quarantine"):
            engine.execute_plan(plan, config_manager)

        assert quarantined_source.exists()

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

    def test_file_keeper_prefers_nested_original_over_root_copy(
        self, tmp_path: Path
    ) -> None:
        lib = tmp_path / "lib"
        nested_dir = lib / "Author - Title" / "Disc 1"
        nested_dir.mkdir(parents=True)

        original = nested_dir / "track.mp3"
        stray_copy = lib / "track.mp3"
        original.write_bytes(b"same-audio")
        stray_copy.write_bytes(b"same-audio")

        engine = DedupeEngine(lib)
        groups = engine.analyze_files()
        assert len(groups) == 1

        plan = engine.build_plan(file_groups=groups)

        assert groups[0].keeper == original
        assert plan.file_groups[0]["keeper"] == str(original)
        assert [op.source for op in plan.operations] == [stray_copy]

    def test_file_keeper_equal_depth_uses_existing_deterministic_tiebreak(
        self, tmp_path: Path
    ) -> None:
        lib = tmp_path / "lib"
        alpha_dir = lib / "alpha"
        bravo_dir = lib / "bravo"
        alpha_dir.mkdir(parents=True)
        bravo_dir.mkdir()

        alpha = alpha_dir / "track.mp3"
        bravo = bravo_dir / "track.mp3"
        alpha.write_bytes(b"same-audio")
        bravo.write_bytes(b"same-audio")

        engine = DedupeEngine(lib)

        assert engine._pick_file_keeper([bravo, alpha], set()) == alpha

    def test_overlapping_edition_and_file_groups_do_not_double_quarantine(
        self, tmp_path: Path, config_manager
    ) -> None:
        lib = tmp_path / "lib"
        keeper_dir = lib / "keeper"
        duplicate_dir = lib / "duplicate"
        keeper_dir.mkdir(parents=True)
        duplicate_dir.mkdir()

        keeper_ab = _make_audiobook(keeper_dir, "The Stand", "Stephen King")
        duplicate_ab = _make_audiobook(
            duplicate_dir,
            "The Stand (Unabridged)",
            "Stephen King",
        )
        duplicate_ab.tracks[0].src_path.write_bytes(keeper_ab.tracks[0].src_path.read_bytes())

        edition_group = EditionGroup(
            members=[
                DedupeCandidate(audiobook_set=keeper_ab, is_keeper=True),
                DedupeCandidate(
                    audiobook_set=duplicate_ab,
                    quarantine_reason="lower ranked duplicate",
                ),
            ],
            keeper=DedupeCandidate(audiobook_set=keeper_ab, is_keeper=True),
        )
        file_group = FileGroup(
            size=keeper_ab.tracks[0].file_size,
            paths=[
                keeper_ab.tracks[0].src_path,
                duplicate_ab.tracks[0].src_path,
            ],
        )

        engine = DedupeEngine(lib)
        plan = engine.build_plan(
            edition_groups=[edition_group],
            file_groups=[file_group],
            keeper_edition_paths={keeper_ab.source_path},
        )

        duplicate_ops = [
            op for op in plan.operations if op.source == duplicate_ab.tracks[0].src_path
        ]
        assert len(duplicate_ops) == 1

        engine.execute_plan(plan, config_manager)
        assert duplicate_ab.tracks[0].src_path.exists() is False


def test_history_lists_rename_and_dedupe_transaction_types(
    tmp_path: Path, config_manager
) -> None:
    runner = CliRunner()
    manager = TransactionManager(config_manager)
    rename_id = "rename-history-test"
    dedupe_id = "dedupe-history-test"

    manager.record_transaction(
        rename_id,
        [
            OperationRecord(
                operation_id=rename_id,
                timestamp=datetime(2026, 7, 20, 12, 0, 0),
                operation_type="rename",
                old_path=tmp_path / "before.mp3",
                new_path=tmp_path / "after.mp3",
            )
        ],
        transaction_type="rename",
        timestamp="2026-07-20T12:00:00",
    )
    manager.record_transaction(
        dedupe_id,
        [
            OperationRecord(
                operation_id=dedupe_id,
                timestamp=datetime(2026, 7, 21, 12, 0, 0),
                operation_type="rename",
                old_path=tmp_path / "copy.mp3",
                new_path=tmp_path / ".bookbot-quarantine" / dedupe_id / "copy.mp3",
            )
        ],
        transaction_type="dedupe",
        timestamp="2026-07-21T12:00:00",
    )

    result = runner.invoke(
        cli,
        ["--config-dir", str(config_manager.config_dir), "history", "--days", "365"],
    )

    assert result.exit_code == 0
    assert f"{rename_id[:8]}... - 2026-07-20T12:00:00 - rename -" in result.output
    assert f"{dedupe_id[:8]}... - 2026-07-21T12:00:00 - dedupe -" in result.output


def test_dedupe_json_writes_empty_plan_when_no_duplicates(tmp_path: Path) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    config_dir = tmp_path / "config"
    unique_book = library / "Unique Book"
    json_path = tmp_path / "plans" / "dedupe-plan.json"

    unique_book.mkdir(parents=True)
    (unique_book / "track.mp3").write_bytes(b"unique-audio")

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "dedupe",
            str(library),
            "--json",
            str(json_path),
        ],
    )

    assert result.exit_code == 0
    assert "Plan written to" in result.output
    assert "No duplicates found." in result.output
    assert json_path.exists()

    plan_data = json.loads(json_path.read_text())
    assert plan_data["operations"] == []
    assert plan_data["edition_groups"] == []
    assert plan_data["file_groups"] == []
    assert plan_data["library_root"] == str(library)
