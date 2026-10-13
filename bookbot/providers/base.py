"""Base provider interface for metadata sources."""

import asyncio
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import aiohttp

from ..core.exceptions import ProviderUnavailableError
from ..core.logging import get_logger
from ..core.matching import AdvancedMatcher, clean_query_title
from ..core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)

if TYPE_CHECKING:
    from ..io.cache import CacheManager

logger = get_logger("provider")

# Exceptions meaning "the service could not be reached", as opposed to a bad
# answer from a reachable service.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectionError,
    asyncio.TimeoutError,
    OSError,
)

# Network failures seen by the provider calls of the current find_matches()
# run. A ContextVar keeps concurrent lookups (one asyncio task per book) from
# seeing each other's failures.
_NETWORK_FAILURES: ContextVar[list[BaseException] | None] = ContextVar(
    "bookbot_provider_network_failures", default=None
)


class MetadataProvider(ABC):
    """Abstract base class for metadata providers."""

    # Fail fast when a host is unreachable; a healthy API answers well within
    # these limits, and an unreachable one should cost seconds, not minutes.
    CONNECT_TIMEOUT = 4.0
    TOTAL_TIMEOUT = 15.0

    def __init__(self, name: str, cache_manager: "CacheManager | None" = None) -> None:
        self.name = name
        self.cache_manager = cache_manager

    def can_search(self, audiobook_set: AudiobookSet) -> bool:
        """Whether this provider can look the set up at all.

        Providers that only resolve identifiers (Audnexus) answer ``False``
        when the set carries none, so the manager neither waits on them nor
        mistakes their empty answer for a reachable service saying "no
        matches".
        """
        return True

    def _client_timeout(self) -> aiohttp.ClientTimeout:
        """HTTP timeouts for this provider's session."""
        return aiohttp.ClientTimeout(
            total=self.TOTAL_TIMEOUT,
            connect=self.CONNECT_TIMEOUT,
            sock_connect=self.CONNECT_TIMEOUT,
        )

    def _note_http_status(self, status: int) -> None:
        """Treat 5xx answers as the service being unavailable."""
        if status >= 500:
            self._record_network_failure(
                ConnectionError(f"{self.name} answered HTTP {status}")
            )

    def _record_network_failure(self, exc: BaseException) -> None:
        """Note that a request failed because the service could not be reached.

        ``search``/``get_by_id`` keep returning empty results so direct callers
        are unaffected, but ``find_matches`` turns a lookup whose requests all
        failed this way into :class:`ProviderUnavailableError`, which lets the
        provider manager tell "unreachable" apart from "no matches".
        """
        logger.debug(f"{self.name} request failed: {exc!r}")
        failures = _NETWORK_FAILURES.get()
        if failures is not None:
            failures.append(exc)

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
        with a fallback ladder. Raises :class:`ProviderUnavailableError` as
        soon as a request fails because the service is unreachable, instead of
        walking the rest of the ladder against a dead host.
        """
        failures: list[BaseException] = []
        token = _NETWORK_FAILURES.set(failures)
        try:
            return await self._find_matches_ladder(audiobook_set, limit, failures)
        finally:
            _NETWORK_FAILURES.reset(token)

    def _raise_if_unreachable(self, failures: list[BaseException]) -> None:
        if failures:
            raise ProviderUnavailableError(
                f"{self.name} unreachable: {failures[-1]}",
                details={
                    "provider": self.name,
                    "error": str(failures[-1]),
                    "kind": "network",
                },
            )

    async def _find_matches_ladder(
        self,
        audiobook_set: AudiobookSet,
        limit: int,
        failures: list[BaseException],
    ) -> list[MatchCandidate]:
        raw_title = audiobook_set.raw_title_guess
        title = clean_query_title(raw_title) if raw_title else None
        author = audiobook_set.author_guess

        async def search(**kwargs: Any) -> list[ProviderIdentity]:
            results = await self.search(limit=limit, **kwargs)
            if not results:
                self._raise_if_unreachable(failures)
            return results

        # ASIN fast path
        if audiobook_set.asin_guess:
            asin_candidate = await self._try_asin_lookup(audiobook_set.asin_guess)
            if asin_candidate is not None:
                return [asin_candidate]
            self._raise_if_unreachable(failures)

        # ISBN fast path via search(isbn=...)
        if audiobook_set.isbn_guess:
            isbn_results = await search(isbn=audiobook_set.isbn_guess)
            if isbn_results:
                return self._score_identities(audiobook_set, isbn_results)

        # Primary fuzzy search: cleaned title + author + series
        identities = await search(
            title=title,
            author=author,
            series=audiobook_set.series_guess,
            isbn=audiobook_set.isbn_guess,
            language=audiobook_set.language_guess,
        )
        if identities:
            return self._score_identities(audiobook_set, identities)

        matcher = AdvancedMatcher()

        # (a) Strip series/volume suffix from title + author
        if title:
            extracted = matcher.extract_series(title)
            if extracted:
                identities = await search(title=extracted[0], author=author)
                if identities:
                    return self._score_identities(audiobook_set, identities)

        # (b) Raw title + author, in case the noise was part of the title
        if raw_title and title != raw_title:
            identities = await search(title=raw_title, author=author)
            if identities:
                return self._score_identities(audiobook_set, identities)

        # (c) Title only
        if title and (author or audiobook_set.series_guess or audiobook_set.isbn_guess):
            identities = await search(title=title)
            if identities:
                return self._score_identities(audiobook_set, identities)

        # (d) Author only, then filter by title similarity >= 0.5
        if author:
            identities = await search(author=author)
            if identities and title:
                filtered = [
                    ident
                    for ident in identities
                    if matcher.match_title(title, ident.title) >= 0.5
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
        raw_title = audiobook_set.raw_title_guess or ""
        query_titles = [raw_title]
        cleaned_title = clean_query_title(raw_title) if raw_title else ""
        if cleaned_title and cleaned_title != raw_title:
            query_titles.append(cleaned_title)
        candidates = []
        for identity in identities:
            # Score against both the raw and the cleaned title; release noise
            # such as "(Unabridged)" should not cost a correct match points.
            match_score = max(
                (
                    matcher.calculate_match(
                        query_title=query_title,
                        query_author=audiobook_set.author_guess,
                        query_series=audiobook_set.series_guess,
                        query_year=None,
                        result_title=identity.title,
                        result_authors=identity.authors,
                        result_series=identity.series_name,
                        result_year=identity.year,
                    )
                    for query_title in query_titles
                ),
                key=lambda score: score.combined_score,
            )

            candidate = MatchCandidate(
                identity=identity,
                confidence=match_score.combined_score,
                confidence_level=self._get_confidence_level(match_score.combined_score),
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
