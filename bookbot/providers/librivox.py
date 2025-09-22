"""LibriVox API provider for public domain audiobooks."""

import asyncio
import json
from typing import Any

import aiohttp
from rapidfuzz import fuzz

from ..core.models import AudiobookSet, ProviderIdentity
from .base import MetadataProvider


class LibriVoxProvider(MetadataProvider):
    """Provider for LibriVox public domain audiobooks."""

    def __init__(self):
        super().__init__("LibriVox")
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
        title: str | None = None,
        author: str | None = None,
        series: str | None = None,
        isbn: str | None = None,
        year: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[ProviderIdentity]:
        """Search for audiobooks using LibriVox API."""
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

                return identities

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get a book by its LibriVox ID."""
        session = await self._get_session()

        params = {"format": "json", "id": external_id}

        try:
            async with session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                books = data.get("books", [])

                if books:
                    return self._parse_book(books[0])

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

    def calculate_match_score(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity
    ) -> float:
        """Calculate match score between audiobook set and LibriVox identity."""
        score = 0.0
        total_weight = 0.0

        # Title matching (weight: 0.5 - higher for LibriVox since less metadata)
        if audiobook_set.raw_title_guess and identity.title:
            title_ratio = (
                fuzz.ratio(
                    audiobook_set.raw_title_guess.lower(), identity.title.lower()
                )
                / 100.0
            )
            score += title_ratio * 0.5
            total_weight += 0.5

        # Author matching (weight: 0.35)
        if audiobook_set.author_guess and identity.authors:
            author_scores = []
            for author in identity.authors:
                author_ratio = (
                    fuzz.ratio(audiobook_set.author_guess.lower(), author.lower())
                    / 100.0
                )
                author_scores.append(author_ratio)

            if author_scores:
                best_author_score = max(author_scores)
                score += best_author_score * 0.35
                total_weight += 0.35

        # Language matching (weight: 0.1)
        if audiobook_set.language_guess and identity.language:
            if audiobook_set.language_guess.lower() == identity.language.lower():
                score += 1.0 * 0.1
            total_weight += 0.1

        # Bonus for public domain content (weight: 0.05)
        if identity.raw_data.get("public_domain"):
            score += 1.0 * 0.05
            total_weight += 0.05

        # Normalize score
        if total_weight > 0:
            return score / total_weight
        else:
            return 0.0
