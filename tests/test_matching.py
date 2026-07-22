"""Tests for the unified AdvancedMatcher engine."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from bookbot.core.matching import AdvancedMatcher
from bookbot.core.models import AudiobookSet, ProviderIdentity
from bookbot.providers.base import MetadataProvider

# ── Title normalization & article stripping ──


class TestTitleNormalization:
    def setup_method(self) -> None:
        self.m = AdvancedMatcher()

    def test_the_stand_vs_stand_the(self) -> None:
        """'Stand, The' should match 'The Stand'."""
        score = self.m.match_title("The Stand", "Stand, The")
        # After normalization both become "stand" variants, expect high score
        assert score >= 0.9

    def test_articles_stripped(self) -> None:
        assert self.m.normalize_title("The Lord of the Rings") == "lord of the rings"
        assert self.m.normalize_title("A Game of Thrones") == "game of thrones"
        assert self.m.normalize_title("An Example Title") == "example title"

    def test_unicode_cafe(self) -> None:
        """Unicode 'Cafe\u0301' ≡ 'Cafe'."""
        score = self.m.match_title("Caf\u00e9", "Cafe")
        assert score == 1.0


# ── Author normalization & aliases ──


class TestAuthorMatching:
    def setup_method(self) -> None:
        self.m = AdvancedMatcher()

    def test_last_first_flip(self) -> None:
        """'King, Stephen' ≡ 'Stephen King'."""
        score = self.m.match_author("King, Stephen", "Stephen King")
        assert score == 1.0

    def test_alias_bachman(self) -> None:
        """'Stephen King' ≡ 'Richard Bachman' via alias (score 1.0)."""
        score = self.m.match_author("Stephen King", "Richard Bachman")
        assert score == 1.0

    def test_alias_galbraith(self) -> None:
        score = self.m.match_author("J.K. Rowling", "Robert Galbraith")
        assert score == 1.0


# ── Series extraction ──


class TestSeriesExtraction:
    def setup_method(self) -> None:
        self.m = AdvancedMatcher()

    def test_book_pattern(self) -> None:
        result = self.m.extract_series("Mistborn Book 1")
        assert result is not None
        assert result[0] == "Mistborn"
        assert result[1] == 1
        assert result[2] == 0.95

    def test_hash_pattern(self) -> None:
        result = self.m.extract_series("Stormlight #4")
        assert result is not None
        assert result[0] == "Stormlight"
        assert result[1] == 4
        assert result[2] == 0.90

    def test_part_pattern(self) -> None:
        result = self.m.extract_series("Foo Part 2")
        assert result is not None
        assert result[0] == "Foo"
        assert result[1] == 2
        assert result[2] == 0.85

    def test_no_series(self) -> None:
        result = self.m.extract_series("Just a Title")
        assert result is None


# ── JSON alias file overrides/extends builtins ──


class TestAliasJsonLoading:
    def test_json_overrides_builtins(self, tmp_path: Path) -> None:
        """An author_aliases.json should extend/override builtins."""
        aliases_json = tmp_path / "author_aliases.json"
        aliases_json.write_text(
            json.dumps({"Stephen King": ["Richard Bachman", "The King"]}),
            encoding="utf-8",
        )

        m = AdvancedMatcher()
        with patch.object(
            AdvancedMatcher, "_find_aliases_json", return_value=aliases_json
        ):
            # Force reload
            m._merged_aliases = None
            aliases = m._effective_aliases

        # Should have merged "the king" into the set
        assert "the king" in aliases["stephen king"]
        # Original builtins should still be present
        assert "richard bachman" in aliases["stephen king"]


# ── MatchScore combined calculation ──


class TestCalculateMatch:
    def setup_method(self) -> None:
        self.m = AdvancedMatcher()

    def test_exact_match(self) -> None:
        score = self.m.calculate_match(
            query_title="The Stand",
            query_author="Stephen King",
            query_series=None,
            query_year=1978,
            result_title="The Stand",
            result_authors=["Stephen King"],
            result_series=None,
            result_year=1978,
        )
        assert score.combined_score > 0.85
        assert score.confidence == "high"
        assert "Excellent title match" in score.reasons
        assert "Author confirmed" in score.reasons

    def test_low_confidence(self) -> None:
        score = self.m.calculate_match(
            query_title="xyz",
            query_author="abc",
            query_series=None,
            query_year=None,
            result_title="totally different",
            result_authors=["unknown author"],
            result_series=None,
            result_year=None,
        )
        assert score.combined_score < 0.65
        assert score.confidence == "low"


# ── End-to-end find_matches via provider ──


class _MockProvider(MetadataProvider):
    """Minimal provider for testing find_matches path."""

    def __init__(self) -> None:
        super().__init__("mock")

    async def search(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        series: str | None = None,
        isbn: str | None = None,
        year: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[ProviderIdentity]:
        return [
            ProviderIdentity(
                provider="mock",
                external_id="1",
                title="The Stand",
                authors=["Stephen King"],
                year=1978,
            ),
            ProviderIdentity(
                provider="mock",
                external_id="2",
                title="It",
                authors=["Stephen King"],
                year=1986,
            ),
        ]

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


class TestFindMatchesEndToEnd:
    def test_find_matches_sorted_with_reasons(self) -> None:
        provider = _MockProvider()
        audiobook = AudiobookSet(
            source_path=Path("/tmp/the_stand"),
            raw_title_guess="The Stand",
            author_guess="Stephen King",
        )

        candidates = asyncio.run(provider.find_matches(audiobook))

        # Should return candidates sorted by confidence (highest first)
        assert len(candidates) == 2
        assert candidates[0].confidence >= candidates[1].confidence
        assert candidates[0].identity.title == "The Stand"
        assert isinstance(candidates[0].match_reasons, list)
        assert len(candidates[0].match_reasons) > 0
        # Without series/year, max combined = title(0.5) + author(0.3) = 0.8
        assert candidates[0].confidence >= 0.8
