"""Base provider interface for metadata sources."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..core.matching import AdvancedMatcher
from ..core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)

if TYPE_CHECKING:
    from ..io.cache import CacheManager


class MetadataProvider(ABC):
    """Abstract base class for metadata providers."""

    def __init__(self, name: str, cache_manager: "CacheManager | None" = None) -> None:
        self.name = name
        self.cache_manager = cache_manager

    @abstractmethod
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
        """Search for books matching the given criteria."""
        pass

    @abstractmethod
    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get a book by its external ID."""
        pass

    async def close(self) -> None:  # noqa: B027
        """Close any open connections. Override in subclasses as needed."""

    async def find_matches(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        """Find potential matches for an audiobook set.

        Uses identifier-first lookup: ASIN, then ISBN, then fuzzy search
        with a fallback ladder.
        """
        # ASIN fast path
        if audiobook_set.asin_guess:
            asin_candidate = await self._try_asin_lookup(audiobook_set.asin_guess)
            if asin_candidate is not None:
                return [asin_candidate]

        # ISBN fast path via search(isbn=...)
        if audiobook_set.isbn_guess:
            isbn_results = await self.search(
                isbn=audiobook_set.isbn_guess, limit=limit
            )
            if isbn_results:
                return self._score_identities(audiobook_set, isbn_results)

        # Primary fuzzy search: title + author + series
        identities = await self.search(
            title=audiobook_set.raw_title_guess,
            author=audiobook_set.author_guess,
            series=audiobook_set.series_guess,
            isbn=audiobook_set.isbn_guess,
            language=audiobook_set.language_guess,
            limit=limit,
        )

        if identities:
            return self._score_identities(audiobook_set, identities)

        # Fallback ladder (at most 3 more calls)
        matcher = AdvancedMatcher()

        # (a) Strip series/volume suffix from title + author
        if audiobook_set.raw_title_guess:
            extracted = matcher.extract_series(audiobook_set.raw_title_guess)
            if extracted:
                bare_title = extracted[0]
                identities = await self.search(
                    title=bare_title,
                    author=audiobook_set.author_guess,
                    limit=limit,
                )
                if identities:
                    return self._score_identities(audiobook_set, identities)

        # (b) Title only
        if audiobook_set.raw_title_guess:
            identities = await self.search(
                title=audiobook_set.raw_title_guess, limit=limit
            )
            if identities:
                return self._score_identities(audiobook_set, identities)

        # (c) Author only, then filter by title similarity >= 0.5
        if audiobook_set.author_guess:
            identities = await self.search(
                author=audiobook_set.author_guess, limit=limit
            )
            if identities and audiobook_set.raw_title_guess:
                filtered = [
                    ident
                    for ident in identities
                    if matcher.match_title(
                        audiobook_set.raw_title_guess, ident.title
                    )
                    >= 0.5
                ]
                if filtered:
                    return self._score_identities(audiobook_set, filtered)

        return []

    async def _try_asin_lookup(self, asin: str) -> MatchCandidate | None:
        """Try to resolve an ASIN via get_by_id. Override in providers that
        support ASIN-based lookup (e.g. Audible). Default returns None."""
        return None

    def _score_identities(
        self,
        audiobook_set: AudiobookSet,
        identities: list[ProviderIdentity],
    ) -> list[MatchCandidate]:
        """Score and rank identities using AdvancedMatcher."""
        matcher = AdvancedMatcher()
        candidates = []
        for identity in identities:
            match_score = matcher.calculate_match(
                query_title=audiobook_set.raw_title_guess or "",
                query_author=audiobook_set.author_guess,
                query_series=audiobook_set.series_guess,
                query_year=None,
                result_title=identity.title,
                result_authors=identity.authors,
                result_series=identity.series_name,
                result_year=identity.year,
            )

            candidate = MatchCandidate(
                identity=identity,
                confidence=match_score.combined_score,
                confidence_level=self._get_confidence_level(
                    match_score.combined_score
                ),
                match_reasons=match_score.reasons,
            )
            candidates.append(candidate)

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    def _get_confidence_level(self, score: float) -> MatchConfidence:
        if score > 0.85:
            return MatchConfidence.HIGH
        elif score > 0.65:
            return MatchConfidence.MEDIUM
        else:
            return MatchConfidence.LOW
