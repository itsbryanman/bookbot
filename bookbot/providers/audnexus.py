"""Audnexus metadata provider for audiobook data."""

import asyncio
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from rapidfuzz import fuzz

from ..core.logging import get_logger
from ..core.models import AudiobookSet, ProviderIdentity
from .base import MetadataProvider

if TYPE_CHECKING:
    from ..io.cache import CacheManager

logger = get_logger("audnexus_provider")


class AudnexusProvider(MetadataProvider):
    """Metadata provider using the public Audnexus API."""

    BASE_URL = "https://api.audnex.us"
    API_TIMEOUT = 30
    RATE_LIMIT_DELAY = 0.2

    def __init__(
        self,
        marketplace: str = "us",
        cache_manager: "CacheManager | None" = None,
    ) -> None:
        super().__init__("Audnexus", cache_manager=cache_manager)
        self.marketplace = marketplace.lower()
        self._session: aiohttp.ClientSession | None = None
        self._last_request_time = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.API_TIMEOUT)
            headers = {
                "User-Agent": "BookBot/0.3.0 (https://github.com/itsbryanman/BookBot)",
                "Accept": "application/json",
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        current_time = time.time()
        elapsed = current_time - self._last_request_time
        if elapsed < self.RATE_LIMIT_DELAY:
            await asyncio.sleep(self.RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.time()

    async def _request(
        self, method: str, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | list[Any] | None:
        """Make an API request with rate limiting and retry on 429."""
        await self._rate_limit()
        session = await self._get_session()

        if params is None:
            params = {}
        if self.marketplace != "us":
            params["region"] = self.marketplace

        url = f"{self.BASE_URL}{path}"
        logger.debug(f"Audnexus API request: {method} {url}", params=params)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with session.request(method, url, params=params) as response:
                    if response.status == 429:
                        wait = 2 ** (attempt + 1)
                        logger.warning(f"Rate limited by Audnexus, waiting {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if response.status == 404:
                        return None
                    if response.status != 200:
                        logger.warning(
                            f"Audnexus returned {response.status} for {path}"
                        )
                        return None
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Audnexus request failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None

        return None

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
        """Search Audnexus for books by name."""
        cache_key = None
        cache_namespace = "audnexus_search"

        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(
                title=title, author=author, series=series, isbn=isbn,
                year=year, language=language, limit=limit,
            )
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    return [
                        ProviderIdentity.model_validate(item)
                        for item in cached_entry["data"]
                    ]
                except (KeyError, TypeError, ValueError):
                    pass

        query = title or ""
        if author and query:
            query = f"{query} {author}"
        elif author:
            query = author

        if not query:
            return []

        data = await self._request("GET", "/books", params={"name": query})
        if not data or not isinstance(data, list):
            return []

        identities = []
        for item in data[:limit]:
            identity = self._parse_book(item)
            if identity:
                identities.append(identity)

        if identities and self.cache_manager and cache_key:
            self.cache_manager.set(
                cache_namespace,
                cache_key,
                [i.model_dump(mode="json") for i in identities],
                ttl_seconds=24 * 60 * 60,
            )

        return identities

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get a book by ASIN."""
        asin = external_id.upper()

        cache_key = None
        cache_namespace = "audnexus_book"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(asin=asin)
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    return ProviderIdentity.model_validate(cached_entry["data"][0])
                except (KeyError, TypeError, ValueError):
                    pass

        data = await self._request("GET", f"/books/{asin}")
        if not data or not isinstance(data, dict):
            return None

        identity = self._parse_book(data)
        if identity and self.cache_manager and cache_key:
            self.cache_manager.set(
                cache_namespace,
                cache_key,
                [identity.model_dump(mode="json")],
                ttl_seconds=24 * 60 * 60,
            )

        return identity

    async def get_chapters(self, asin: str) -> list[dict[str, Any]]:
        """Get chapter data for a book by ASIN.

        Returns a list of dicts with keys: title, start_ms, length_ms.
        """
        asin = asin.upper()
        data = await self._request("GET", f"/books/{asin}/chapters")
        if not data or not isinstance(data, dict):
            return []

        chapters_raw = data.get("chapters", [])
        chapters = []
        for ch in chapters_raw:
            chapters.append({
                "title": ch.get("title", f"Chapter {len(chapters) + 1}"),
                "start_ms": int(ch.get("startOffsetMs", 0)),
                "length_ms": int(ch.get("lengthMs", 0)),
            })

        return chapters

    def _parse_book(self, data: dict[str, Any]) -> ProviderIdentity | None:
        """Parse Audnexus book data into a ProviderIdentity."""
        try:
            asin = data.get("asin", "")
            title = data.get("title", "")
            if not title:
                return None

            authors = []
            for author in data.get("authors", []):
                name = author.get("name", "")
                if name:
                    authors.append(name)

            narrators = []
            for narrator in data.get("narrators", []):
                name = narrator.get("name", "")
                if name:
                    narrators.append(name)

            series_name = None
            series_index = None
            series_list = data.get("seriesPrimary", data.get("series", []))
            if isinstance(series_list, dict):
                series_name = series_list.get("name")
                series_index = series_list.get("position")
            elif isinstance(series_list, list) and series_list:
                first_series = series_list[0]
                if isinstance(first_series, dict):
                    series_name = first_series.get("name")
                    series_index = first_series.get("position")

            year = None
            release_date = data.get("releaseDate", "")
            if release_date and len(release_date) >= 4:
                try:
                    year = int(release_date[:4])
                except ValueError:
                    pass

            cover_url = data.get("image", "")
            cover_urls = [cover_url] if cover_url else []

            publisher = data.get("publisherName", "") or data.get("publisher", "")
            description = data.get("summary", "") or data.get("description", "")
            language = data.get("language", "")

            return ProviderIdentity(
                provider=self.name,
                external_id=asin.upper(),
                title=title,
                authors=authors,
                narrator=narrators[0] if narrators else None,
                series_name=series_name,
                series_index=str(series_index) if series_index else None,
                year=year,
                language=language if language else None,
                publisher=publisher if publisher else None,
                description=description if description else None,
                asin=asin.upper(),
                cover_urls=cover_urls,
                raw_data=data,
            )

        except (KeyError, TypeError, ValueError):
            return None

    def calculate_match_score(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity
    ) -> float:
        """Calculate match score with weighting: title 0.5, author 0.3, narrator 0.1, year 0.1."""
        score = 0.0

        # Title similarity (weight: 0.5)
        if audiobook_set.raw_title_guess and identity.title:
            title_score = (
                fuzz.token_sort_ratio(
                    audiobook_set.raw_title_guess.lower(), identity.title.lower()
                )
                / 100.0
            )
            score += title_score * 0.5

        # Author similarity (weight: 0.3)
        if audiobook_set.author_guess and identity.authors:
            best_author_score = 0.0
            for author in identity.authors:
                author_score = (
                    fuzz.ratio(audiobook_set.author_guess.lower(), author.lower())
                    / 100.0
                )
                best_author_score = max(best_author_score, author_score)
            score += best_author_score * 0.3

        # Narrator similarity (weight: 0.1)
        if audiobook_set.narrator_guess and identity.narrator:
            narrator_score = (
                fuzz.ratio(
                    audiobook_set.narrator_guess.lower(), identity.narrator.lower()
                )
                / 100.0
            )
            score += narrator_score * 0.1

        # Year match (weight: 0.1)
        if audiobook_set.year_guess and identity.year:
            if audiobook_set.year_guess == identity.year:
                score += 0.1
            elif abs(audiobook_set.year_guess - identity.year) <= 1:
                score += 0.05

        return min(1.0, max(0.0, score))
