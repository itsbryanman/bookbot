"""Audible provider for audiobook metadata (metadata only, no DRM operations)."""

import asyncio
import re
from typing import TYPE_CHECKING

import aiohttp
from bs4 import BeautifulSoup

from ..core.models import MatchCandidate, MatchConfidence, ProviderIdentity
from .base import MetadataProvider

if TYPE_CHECKING:
    from ..io.cache import CacheManager


class AudibleProvider(MetadataProvider):
    """Provider for Audible audiobook metadata."""

    def __init__(
        self,
        marketplace: str = "US",
        cache_manager: "CacheManager | None" = None,
    ) -> None:
        super().__init__("Audible", cache_manager=cache_manager)
        self.marketplace = marketplace.upper()

        # Audible marketplace domains
        self.marketplaces = {
            "US": "audible.com",
            "UK": "audible.co.uk",
            "CA": "audible.ca",
            "AU": "audible.com.au",
            "FR": "audible.fr",
            "DE": "audible.de",
            "IT": "audible.it",
            "ES": "audible.es",
            "JP": "audible.co.jp",
            "IN": "audible.in",
        }

        self.base_domain = self.marketplaces.get(marketplace, "audible.com")
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/91.0.4472.124 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                    "Connection": "keep-alive",
                },
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
        """Search for audiobooks using Audible search."""
        session = await self._get_session()

        # Build search query
        query_parts = []
        if title:
            query_parts.append(title)
        if author:
            query_parts.append(author)
        if series:
            query_parts.append(series)

        if not query_parts:
            return []

        query = " ".join(query_parts)

        # Audible search URL
        search_url = f"https://www.{self.base_domain}/search"
        params = {
            "keywords": query,
            "node": "18573211011",  # Audible Books & Originals category
        }

        try:
            async with session.get(search_url, params=params) as response:
                if response.status != 200:
                    return []

                html = await response.text()
                identities = self._parse_search_results(html)

                return identities[:limit]

        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get audiobook details by Audible ASIN."""
        session = await self._get_session()

        product_url = f"https://www.{self.base_domain}/pd/{external_id}"

        try:
            async with session.get(product_url) as response:
                if response.status != 200:
                    return None

                html = await response.text()
                return self._parse_product_page(html, external_id)

        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _try_asin_lookup(self, asin: str) -> MatchCandidate | None:
        """Resolve an ASIN directly via get_by_id."""
        identity = await self.get_by_id(asin)
        if identity is not None:
            return MatchCandidate(
                identity=identity,
                confidence=1.0,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=["ASIN exact match"],
            )
        return None

    def _parse_search_results(self, html: str) -> list[ProviderIdentity]:
        """Parse Audible search results HTML."""
        soup = BeautifulSoup(html, "html.parser")
        identities: list[ProviderIdentity] = []

        for item in soup.select("[data-asin]"):
            asin_raw = item.get("data-asin")
            asin = str(asin_raw) if asin_raw else ""
            if not asin:
                continue

            title_tag = item.select_one("h3.bc-heading")
            if not title_tag:
                continue

            link = title_tag.find("a")
            title = self._clean_text(link.get_text() if link else title_tag.get_text())
            if not title:
                continue

            author_elements = item.select("li.authorLabel a")
            if not author_elements:
                # fallback to any text inside author label
                author_container = item.select_one("li.authorLabel")
                author_text = (
                    self._clean_text(author_container.get_text())
                    if author_container
                    else ""
                )
                authors = [author_text] if author_text else []
            else:
                authors = [self._clean_text(a.get_text()) for a in author_elements]

            cover_tag = item.select_one("img")
            cover_urls: list[str] = []
            if cover_tag and cover_tag.get("src"):
                cover_url_raw = cover_tag.get("src")
                cover_url = str(cover_url_raw) if cover_url_raw else ""
                if cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                if cover_url:
                    cover_urls.append(cover_url)

            product_href_raw = link.get("href") if link else None
            product_href = str(product_href_raw) if product_href_raw else None
            if product_href and product_href.startswith("/"):
                product_url = f"https://www.{self.base_domain}{product_href}"
            else:
                product_url = (
                    product_href or f"https://www.{self.base_domain}/pd/{asin}"
                )

            identity = ProviderIdentity(
                provider=self.name,
                external_id=asin,
                title=title,
                authors=authors,
                series_name=None,
                series_index=None,
                year=None,
                language="en",
                asin=asin,
                narrator=None,
                cover_urls=cover_urls,
                description="",
                raw_data={
                    "marketplace": self.marketplace,
                    "product_url": product_url,
                },
            )
            identities.append(identity)

        return identities

    def _parse_product_page(self, html: str, asin: str) -> ProviderIdentity | None:
        """Parse Audible product page for detailed information."""

        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.select_one("h1.bc-heading")
        if not title_tag:
            return None
        title = self._clean_text(title_tag.get_text())

        author_elements = soup.select("li.authorLabel a")
        authors = [
            self._clean_text(a.get_text())
            for a in author_elements
            if a.get_text(strip=True)
        ]

        if not authors:
            # fallback to regex as last resort
            authors = []
            author_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>\s*By:\s*</span>.*?'
                r"<a[^>]*>([^<]+)</a>"
            )
            for match in re.finditer(author_pattern, html, re.IGNORECASE):
                author = self._clean_text(match.group(1))
                if author:
                    authors.append(author)

        narrator_elements = soup.select("li.narratorLabel a")
        narrator = None
        if narrator_elements:
            narrator = self._clean_text(narrator_elements[0].get_text())
        else:
            narrator_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>\s*Narrated by:\s*</span>.*?'
                r"<a[^>]*>([^<]+)</a>"
            )
            narrator_match = re.search(narrator_pattern, html, re.IGNORECASE)
            if narrator_match:
                narrator = self._clean_text(narrator_match.group(1))

        series_name = None
        series_index = None
        series_link = soup.select_one("li.seriesLabel a")
        if series_link:
            series_text = self._clean_text(series_link.get_text())
            book_num_match = re.search(
                r"(.+?),?\s*Book\s*(\d+)", series_text, re.IGNORECASE
            )
            if book_num_match:
                series_name = book_num_match.group(1).strip()
                try:
                    series_index = int(book_num_match.group(2))
                except ValueError:
                    series_index = None
            else:
                series_name = series_text
        else:
            series_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>\s*Series:\s*</span>.*?'
                r"<a[^>]*>([^<]+)</a>"
            )
            series_match = re.search(series_pattern, html, re.IGNORECASE)
            if series_match:
                series_text = self._clean_text(series_match.group(1))
                book_num_match = re.search(
                    r"(.+?),?\s*Book\s*(\d+)", series_text, re.IGNORECASE
                )
                if book_num_match:
                    series_name = book_num_match.group(1).strip()
                    try:
                        series_index = int(book_num_match.group(2))
                    except ValueError:
                        series_index = None
                else:
                    series_name = series_text

        # Release year
        year = None
        release_element = soup.select_one(
            "li.releaseDateLabel span[data-qa='release-date']"
        )
        if release_element:
            year_match = re.search(r"(\d{4})", release_element.get_text())
            if year_match:
                year = int(year_match.group(1))
        else:
            date_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>\s*Release date:\s*</span>'
                r"\s*<span[^>]*>([^<]+)</span>"
            )
            date_match = re.search(date_pattern, html, re.IGNORECASE)
            if date_match:
                date_str = self._clean_text(date_match.group(1))
                year_match = re.search(r"(\d{4})", date_str)
                if year_match:
                    year = int(year_match.group(1))

        description = ""
        description_container = soup.select_one(
            "div.bc-section.productPublisherSummary"
        )
        if description_container:
            description = self._clean_text(description_container.get_text())
        else:
            desc_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>'
                r"\s*Publisher.?s Summary\s*</span>.*?"
                r"<span[^>]*>([^<]+)</span>"
            )
            desc_match = re.search(desc_pattern, html, re.IGNORECASE | re.DOTALL)
            if desc_match:
                description = self._clean_text(desc_match.group(1))

        cover_urls_list: list[str] = []
        cover_img = soup.select_one("img.bc-image-inset-border")
        if cover_img and cover_img.get("src"):
            cover_url_raw = cover_img.get("src")
            cover_url = str(cover_url_raw) if cover_url_raw else ""
            if cover_url.startswith("//"):
                cover_url = "https:" + cover_url
            if cover_url:
                cover_urls_list.append(cover_url)
        else:
            cover_pattern = (
                r'<img[^>]*src="([^"]*audible[^"]*\.(jpg|png))"[^>]*'
                r'class="[^"]*bc-image-inset-border[^"]*"'
            )
            cover_match = re.search(cover_pattern, html, re.IGNORECASE)
            if cover_match:
                cover_url = cover_match.group(1)
                if cover_url.startswith("//"):
                    cover_url = "https:" + cover_url
                cover_urls_list.append(cover_url)

        runtime = None
        runtime_element = soup.select_one("li.runtimeLabel span")
        if runtime_element:
            runtime = self._clean_text(runtime_element.get_text())
        else:
            runtime_pattern = (
                r'<span[^>]*class="[^"]*bc-text[^"]*"[^>]*>\s*Length:\s*</span>'
                r"\s*<span[^>]*>([^<]+)</span>"
            )
            runtime_match = re.search(runtime_pattern, html, re.IGNORECASE)
            if runtime_match:
                runtime = self._clean_text(runtime_match.group(1))

        # Parse runtime string into minutes (e.g. "16 hrs and 12 mins")
        runtime_minutes = self._parse_runtime_to_minutes(runtime)

        return ProviderIdentity(
            provider=self.name,
            external_id=asin,
            title=title,
            authors=authors,
            series_name=series_name,
            series_index=str(series_index) if series_index is not None else None,
            year=year,
            language="en",  # Audible is primarily English
            asin=asin,
            narrator=narrator,
            runtime_minutes=runtime_minutes,
            cover_urls=cover_urls_list,
            description=description,
            raw_data={
                "marketplace": self.marketplace,
                "product_url": f"https://www.{self.base_domain}/pd/{asin}",
                "narrator": narrator,
                "runtime": runtime,
            },
        )

    @staticmethod
    def _parse_runtime_to_minutes(runtime: str | None) -> int | None:
        """Parse Audible runtime string to minutes.

        Handles formats like "16 hrs and 12 mins", "3 hrs", "45 mins".
        """
        if not runtime:
            return None
        total = 0
        hours_match = re.search(r"(\d+)\s*hr", runtime, re.IGNORECASE)
        mins_match = re.search(r"(\d+)\s*min", runtime, re.IGNORECASE)
        if hours_match:
            total += int(hours_match.group(1)) * 60
        if mins_match:
            total += int(mins_match.group(1))
        return total if total > 0 else None

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text."""
        if not text:
            return ""

        # Remove HTML entities and extra whitespace
        text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

