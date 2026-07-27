"""CLI tests for read-only merged matching inspection."""

import json
from pathlib import Path

from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)


class _StubMergedProvider:
    def __init__(self) -> None:
        self.seen_limits: list[int] = []
        self.closed = False

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        self.seen_limits.append(limit)
        return [
            MatchCandidate(
                identity=ProviderIdentity(
                    provider="openlibrary",
                    external_id="stub-1",
                    title="Matched Title",
                    authors=["Matched Author"],
                    narrator="Narrator Name",
                    asin="B003TEST",
                    isbn_13="9781234567890",
                    runtime_minutes=610,
                    raw_data={"providers": ["openlibrary", "audnexus"]},
                ),
                confidence=0.97,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=[
                    "Corroborated by 2 providers",
                    "ASIN exact match",
                ],
            )
        ]

    async def close_all(self) -> None:
        self.closed = True


class _UnavailableMergedProvider:
    def __init__(self) -> None:
        self.closed = False

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        raise ConnectionError("offline")

    async def close_all(self) -> None:
        self.closed = True


def test_match_command_prints_reasons_from_merged_provider(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    folder = tmp_path / "library"
    folder.mkdir()
    provider = _StubMergedProvider()
    audiobook = AudiobookSet(
        source_path=folder / "Author - Title",
        raw_title_guess="Title",
        author_guess="Author",
        total_duration=609 * 60.0,
    )

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: provider,
    )
    monkeypatch.setattr(
        "bookbot.cli.AudioFileScanner.scan_directory",
        lambda self, path: [audiobook],
    )

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "config"),
            "match",
            str(folder),
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "Narrator:  Narrator Name" in result.output
    assert "IDs:       ASIN B003TEST | ISBN-13 9781234567890" in result.output
    assert "Runtime:   610 min (local 609 min, delta 0.2%)" in result.output
    assert "Corroborated by 2 providers" in result.output
    assert "ASIN exact match" in result.output
    assert "Providers: openlibrary, audnexus" in result.output
    assert "Review:    [none]" in result.output
    assert provider.seen_limits == [1]
    assert provider.closed is True


def test_match_command_can_emit_structured_json(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    folder = tmp_path / "library"
    folder.mkdir()
    provider = _StubMergedProvider()
    audiobook = AudiobookSet(
        source_path=folder / "Author - Title",
        raw_title_guess="Title",
        author_guess="Author",
    )

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: provider,
    )
    monkeypatch.setattr(
        "bookbot.cli.AudioFileScanner.scan_directory",
        lambda self, path: [audiobook],
    )

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "config"),
            "match",
            str(folder),
            "--limit",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["query"] == "Title"
    assert payload[0]["candidates"][0]["providers"] == ["openlibrary", "audnexus"]
    assert payload[0]["candidates"][0]["asin"] == "B003TEST"


def test_match_command_reports_provider_failures_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    folder = tmp_path / "library"
    folder.mkdir()
    provider = _UnavailableMergedProvider()
    audiobook = AudiobookSet(
        source_path=folder / "Author - Title",
        raw_title_guess="Title",
        author_guess="Author",
    )

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: provider,
    )
    monkeypatch.setattr(
        "bookbot.cli.AudioFileScanner.scan_directory",
        lambda self, path: [audiobook],
    )

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "config"),
            "match",
            str(folder),
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert "Provider unavailable - no matches" in result.output
    assert "network error: offline" in result.output
    assert "Traceback" not in result.output
    assert provider.closed is True
