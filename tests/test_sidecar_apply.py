"""Tests for post-apply sidecar writes and undo behavior."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from bookbot.core.models import (
    AudioFormat,
    AudiobookSet,
    ProviderIdentity,
    RenameOperation,
    RenamePlan,
    Track,
    TrackStatus,
)
from bookbot.core.operations import TransactionManager


def _make_track(path: Path) -> Track:
    return Track(
        src_path=path,
        disc=1,
        track_index=1,
        file_size=0,
        audio_format=AudioFormat.MP3,
        status=TrackStatus.VALID,
    )


def _make_identity() -> ProviderIdentity:
    return ProviderIdentity(
        provider="audnexus",
        external_id="clean-title",
        title="Clean Title",
        authors=["Author Name"],
        series_name="Series Name",
        series_index="1",
        year=2020,
        narrator="Narrator Name",
        isbn_13="9781234567890",
        asin="B003TEST01",
        description="A matched audiobook.",
        cover_urls=["https://example.com/cover.jpg"],
    )


def test_execute_plan_writes_sidecars_and_undo_removes_them(
    tmp_path: Path, config_manager, monkeypatch
) -> None:
    config = config_manager.load_config()
    config.output.write_cover = True
    config.output.write_metadata_json = True
    config.output.write_metadata_opf = True
    config.output.write_nfo = True
    config_manager.save_config(config)

    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    source_track = source_dir / "01.mp3"
    source_track.write_bytes(b"audio")
    destination_track = tmp_path / "organized" / "01.mp3"

    track = _make_track(source_track)
    audiobook_set = AudiobookSet(
        source_path=source_dir,
        raw_title_guess="Rough Book",
        total_tracks=1,
        tracks=[track],
        chosen_identity=_make_identity(),
    )
    plan = RenamePlan(
        plan_id="sidecar-apply",
        created_at=datetime.now(),
        source_path=tmp_path,
        audiobook_sets=[audiobook_set],
        operations=[
            RenameOperation(
                old_path=source_track,
                new_path=destination_track,
                track=track,
            )
        ],
        dry_run=True,
    )

    monkeypatch.setattr(
        TransactionManager,
        "_download_cover_bytes",
        lambda self, url, timeout: b"cover-bytes",
    )

    manager = TransactionManager(config_manager)

    assert manager.execute_plan(plan, dry_run=False) is True
    assert destination_track.exists()
    assert (destination_track.parent / "metadata.json").exists()
    assert (destination_track.parent / "metadata.opf").exists()
    assert (destination_track.parent / "audiobook.nfo").exists()
    assert (destination_track.parent / "cover.jpg").exists()
    assert plan.applied_transaction_id is not None

    assert manager.undo_transaction(plan.applied_transaction_id) is True
    assert source_track.exists()
    assert not (destination_track.parent / "metadata.json").exists()
    assert not (destination_track.parent / "metadata.opf").exists()
    assert not (destination_track.parent / "audiobook.nfo").exists()
    assert not (destination_track.parent / "cover.jpg").exists()


def test_execute_plan_backs_up_existing_sidecar_and_undo_restores_it(
    tmp_path: Path, config_manager
) -> None:
    config = config_manager.load_config()
    config.output.write_cover = False
    config.output.write_metadata_json = True
    config.output.write_metadata_opf = False
    config.output.write_nfo = False
    config_manager.save_config(config)

    book_dir = tmp_path / "book"
    book_dir.mkdir()
    track_path = book_dir / "01.mp3"
    track_path.write_bytes(b"audio")
    metadata_path = book_dir / "metadata.json"
    original_metadata = '{"title": "Original"}\n'
    metadata_path.write_text(original_metadata, encoding="utf-8")

    track = _make_track(track_path)
    audiobook_set = AudiobookSet(
        source_path=book_dir,
        raw_title_guess="Rough Book",
        total_tracks=1,
        tracks=[track],
        chosen_identity=_make_identity(),
    )
    plan = RenamePlan(
        plan_id="sidecar-backup",
        created_at=datetime.now(),
        source_path=tmp_path,
        audiobook_sets=[audiobook_set],
        operations=[],
        planned_sidecars=[metadata_path],
        dry_run=True,
    )

    manager = TransactionManager(config_manager)

    assert manager.execute_plan(plan, dry_run=False) is True
    backup_path = metadata_path.with_name("metadata.json.bak")
    assert backup_path.exists()
    assert "Clean Title" in metadata_path.read_text(encoding="utf-8")
    assert plan.applied_transaction_id is not None

    assert manager.undo_transaction(plan.applied_transaction_id) is True
    assert metadata_path.read_text(encoding="utf-8") == original_metadata
    assert not backup_path.exists()
