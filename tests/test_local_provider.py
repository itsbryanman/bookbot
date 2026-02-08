"""Tests for the LocalMetadataProvider."""

import json
from pathlib import Path

import pytest

from bookbot.core.models import AudiobookSet
from bookbot.providers.local import LocalMetadataProvider


@pytest.mark.asyncio()
async def test_local_provider_reads_json(tmp_path: Path) -> None:
    """Local provider should read metadata from a JSON sidecar file."""
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "title": "The Test Book",
                "authors": ["Example Author"],
                "series": "Example Series",
                "series_index": "1",
                "year": 2023,
                "language": "en",
            }
        ),
        encoding="utf-8",
    )

    audiobook_set = AudiobookSet(source_path=tmp_path)

    provider = LocalMetadataProvider()
    matches = await provider.find_matches(audiobook_set)

    assert len(matches) == 1
    candidate = matches[0]
    assert candidate.identity.title == "The Test Book"
    assert candidate.identity.authors == ["Example Author"]
    assert candidate.identity.series_name == "Example Series"
    assert candidate.identity.series_index == "1"
    assert candidate.identity.year == 2023
    assert candidate.confidence >= 0.9
    assert "Loaded from metadata.json" in candidate.match_reasons


@pytest.mark.asyncio()
async def test_local_provider_reads_nfo(tmp_path: Path) -> None:
    """NFO metadata should be parsed into a provider identity."""
    nfo_path = tmp_path / "book.nfo"
    nfo_path.write_text(
        """
        Title: Another Test Book
        Author: Jane Doe
        Series: Test Cycle
        Series Index: 2
        Year: 2018
        Narrator: Sample Narrator
        Language: en
        """.strip(),
        encoding="utf-8",
    )

    audiobook_set = AudiobookSet(source_path=tmp_path)

    provider = LocalMetadataProvider()
    matches = await provider.find_matches(audiobook_set)

    assert len(matches) == 1
    candidate = matches[0]
    assert candidate.identity.title == "Another Test Book"
    assert candidate.identity.authors == ["Jane Doe"]
    assert candidate.identity.series_name == "Test Cycle"
    assert candidate.identity.series_index == "2"
    assert candidate.identity.year == 2018
    assert "Loaded from book.nfo" in candidate.match_reasons


@pytest.mark.asyncio()
async def test_local_provider_returns_empty_when_missing_metadata(
    tmp_path: Path,
) -> None:
    """No sidecar files should result in no matches."""
    audiobook_set = AudiobookSet(source_path=tmp_path)

    provider = LocalMetadataProvider()
    matches = await provider.find_matches(audiobook_set)

    assert matches == []
