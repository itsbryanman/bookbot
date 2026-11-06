"""apply checks the plan file is writable before moving anything."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.planning import PlanBuilder, save_plan


def _plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "cfg"
    config = ConfigManager(config_dir).load_config()
    config.output.write_metadata_json = False
    config.output.write_cover = False
    library = tmp_path / "lib"
    folder = library / "rough"
    folder.mkdir(parents=True)
    track_path = folder / "01.mp3"
    track_path.write_bytes(b"audio")
    book = AudiobookSet(
        source_path=folder,
        raw_title_guess="Dune",
        total_tracks=1,
        tracks=[
            Track(
                src_path=track_path,
                disc=1,
                track_index=1,
                file_size=5,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
        ],
        chosen_identity=ProviderIdentity(
            provider="x", external_id="1", title="Dune", authors=["Frank Herbert"]
        ),
    )
    plan = PlanBuilder(config).create_plan(
        library_root=library, audiobook_sets=[book], source_roots=[library]
    )
    assert plan.operations
    plan_file = tmp_path / "plans" / "plan.json"
    save_plan(plan, plan_file)
    return config_dir, plan_file, track_path


def _apply(config_dir: Path, plan_file: Path, *extra: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(
        cli, ["--config-dir", str(config_dir), "apply", str(plan_file), "--yes", *extra]
    )


def _deny_writes_to(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    real_access = os.access

    def fake_access(path, mode, *args, **kwargs):  # type: ignore[no-untyped-def]
        if mode & os.W_OK and Path(path).resolve() in {
            denied.resolve(),
            denied.parent.resolve(),
        }:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr("bookbot.cli.os.access", fake_access)


def test_unwritable_plan_file_fails_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)
    _deny_writes_to(monkeypatch, plan_file)

    result = _apply(config_dir, plan_file)

    assert result.exit_code == 1
    assert "not writable" in result.output
    assert "--plan-out" in result.output
    assert track_path.exists()
    assert "Transaction ID" not in result.output


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores file permissions",
)
def test_read_only_plan_file_real_permissions(tmp_path: Path) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)
    plan_file.chmod(0o444)
    try:
        result = _apply(config_dir, plan_file)
    finally:
        plan_file.chmod(0o644)
    assert result.exit_code == 1
    assert track_path.exists()


def test_no_update_plan_applies_without_writing_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)
    before = plan_file.read_text()
    _deny_writes_to(monkeypatch, plan_file)

    result = _apply(config_dir, plan_file, "--no-update-plan")

    assert result.exit_code == 0, result.output
    assert not track_path.exists()
    assert plan_file.read_text() == before
    assert "Transaction ID" in result.output


def test_plan_out_writes_applied_plan_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)
    _deny_writes_to(monkeypatch, plan_file)
    plan_out = tmp_path / "out" / "applied.json"

    result = _apply(config_dir, plan_file, "--plan-out", str(plan_out))

    assert result.exit_code == 0, result.output
    assert not track_path.exists()
    assert json.loads(plan_out.read_text())["applied_transaction_id"]


def test_plan_out_and_no_update_plan_are_exclusive(tmp_path: Path) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)
    result = _apply(
        config_dir, plan_file, "--no-update-plan", "--plan-out", str(tmp_path / "x")
    )
    assert result.exit_code == 1
    assert track_path.exists()


def test_failed_post_apply_save_reports_transaction_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir, plan_file, track_path = _plan(tmp_path)

    def broken_save(plan, path):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("bookbot.cli.save_plan", broken_save)

    result = _apply(config_dir, plan_file)

    assert result.exit_code == 2
    assert not track_path.exists()
    match = re.search(r"Transaction ID: ([0-9a-f-]{36})", result.output)
    assert match, result.output
    assert "files were changed; plan file not updated" in result.output
    assert "bookbot history" in result.output
    assert f"bookbot undo {match.group(1)}" in result.output
