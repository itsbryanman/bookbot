"""Crash recovery and durable transaction-log behavior."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from bookbot.core.models import AudioFormat, RenameOperation, RenamePlan, Track
from bookbot.core.operations import TransactionManager


def _plan(tmp_path: Path) -> tuple[RenamePlan, Path, Path]:
    source = tmp_path / "01 - Chapter.mp3"
    target = tmp_path / "Book" / "01 - Chapter.mp3"
    source.write_bytes(b"audio")
    track = Track(
        src_path=source,
        track_index=1,
        audio_format=AudioFormat.MP3,
        file_size=5,
    )
    plan = RenamePlan(
        plan_id="durable",
        created_at=datetime(2026, 10, 13),
        source_path=tmp_path,
        operations=[RenameOperation(old_path=source, new_path=target, track=track)],
    )
    return plan, source, target


def test_log_failure_blocks_apply_and_restores_source(
    config_manager, tmp_path, monkeypatch
) -> None:
    plan, source, target = _plan(tmp_path)
    manager = TransactionManager(config_manager)
    real_write = manager._write_transaction_log
    writes = 0

    def fail_before_second_move(log_file, log_data):
        nonlocal writes
        writes += 1
        if writes == 3:
            return False
        return real_write(log_file, log_data)

    monkeypatch.setattr(manager, "_write_transaction_log", fail_before_second_move)

    with pytest.raises(RuntimeError, match="required undo log"):
        manager.execute_plan(plan)

    assert source.read_bytes() == b"audio"
    assert not target.exists()
    assert plan.applied_transaction_id is None


def test_interrupted_move_leaves_recoverable_write_ahead_journal(
    config_manager, tmp_path, monkeypatch
) -> None:
    plan, source, target = _plan(tmp_path)
    manager = TransactionManager(config_manager)
    original_rename = Path.rename

    def interrupt_rename(path, destination):
        original_rename(path, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "rename", interrupt_rename)

    with pytest.raises(KeyboardInterrupt):
        manager.execute_plan(plan)

    logs = list(manager.log_dir.glob("transaction_*.json"))
    assert len(logs) == 1
    log_data = json.loads(logs[0].read_text())
    assert log_data["status"] == "in_progress"
    assert log_data["operations"][0]["old_path"] == str(source)
    assert manager.list_incomplete_transactions()[0]["id"] == log_data["transaction_id"]
    assert not source.exists()
    assert not target.exists()
