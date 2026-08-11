"""Tests for the persisted manual review queue workflow."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bookbot.cli import _run_review_session, cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.planning import PlanBuilder


class _ReviewProvider:
    def __init__(self) -> None:
        self.closed = False

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        return [
            MatchCandidate(
                identity=ProviderIdentity(
                    provider="audnexus",
                    external_id="wrong-match",
                    title="Wrong Match",
                    authors=["Wrong Author"],
                ),
                confidence=0.72,
                confidence_level=MatchConfidence.MEDIUM,
                match_reasons=["stub"],
            ),
            MatchCandidate(
                identity=ProviderIdentity(
                    provider="audnexus",
                    external_id="clean-title",
                    title="Clean Title",
                    authors=["Author Name"],
                ),
                confidence=0.95,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=["stub"],
            ),
        ][:limit]

    async def close_all(self) -> None:
        self.closed = True


def _make_audiobook(folder: Path) -> AudiobookSet:
    source_path = folder / "rough-book"
    source_path.mkdir(parents=True, exist_ok=True)
    track_path = source_path / "01.mp3"
    track_path.touch()
    track = Track(
        src_path=track_path,
        disc=1,
        track_index=1,
        file_size=0,
        audio_format=AudioFormat.MP3,
        status=TrackStatus.VALID,
    )
    return AudiobookSet(
        source_path=source_path,
        raw_title_guess="Rough Book",
        total_tracks=1,
        tracks=[track],
    )


def test_accept_command_persists_manual_choice_and_unblocks_planning(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    library.mkdir()
    config_dir = tmp_path / "config"
    provider = _ReviewProvider()

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: provider,
    )
    monkeypatch.setattr(
        "bookbot.cli.AudioFileScanner.scan_directory",
        lambda self, path: [_make_audiobook(path)],
    )

    queue_result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "review-queue",
            str(library),
            "--json",
        ],
    )

    assert queue_result.exit_code == 0
    queue_payload = json.loads(queue_result.output)
    assert queue_payload[0]["set_index"] == 1
    assert queue_payload[0]["review_reason"].startswith("Confidence 0.72")

    accept_result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "accept",
            str(library),
            "--set",
            "1",
            "--candidate",
            "2",
        ],
    )

    assert accept_result.exit_code == 0
    assert "Accepted candidate 2 for set 1" in accept_result.output

    config_manager = ConfigManager(config_dir)
    audiobook_sets, _sessions = _run_review_session(
        config_manager,
        library,
        limit=10,
    )
    plan = PlanBuilder(config_manager.load_config()).create_plan(
        library,
        audiobook_sets,
        source_roots=[library],
    )

    assert plan.skipped == []
    assert plan.operations
    assert provider.closed is True
