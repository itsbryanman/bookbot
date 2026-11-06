"""organize runs through the write-ahead journal: undo, recover, preflight."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import AudiobookSet, AudioFormat, Track, TrackStatus
from bookbot.core.operations import TransactionManager


def _book(folder: Path, title: str, author: str) -> AudiobookSet:
    folder.mkdir(parents=True, exist_ok=True)
    tracks = []
    for index in (1, 2, 3):
        path = folder / f"{index:02d}.mp3"
        if not path.exists():
            path.write_bytes(f"{title}-{index}".encode())
        tracks.append(
            Track(
                src_path=path,
                disc=1,
                track_index=index,
                file_size=path.stat().st_size,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
        )
    cover = folder / "cover.jpg"
    if not cover.exists():
        cover.write_bytes(b"jpg")
    return AudiobookSet(
        source_path=folder,
        raw_title_guess=title,
        author_guess=author,
        total_tracks=3,
        tracks=tracks,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _dirs(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir())


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "lib"
    root.mkdir()

    def fake_scan(self, path, progress=None):  # type: ignore[no-untyped-def]
        return [
            _book(path / "messy one", "American Gods", "Neil Gaiman"),
            _book(path / "messy two", "Good Omens", "Terry Pratchett"),
        ]

    monkeypatch.setattr(
        "bookbot.core.discovery.AudioFileScanner.scan_directory", fake_scan
    )
    fake_scan(None, root)
    return root


def _invoke(config_dir: Path, *args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(cli, ["--config-dir", str(config_dir), *args])


def test_organize_confirm_then_undo_restores_exact_tree(
    tmp_path: Path, library: Path
) -> None:
    config_dir = tmp_path / "cfg"
    before_files = _snapshot(library)
    before_dirs = _dirs(library)

    result = _invoke(config_dir, "organize", str(library), "--confirm")

    assert result.exit_code == 0, result.output
    match = re.search(r"Transaction ID: ([0-9a-f-]{36})", result.output)
    assert match, result.output
    assert (library / "Gaiman, Neil" / "American Gods" / "cover.jpg").is_file()
    assert not (library / "messy one").exists()

    history = _invoke(config_dir, "history")
    assert match.group(1)[:8] in history.output

    undone = _invoke(config_dir, "undo", match.group(1))
    assert undone.exit_code == 0, undone.output
    assert _snapshot(library) == before_files
    assert _dirs(library) == before_dirs


class _Crash(BaseException):
    """Stands in for the process being killed (not caught by rollback)."""


def test_crash_mid_organize_leaves_journal_that_recover_rolls_back(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    before_files = _snapshot(library)
    real_rename = Path.rename
    calls = {"n": 0}

    def crashing_rename(self: Path, target):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 5:
            raise _Crash()
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", crashing_rename)
    with pytest.raises(_Crash):
        CliRunner().invoke(
            cli,
            ["--config-dir", str(config_dir), "organize", str(library), "--confirm"],
            catch_exceptions=False,
        )
    monkeypatch.setattr(Path, "rename", real_rename)

    assert _snapshot(library) != before_files
    manager = TransactionManager(ConfigManager(config_dir))
    incomplete = manager.list_incomplete_transactions()
    assert len(incomplete) == 1
    transaction_id = incomplete[0]["id"]

    listed = _invoke(config_dir, "recover")
    assert transaction_id in listed.output

    recovered = _invoke(config_dir, "recover", transaction_id)
    assert recovered.exit_code == 0, recovered.output
    assert _snapshot(library) == before_files


def test_organize_refuses_target_on_another_filesystem(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "other-disk"
    target.mkdir()
    before = _snapshot(library)

    def fake_device(path: Path) -> int:
        resolved = path.resolve()
        return 2 if resolved == target or target in resolved.parents else 1

    monkeypatch.setattr("bookbot.core.organizer._device_of", fake_device)

    result = _invoke(
        tmp_path / "cfg",
        "organize",
        str(library),
        "--target",
        str(target),
        "--confirm",
    )

    assert result.exit_code == 1
    assert "different filesystem" in result.output
    assert _snapshot(library) == before
    assert list(target.iterdir()) == []


def test_dry_run_flag_is_hidden_and_deprecated(tmp_path: Path, library: Path) -> None:
    help_text = _invoke(tmp_path / "cfg", "organize", "--help").output
    assert "--dry-run" not in help_text

    result = _invoke(tmp_path / "cfg", "organize", str(library), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output
    assert "Dry run complete" in result.output


def test_organize_journal_is_a_rename_transaction(
    tmp_path: Path, library: Path
) -> None:
    config_dir = tmp_path / "cfg"
    result = _invoke(config_dir, "organize", str(library), "--confirm")
    assert result.exit_code == 0, result.output

    manager = TransactionManager(ConfigManager(config_dir))
    logs = list(manager.log_dir.glob("*.json"))
    payloads = [json.loads(p.read_text()) for p in logs]
    statuses = [p.get("status") for p in payloads if "operations" in p]
    assert statuses == ["completed"]


# ── Codex review on #8 / #5 ──


def test_relative_paths_are_journaled_absolute_and_undo_from_elsewhere(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    before = _snapshot(library)
    monkeypatch.chdir(tmp_path)

    result = _invoke(config_dir, "organize", "lib", "--confirm")
    assert result.exit_code == 0, result.output
    transaction_id = re.search(r"Transaction ID: ([0-9a-f-]{36})", result.output)
    assert transaction_id

    manager = TransactionManager(ConfigManager(config_dir))
    log = json.loads(manager._transaction_log_path(transaction_id.group(1)).read_text())
    paths = [op[key] for op in log["operations"] for key in ("old_path", "new_path")]
    assert paths and all(Path(p).is_absolute() for p in paths)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    undone = _invoke(config_dir, "undo", transaction_id.group(1))
    assert undone.exit_code == 0, undone.output
    assert _snapshot(library) == before


def test_organize_carries_saved_decisions_to_new_paths(
    tmp_path: Path, library: Path
) -> None:
    from bookbot.core.models import MatchCandidate, MatchConfidence, ProviderIdentity
    from bookbot.core.review_store import ReviewDecisionStore

    config_dir = tmp_path / "cfg"
    store = ReviewDecisionStore(ConfigManager(config_dir))
    book = _book(library / "messy one", "American Gods", "Neil Gaiman")
    identity = ProviderIdentity(
        provider="x",
        external_id="ag",
        title="American Gods",
        authors=["Neil Gaiman"],
        narrator="George Guidall",
    )
    store.set_accept(
        book,
        MatchCandidate(
            identity=identity, confidence=0.9, confidence_level=MatchConfidence.HIGH
        ),
        candidate_index=1,
    )

    result = _invoke(config_dir, "organize", str(library), "--confirm")
    assert result.exit_code == 0, result.output

    new_folder = next(p.parent for p in library.rglob("*.mp3") if "Gaiman" in str(p))
    moved = AudiobookSet(
        source_path=new_folder,
        tracks=[
            Track(
                src_path=path,
                disc=1,
                track_index=index,
                file_size=path.stat().st_size,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
            for index, path in enumerate(sorted(new_folder.glob("*.mp3")), 1)
        ],
    )
    moved_decision = store.get(moved)
    assert moved_decision is not None
    assert moved_decision.identity == identity
