"""Hardcover metadata provider using GraphQL and REST APIs."""

import asyncio
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from ..core.logging import get_logger
from ..core.models import ProviderIdentity
from .base import MetadataProvider

if TYPE_CHECKING:
    from ..io.cache import CacheManager

logger = get_logger("hardcover_provider")


class HardcoverProvider(MetadataProvider):
    """Metadata provider using Hardcover's public API."""

    BASE_URL = "https://api.hardcover.app/v1"
    GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
    API_TIMEOUT = 30
    RATE_LIMIT_DELAY = 0.2

    GET_BOOK_QUERY = """
query GetBook($id: Int!) {
  books(where: {id: {_eq: $id}}) {
    id
    title
    description
    release_year
    pages
    cached_image
    cached_contributors
    book_series { series { name } position }
    editions { isbn_13 isbn_10 audio_seconds }
  }
}
"""

    def __init__(
        self,
        api_key: str,
        cache_manager: "CacheManager | None" = None,
    ) -> None:
        super().__init__("Hardcover", cache_manager=cache_manager)
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._last_request_time = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.API_TIMEOUT)
            headers = {
                "User-Agent": "BookBot/0.3.0 (https://github.com/itsbryanman/BookBot)",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
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
        """Search Hardcover for books using the search endpoint."""
        cache_key = None
        cache_namespace = "hardcover_search"

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

        await self._rate_limit()
        session = await self._get_session()

        url = f"{self.BASE_URL}/search/books"
        params = {"query": query, "per_page": str(min(limit, 25))}

        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    logger.warning(f"Hardcover search returned {response.status}")
                    return []

                data = await response.json()
                results = data.get("results", data) if isinstance(data, dict) else data
                if not isinstance(results, list):
                    results = [results] if isinstance(results, dict) else []

                identities = []
                for item in results[:limit]:
                    identity = self._parse_search_result(item)
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

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Hardcover search failed: {e}")
            return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get a book by Hardcover ID using GraphQL."""
        cache_key = None
        cache_namespace = "hardcover_book"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(external_id=external_id)
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    return ProviderIdentity.model_validate(cached_entry["data"][0])
                except (KeyError, TypeError, ValueError):
                    pass

        await self._rate_limit()
        session = await self._get_session()

        try:
            book_id = int(external_id)
        except ValueError:
            return None

        payload = {
            "query": self.GET_BOOK_QUERY,
            "variables": {"id": book_id},
        }

        try:
            async with session.post(self.GRAPHQL_URL, json=payload) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                books = data.get("data", {}).get("books", [])
                if not books:
                    return None

                identity = self._parse_graphql_book(books[0])
                if identity and self.cache_manager and cache_key:
                    self.cache_manager.set(
                        cache_namespace,
                        cache_key,
                        [identity.model_dump(mode="json")],
                        ttl_seconds=24 * 60 * 60,
                    )

                return identity

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Hardcover get_by_id failed: {e}")
            return None

    def _parse_search_result(self, data: dict[str, Any]) -> ProviderIdentity | None:
        """Parse a search result into a ProviderIdentity."""
        try:
            book_id = data.get("id")
            title = data.get("title", "")
            if not title or book_id is None:
                return None

            authors = data.get("author_names", [])
            if isinstance(authors, str):
                authors = [authors]

            series_names = data.get("series_names", [])
            series_name = series_names[0] if series_names else None

            isbns = data.get("isbns", [])
            isbn_13 = None
            isbn_10 = None
            for isbn in (isbns or []):
                if isinstance(isbn, str):
                    clean = isbn.replace("-", "")
                    if len(clean) == 13:
                        isbn_13 = clean
                    elif len(clean) == 10:
                        isbn_10 = clean

            return ProviderIdentity(
                provider=self.name,
                external_id=str(book_id),
                title=title,
                authors=authors,
                series_name=series_name,
                isbn_13=isbn_13,
                isbn_10=isbn_10,
                raw_data=data,
            )

        except (KeyError, TypeError, ValueError):
            return None

    def _parse_graphql_book(self, data: dict[str, Any]) -> ProviderIdentity | None:
        """Parse GraphQL book data into a ProviderIdentity."""
        try:
            book_id = data.get("id")
            title = data.get("title", "")
            if not title or book_id is None:
                return None

            # Extract authors from cached_contributors
            authors = []
            contributors = data.get("cached_contributors", [])
            if isinstance(contributors, list):
                for contrib in contributors:
                    if isinstance(contrib, dict):
                        name = contrib.get("name", "")
                        if name:
                            authors.append(name)
                    elif isinstance(contrib, str):
                        authors.append(contrib)

            # Extract series info
            series_name = None
            series_index = None
            book_series = data.get("book_series", [])
            if book_series:
                first_series = book_series[0]
                series_data = first_series.get("series", {})
                if series_data:
                    series_name = series_data.get("name")
                position = first_series.get("position")
                if position is not None:
                    series_index = str(position)

            # Extract ISBNs and audio_seconds from editions
            isbn_13 = None
            isbn_10 = None
            audio_seconds = None
            editions = data.get("editions", [])
            for edition in (editions or []):
                if isinstance(edition, dict):
                    if not isbn_13 and edition.get("isbn_13"):
                        isbn_13 = edition["isbn_13"]
                    if not isbn_10 and edition.get("isbn_10"):
                        isbn_10 = edition["isbn_10"]
                    if edition.get("audio_seconds"):
                        audio_seconds = edition["audio_seconds"]

            # Cover
            cover_urls = []
            cached_image = data.get("cached_image")
            if cached_image:
                if isinstance(cached_image, str):
                    cover_urls = [cached_image]
                elif isinstance(cached_image, dict):
                    url = cached_image.get("url", "")
                    if url:
                        cover_urls = [url]

            year = data.get("release_year")
            description = data.get("description", "")

            identity = ProviderIdentity(
                provider=self.name,
                external_id=str(book_id),
                title=title,
                authors=authors,
                series_name=series_name,
                series_index=series_index,
                year=year,
                isbn_13=isbn_13,
                isbn_10=isbn_10,
                description=description if description else None,
                cover_urls=cover_urls,
                raw_data=data,
            )

            # Store audio_seconds in raw_data for match scoring
            if audio_seconds:
                identity.raw_data["_audio_seconds"] = audio_seconds

            return identity

        except (KeyError, TypeError, ValueError):
            return None

