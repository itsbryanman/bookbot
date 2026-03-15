"""Tests for the Hardcover provider."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from bookbot.core.models import AudiobookSet, ProviderIdentity
from bookbot.providers.hardcover import HardcoverProvider


@pytest.fixture
def provider():
    return HardcoverProvider(api_key="test-token")


@pytest.fixture
def mock_search_result():
    return {
        "id": 42,
        "title": "Project Hail Mary",
        "author_names": ["Andy Weir"],
        "series_names": [],
        "isbns": ["9780593135204"],
    }


@pytest.fixture
def mock_graphql_book():
    return {
        "id": 42,
        "title": "Project Hail Mary",
        "description": "A lone astronaut must save Earth.",
        "release_year": 2021,
        "pages": 496,
        "cached_image": "https://example.com/cover.jpg",
        "cached_contributors": [{"name": "Andy Weir"}],
        "book_series": [],
        "editions": [
            {"isbn_13": "9780593135204", "isbn_10": None, "audio_seconds": 58320}
        ],
    }


class TestHardcoverProvider:
    def test_init(self, provider):
        assert provider.name == "Hardcover"
        assert provider.api_key == "test-token"

    def test_parse_search_result(self, provider, mock_search_result):
        identity = provider._parse_search_result(mock_search_result)
        assert identity is not None
        assert identity.title == "Project Hail Mary"
        assert identity.authors == ["Andy Weir"]
        assert identity.isbn_13 == "9780593135204"
        assert identity.external_id == "42"

    def test_parse_search_result_no_id(self, provider):
        result = provider._parse_search_result({"title": "Test"})
        assert result is None

    def test_parse_graphql_book(self, provider, mock_graphql_book):
        identity = provider._parse_graphql_book(mock_graphql_book)
        assert identity is not None
        assert identity.title == "Project Hail Mary"
        assert identity.authors == ["Andy Weir"]
        assert identity.year == 2021
        assert identity.isbn_13 == "9780593135204"
        assert identity.raw_data.get("_audio_seconds") == 58320

    def test_parse_graphql_book_with_series(self, provider, mock_graphql_book):
        mock_graphql_book["book_series"] = [
            {"series": {"name": "Test Series"}, "position": 3}
        ]
        identity = provider._parse_graphql_book(mock_graphql_book)
        assert identity is not None
        assert identity.series_name == "Test Series"
        assert identity.series_index == "3"

    def test_calculate_match_score_with_duration_bonus(self, provider):
        ab_set = AudiobookSet(
            source_path=Path("/tmp/test"),
            raw_title_guess="Project Hail Mary",
            author_guess="Andy Weir",
            total_duration=58000.0,  # close to 58320
        )
        identity = ProviderIdentity(
            provider="Hardcover",
            external_id="42",
            title="Project Hail Mary",
            authors=["Andy Weir"],
            raw_data={"_audio_seconds": 58320},
        )
        score = provider.calculate_match_score(ab_set, identity)
        # Should include the 0.05 duration bonus
        assert score > 0.8

    @pytest.mark.asyncio
    async def test_search_empty_query(self, provider):
        result = await provider.search()
        assert result == []

    @pytest.mark.asyncio
    async def test_close(self, provider):
        await provider.close()
