"""LibriVox API provider for public domain audiobooks."""

import asyncio
import json
from typing import TYPE_CHECKING, Any

import aiohttp
from pydantic import ValidationError

from ..core.models import ProviderIdentity
from .base import MetadataProvider

if TYPE_CHECKING:
    from ..io.cache import CacheManager


class LibriVoxProvider(MetadataProvider):
    """Provider for LibriVox public domain audiobooks."""

    def __init__(self, cache_manager: "CacheManager | None" = None) -> None:
        super().__init__("LibriVox", cache_manager=cache_manager)
        self.base_url = "https://librivox.org/api/feed"
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "BookBot/1.0 (Audiobook Organizer)"},
            )
        return self.session

    async def close(self) -> None:
        """Close HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

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
        """Search for audiobooks using LibriVox API."""
        cache_key = None
        cache_namespace = "librivox_search"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(
                title=title,
                author=author,
                series=series,
                language=language,
                limit=limit,
            )
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    return [
                        ProviderIdentity.model_validate(item)
                        for item in cached_entry["data"]
                    ]
                except (ValidationError, KeyError, TypeError):
                    pass

        session = await self._get_session()

        # LibriVox supports title and author searches
        params = {"format": "json", "limit": min(limit, 50)}  # LibriVox API limit

        if title:
            params["title"] = title
        elif author:
            params["author"] = author
        else:
            # If no title or author, search for anything
            if series:
                params["title"] = series
            else:
                return []

        try:
            async with session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    return []

                data = await response.json()
                books = data.get("books", [])

                identities = []
                for book in books:
                    identity = self._parse_book(book)
                    if identity:
                        identities.append(identity)

                if identities and self.cache_manager and cache_key:
                    self.cache_manager.set(
                        cache_namespace,
                        cache_key,
                        [identity.model_dump(mode="json") for identity in identities],
                        ttl_seconds=12 * 60 * 60,
                    )

                return identities

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get a book by its LibriVox ID."""
        cache_key = None
        cache_namespace = "librivox_id"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(external_id=external_id)
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    data = cached_entry["data"][0]
                    return ProviderIdentity.model_validate(data)
                except (ValidationError, KeyError, TypeError):
                    pass

        session = await self._get_session()

        params = {"format": "json", "id": external_id}

        try:
            async with session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                books = data.get("books", [])

                if books:
                    identity = self._parse_book(books[0])
                    if identity and self.cache_manager and cache_key:
                        self.cache_manager.set(
                            cache_namespace,
                            cache_key,
                            [identity.model_dump(mode="json")],
                            ttl_seconds=12 * 60 * 60,
                        )
                    return identity

                return None

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

    def _parse_book(self, book: dict[str, Any]) -> ProviderIdentity | None:
        """Parse a LibriVox book into a ProviderIdentity."""
        title = book.get("title")
        if not title:
            return None

        # Extract basic information
        authors = []
        if book.get("authors"):
            for author in book["authors"]:
                if isinstance(author, dict):
                    first_name = author.get("first_name", "")
                    last_name = author.get("last_name", "")
                    author_name = f"{first_name} {last_name}".strip()
                    if author_name:
                        authors.append(author_name)
                elif isinstance(author, str):
                    authors.append(author)

        # Parse publication info
        year = None
        if book.get("date_recorded"):
            try:
                year = int(book["date_recorded"].split("-")[0])
            except (ValueError, IndexError, AttributeError):
                pass

        # LibriVox is primarily English content
        language = book.get("language", "en")

        # Extract URLs
        url_librivox = book.get("url_librivox", "")
        url_project = book.get("url_project", "")

        # Cover art (LibriVox doesn't always have cover art)
        cover_urls = []
        if url_project:
            # Construct potential cover art URL
            cover_urls.append(url_project + "/cover.jpg")

        # Description
        description = book.get("description", "")

        # Genre/Category
        genre = book.get("genre", "")
        categories = [genre] if genre else []

        # Total time (if available)
        total_time = book.get("totaltimesecs")

        # Sections (chapters)
        sections = book.get("sections", [])

        return ProviderIdentity(
            provider=self.name,
            external_id=str(book.get("id", "")),
            title=title,
            authors=authors,
            series_name=None,  # LibriVox doesn't track series well
            series_index=None,
            year=year,
            language=language,
            cover_urls=cover_urls,
            description=description,
            raw_data={
                "genre": genre,
                "categories": categories,
                "url_librivox": url_librivox,
                "url_project": url_project,
                "total_time_seconds": total_time,
                "num_sections": len(sections),
                "reader_count": len(
                    {s.get("reader") for s in sections if s.get("reader")}
                ),
                "copyright_year": book.get("copyright_year"),
                "zip_file": book.get("url_zip_file"),
                "m4b_file": book.get("url_m4b"),
                "public_domain": True,  # All LibriVox content is public domain
            },
        )

