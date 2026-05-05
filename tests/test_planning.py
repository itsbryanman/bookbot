"""Tests for declarative rename plan workflows."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudioFormat, RenameOperation, RenamePlan, Track
from bookbot.core.operations import TransactionManager
from bookbot.core.planning import PlanBuilder

FIXTURE_LIBRARY = Path(__file__).parent / "fixtures" / "messy_library"
GOLDEN_DIR = Path(__file__).parent / "golden"


def _build_plan(config_manager, profile_name: str | None = None) -> RenamePlan:
    scanner = AudioFileScanner(recursive=True, max_depth=8)
    audiobook_sets = scanner.scan_directory(FIXTURE_LIBRARY)
    if profile_name:
        config = config_manager.load_profile(profile_name).config
    else:
        config = config_manager.load_config()
    return PlanBuilder(config).create_plan(
        FIXTURE_LIBRARY,
        audiobook_sets,
        profile_name=profile_name,
        source_roots=[FIXTURE_LIBRARY],
    )


def _plan_summary(plan: RenamePlan) -> dict[str, object]:
    return {
        "profile_name": plan.profile_name,
        "source_path": "__FIXTURE_LIBRARY__",
        "warnings": plan.warnings,
        "conflicts": plan.conflicts,
        "operations": [
            {
                "old": str(operation.old_path.relative_to(FIXTURE_LIBRARY)),
                "new": str(operation.new_path.relative_to(FIXTURE_LIBRARY)),
            }
            for operation in plan.operations
        ],
    }


def test_same_input_always_creates_same_plan(config_manager) -> None:
    first = _plan_summary(_build_plan(config_manager, "audiobookshelf"))
    second = _plan_summary(_build_plan(config_manager, "audiobookshelf"))

    assert first == second


def test_audiobookshelf_plan_matches_golden(config_manager) -> None:
    plan = _plan_summary(_build_plan(config_manager, "audiobookshelf"))
    golden = json.loads((GOLDEN_DIR / "audiobookshelf_plan.json").read_text())

    assert plan == golden


def test_plex_plan_matches_golden(config_manager) -> None:
    plan = _plan_summary(_build_plan(config_manager, "plex"))
    golden = json.loads((GOLDEN_DIR / "plex_plan.json").read_text())

    assert plan == golden


def test_plan_rejects_path_traversal_and_illegal_filenames(tmp_path) -> None:
    source = tmp_path / "01 - Prelude.mp3"
    source.touch()
    track = Track(
        src_path=source,
        disc=1,
        track_index=1,
        audio_format=AudioFormat.MP3,
    )
    plan = RenamePlan(
        plan_id="unsafe",
        created_at=datetime(2024, 1, 1),
        source_path=tmp_path,
        operations=[
            RenameOperation(
                old_path=source,
                new_path=tmp_path.parent / "escape:bad.mp3",
                track=track,
            )
        ],
    )

    assert plan.validate_plan() is False
    assert any("escapes plan root" in conflict for conflict in plan.conflicts)
    assert any("Illegal filename characters" in conflict for conflict in plan.conflicts)


def test_plan_rejects_duplicate_destinations(tmp_path) -> None:
    source_a = tmp_path / "01 - A.mp3"
    source_b = tmp_path / "02 - B.mp3"
    source_a.touch()
    source_b.touch()

    target = tmp_path / "01 - Duplicate.mp3"
    track_a = Track(
        src_path=source_a,
        disc=1,
        track_index=1,
        audio_format=AudioFormat.MP3,
    )
    track_b = Track(
        src_path=source_b,
        disc=1,
        track_index=2,
        audio_format=AudioFormat.MP3,
    )
    plan = RenamePlan(
        plan_id="duplicate",
        created_at=datetime(2024, 1, 1),
        source_path=tmp_path,
        operations=[
            RenameOperation(old_path=source_a, new_path=target, track=track_a),
            RenameOperation(old_path=source_b, new_path=target, track=track_b),
        ],
    )

    assert plan.validate_plan() is False
    assert any("Duplicate target path" in conflict for conflict in plan.conflicts)


def test_undo_restores_original_state(config_manager, tmp_path) -> None:
    source_a = tmp_path / "01 - A.mp3"
    source_b = tmp_path / "02 - B.mp3"
    source_a.touch()
    source_b.touch()

    track_a = Track(
        src_path=source_a,
        disc=1,
        track_index=1,
        audio_format=AudioFormat.MP3,
    )
    track_b = Track(
        src_path=source_b,
        disc=1,
        track_index=2,
        audio_format=AudioFormat.MP3,
    )
    plan = RenamePlan(
        plan_id="undoable",
        created_at=datetime(2024, 1, 1),
        source_path=tmp_path,
        operations=[
            RenameOperation(
                old_path=source_a,
                new_path=tmp_path / "Renamed" / "01 - A.mp3",
                track=track_a,
            ),
            RenameOperation(
                old_path=source_b,
                new_path=tmp_path / "Renamed" / "02 - B.mp3",
                track=track_b,
            ),
        ],
    )

    manager = TransactionManager(config_manager)

    assert manager.execute_plan(plan, dry_run=False) is True
    assert plan.applied_transaction_id is not None
    assert (tmp_path / "Renamed" / "01 - A.mp3").exists()
    assert (tmp_path / "Renamed" / "02 - B.mp3").exists()

    assert manager.undo_transaction(plan.applied_transaction_id) is True
    assert source_a.exists()
    assert source_b.exists()
