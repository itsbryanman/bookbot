"""Google Books API provider for metadata."""

import asyncio
import json
from typing import Any, List, Optional

import aiohttp
from rapidfuzz import fuzz

from pydantic import ValidationError

from ..core.models import AudiobookSet, ProviderIdentity
from .base import MetadataProvider


class GoogleBooksProvider(MetadataProvider):
    """Provider for Google Books API."""

    def __init__(self, api_key: Optional[str] = None, cache_manager=None):
        super().__init__("Google Books", cache_manager=cache_manager)
        self.api_key = api_key
        self.base_url = "https://www.googleapis.com/books/v1"
        self.session: Optional[aiohttp.ClientSession] = None

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
        title: Optional[str] = None,
        author: Optional[str] = None,
        series: Optional[str] = None,
        isbn: Optional[str] = None,
        year: Optional[int] = None,
        language: Optional[str] = None,
        limit: int = 10,
    ) -> List[ProviderIdentity]:
        """Search for books using Google Books API."""
        # Build search query
        query_parts = []

        if isbn:
            query_parts.append(f"isbn:{isbn}")
        if title:
            query_parts.append(f'intitle:"{title}"')
        if author:
            query_parts.append(f'inauthor:"{author}"')

        if not query_parts and series:
            query_parts.append(f'"{series}"')

        if not query_parts:
            return []

        query = " ".join(query_parts)

        params = {
            "q": query,
            "maxResults": min(limit, 40),  # Google Books API limit
            "printType": "books",
            "fields": (
                "items(id,volumeInfo(title,authors,publishedDate,industryIdentifiers,"
                "language,description,imageLinks,categories,seriesInfo))"
            ),
        }

        if self.api_key:
            params["key"] = self.api_key

        if language:
            params["langRestrict"] = language

        cache_key = None
        cache_namespace = "googlebooks_search"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(
                query=query,
                language=language,
                limit=limit,
                api_key_used=bool(self.api_key),
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

        try:
            async with session.get(
                f"{self.base_url}/volumes", params=params
            ) as response:
                if response.status != 200:
                    return []

                data = await response.json()
                items = data.get("items", [])

                identities = []
                for item in items:
                    identity = self._parse_volume(item)
                    if identity:
                        identities.append(identity)

                if identities and self.cache_manager and cache_key:
                    self.cache_manager.set(
                        cache_namespace,
                        cache_key,
                        [identity.model_dump(mode="json") for identity in identities],
                        ttl_seconds=24 * 60 * 60,
                    )

                return identities

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return []

    async def get_by_id(self, external_id: str) -> Optional[ProviderIdentity]:
        """Get a book by its Google Books volume ID."""
        cache_key = None
        cache_namespace = "googlebooks_volume"
        if self.cache_manager:
            cache_key = self.cache_manager.generate_query_hash(volume_id=external_id)
            cached_entry = self.cache_manager.get(cache_namespace, cache_key)
            if cached_entry:
                try:
                    data = cached_entry["data"][0]
                    return ProviderIdentity.model_validate(data)
                except (ValidationError, KeyError, TypeError):
                    pass

        session = await self._get_session()

        params = {
            "fields": (
                "id,volumeInfo(title,authors,publishedDate,industryIdentifiers,"
                "language,description,imageLinks,categories,seriesInfo)"
            )
        }

        if self.api_key:
            params["key"] = self.api_key

        try:
            async with session.get(
                f"{self.base_url}/volumes/{external_id}", params=params
            ) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                identity = self._parse_volume(data)
                if identity and self.cache_manager and cache_key:
                    self.cache_manager.set(
                        cache_namespace,
                        cache_key,
                        [identity.model_dump(mode="json")],
                        ttl_seconds=24 * 60 * 60,
                    )
                return identity

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

    def _parse_volume(self, item: dict[str, Any]) -> ProviderIdentity | None:
        """Parse a Google Books volume item into a ProviderIdentity."""
        volume_info = item.get("volumeInfo", {})

        title = volume_info.get("title")
        if not title:
            return None

        # Extract basic information
        authors = volume_info.get("authors", [])
        published_date = volume_info.get("publishedDate", "")
        language = volume_info.get("language", "en")
        description = volume_info.get("description", "")

        # Extract year from published date
        year = None
        if published_date:
            try:
                year = int(published_date.split("-")[0])
            except (ValueError, IndexError):
                pass

        # Extract ISBNs
        industry_identifiers = volume_info.get("industryIdentifiers", [])
        identifiers = {
            identifier["type"].lower(): identifier["identifier"]
            for identifier in industry_identifiers
        }
        isbn_10 = identifiers.get("isbn_10")
        isbn_13 = identifiers.get("isbn_13")

        # Extract cover art URLs
        cover_urls_list = []
        image_links = volume_info.get("imageLinks", {})
        for _size, url in image_links.items():
            # Convert HTTP to HTTPS
            if url.startswith("http://"):
                url = url.replace("http://", "https://")
            cover_urls_list.append(url)

        # Extract series information (if available)
        series_name = None
        series_index = None

        # Check for series info in the response
        series_info = volume_info.get("seriesInfo")
        if series_info:
            series_name = (
                series_info.get("volumeSeries", [{}])[0]
                .get("series", {})
                .get("seriesId")
            )
            series_index = series_info.get("volumeSeries", [{}])[0].get("orderNumber")

        # Fallback: try to extract series from title or categories
        if not series_name:
            categories = volume_info.get("categories", [])
            for category in categories:
                if "series" in category.lower():
                    series_name = category
                    break

        return ProviderIdentity(
            provider=self.name,
            external_id=item["id"],
            title=title,
            authors=authors,
            series_name=series_name,
            series_index=series_index,
            year=year,
            language=language,
            isbn_10=isbn_10,
            isbn_13=isbn_13,
            description=description,
            cover_urls=cover_urls_list,
            raw_data={
                "categories": volume_info.get("categories", []),
                "page_count": volume_info.get("pageCount"),
                "publisher": volume_info.get("publisher"),
                "published_date": published_date,
            },
        )

    def calculate_match_score(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity
    ) -> float:
        """Calculate match score between audiobook set and Google Books identity."""
        score = 0.0
        total_weight = 0.0

        # Title matching (weight: 0.4)
        if audiobook_set.raw_title_guess and identity.title:
            title_ratio = (
                fuzz.ratio(
                    audiobook_set.raw_title_guess.lower(), identity.title.lower()
                )
                / 100.0
            )
            score += title_ratio * 0.4
            total_weight += 0.4

        # Author matching (weight: 0.3)
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
                score += best_author_score * 0.3
                total_weight += 0.3

        # Series matching (weight: 0.15)
        if audiobook_set.series_guess and identity.series_name:
            series_ratio = (
                fuzz.ratio(
                    audiobook_set.series_guess.lower(), identity.series_name.lower()
                )
                / 100.0
            )
            score += series_ratio * 0.15
            total_weight += 0.15

        # Language matching (weight: 0.1)
        if audiobook_set.language_guess and identity.language:
            if audiobook_set.language_guess.lower() == identity.language.lower():
                score += 1.0 * 0.1
            total_weight += 0.1

        # Year proximity (weight: 0.05)
        if audiobook_set.year_guess and identity.year:
            year_diff = abs(audiobook_set.year_guess - identity.year)
            if year_diff == 0:
                year_score = 1.0
            elif year_diff <= 2:
                year_score = 0.8
            elif year_diff <= 5:
                year_score = 0.5
            else:
                year_score = 0.0

            score += year_score * 0.05
            total_weight += 0.05

        # Normalize score
        if total_weight > 0:
            return score / total_weight
        else:
            return 0.0
