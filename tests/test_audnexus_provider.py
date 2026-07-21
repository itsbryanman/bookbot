"""Tests for the Audnexus provider."""

from unittest.mock import AsyncMock, patch

import pytest

from bookbot.providers.audnexus import AudnexusProvider


@pytest.fixture
def provider():
    return AudnexusProvider(marketplace="us")


@pytest.fixture
def mock_book_response():
    return {
        "asin": "B003JVHSIO",
        "title": "The Name of the Wind",
        "authors": [{"name": "Patrick Rothfuss", "asin": "B001IGFHW6"}],
        "narrators": [{"name": "Nick Podehl"}],
        "seriesPrimary": {"name": "The Kingkiller Chronicle", "position": "1"},
        "releaseDate": "2009-04-28",
        "publisherName": "Brilliance Audio",
        "summary": "A great fantasy novel.",
        "language": "english",
        "image": "https://example.com/cover.jpg",
    }


@pytest.fixture
def mock_search_response(mock_book_response):
    return [mock_book_response]


@pytest.fixture
def mock_chapters_response():
    return {
        "chapters": [
            {"title": "Prologue", "startOffsetMs": 0, "lengthMs": 120000},
            {"title": "Chapter 1", "startOffsetMs": 120000, "lengthMs": 300000},
            {"title": "Chapter 2", "startOffsetMs": 420000, "lengthMs": 250000},
        ]
    }


class TestAudnexusProvider:
    def test_init(self, provider):
        assert provider.name == "Audnexus"
        assert provider.marketplace == "us"

    def test_parse_book(self, provider, mock_book_response):
        identity = provider._parse_book(mock_book_response)
        assert identity is not None
        assert identity.title == "The Name of the Wind"
        assert identity.authors == ["Patrick Rothfuss"]
        assert identity.narrator == "Nick Podehl"
        assert identity.series_name == "The Kingkiller Chronicle"
        assert identity.series_index == "1"
        assert identity.year == 2009
        assert identity.asin == "B003JVHSIO"
        assert identity.publisher == "Brilliance Audio"
        assert identity.provider == "Audnexus"
        assert len(identity.cover_urls) == 1

    def test_parse_book_missing_title(self, provider):
        result = provider._parse_book({"asin": "B123", "title": ""})
        assert result is None

    def test_parse_book_no_series(self, provider):
        data = {
            "asin": "B123",
            "title": "Standalone Book",
            "authors": [{"name": "Author"}],
            "narrators": [],
        }
        identity = provider._parse_book(data)
        assert identity is not None
        assert identity.series_name is None
        assert identity.series_index is None

    def test_match_score_perfect(self, provider):
        from bookbot.core.matching import AdvancedMatcher

        matcher = AdvancedMatcher()
        score = matcher.calculate_match(
            query_title="The Name of the Wind",
            query_author="Patrick Rothfuss",
            query_series=None,
            query_year=2009,
            result_title="The Name of the Wind",
            result_authors=["Patrick Rothfuss"],
            result_series=None,
            result_year=2009,
        )
        assert score.combined_score > 0.85

    def test_match_score_partial(self, provider):
        from bookbot.core.matching import AdvancedMatcher

        matcher = AdvancedMatcher()
        score = matcher.calculate_match(
            query_title="name of the wind",
            query_author=None,
            query_series=None,
            query_year=None,
            result_title="The Name of the Wind",
            result_authors=["Patrick Rothfuss"],
            result_series=None,
            result_year=None,
        )
        assert 0.3 < score.combined_score < 0.8

    def test_match_score_no_data(self, provider):
        from bookbot.core.matching import AdvancedMatcher

        matcher = AdvancedMatcher()
        score = matcher.calculate_match(
            query_title="",
            query_author=None,
            query_series=None,
            query_year=None,
            result_title="Something",
            result_authors=[],
            result_series=None,
            result_year=None,
        )
        assert score.combined_score < 0.5

    @pytest.mark.asyncio
    async def test_search_empty_query(self, provider):
        result = await provider.search()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_chapters(self, provider, mock_chapters_response):
        with patch.object(
            provider, "_request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = mock_chapters_response
            chapters = await provider.get_chapters("B003JVHSIO")

        assert len(chapters) == 3
        assert chapters[0]["title"] == "Prologue"
        assert chapters[0]["start_ms"] == 0
        assert chapters[0]["length_ms"] == 120000
        assert chapters[1]["title"] == "Chapter 1"

    @pytest.mark.asyncio
    async def test_get_by_id(self, provider, mock_book_response):
        with patch.object(
            provider, "_request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = mock_book_response
            identity = await provider.get_by_id("b003jvhsio")

        # Should uppercase the ASIN
        mock_request.assert_called_once_with("GET", "/books/B003JVHSIO")
        assert identity is not None
        assert identity.title == "The Name of the Wind"

    @pytest.mark.asyncio
    async def test_close(self, provider):
        await provider.close()  # should not raise
