"""Audible provider for audiobook metadata (metadata only, no DRM operations)."""

import re
from typing import TYPE_CHECKING, Any

import aiohttp

from ..core.matching import filter_author_names
from ..core.models import MatchCandidate, MatchConfidence, ProviderIdentity
from .base import NETWORK_ERRORS, MetadataProvider

if TYPE_CHECKING:
    from ..io.cache import CacheManager


class AudibleProvider(MetadataProvider):
    """Provider for Audible audiobook metadata.

    Uses the public Audible catalog API (``api.audible.<tld>``), which needs no
    authentication and returns structured JSON. The storefront HTML pages are
    not used: they sit behind bot protection and answer automated requests
    with HTTP 503.
    """

    RESPONSE_GROUPS = "contributors,product_desc,product_attrs,series,media"

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

        self.base_domain = self.marketplaces.get(self.marketplace, "audible.com")
        self.api_base = f"https://api.{self.base_domain}"
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self._client_timeout(),
                headers={
                    "User-Agent": (
                        "BookBot/0.3.0 (https://github.com/itsbryanman/BookBot)"
                    ),
                    "Accept": "application/json",
                },
            )
        return self.session

    async def close(self) -> None:
        """Close HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_json(
        self, path: str, params: dict[str, str]
    ) -> dict[str, Any] | None:
        """GET a catalog API path; ``None`` on any failure.

        Connection failures, timeouts and 5xx answers are recorded as network
        failures so the provider manager can tell "unreachable" from "no
        results".
        """
        try:
            session = await self._get_session()
            async with session.get(f"{self.api_base}{path}", params=params) as resp:
                if resp.status != 200:
                    self._note_http_status(resp.status)
                    return None
                data = await resp.json(content_type=None)
        except NETWORK_ERRORS as exc:
            self._record_network_failure(exc)
            return None
        except (aiohttp.ClientError, ValueError):
            return None
        return data if isinstance(data, dict) else None

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
        """Search the Audible catalog.

        Title and author go into their dedicated fields: the catalog ranks
        free-text ``keywords`` searches by popularity, so "Rock Paper Scissors
        Alice Feeney" returns unrelated bestsellers first. The series name is
        not sent; it narrows results to nothing more often than it helps.
        """
        params: dict[str, str] = {
            "num_results": str(max(1, min(limit, 50))),
            "products_sort_by": "Relevance",
            "response_groups": self.RESPONSE_GROUPS,
        }
        if title:
            params["title"] = title
        if author:
            params["author"] = author
        if isbn and not title and not author:
            params["keywords"] = isbn
        if not any(key in params for key in ("title", "author", "keywords")):
            return []

        data = await self._get_json("/1.0/catalog/products", params)
        if not data:
            return []

        identities: list[ProviderIdentity] = []
        for product in data.get("products") or []:
            if isinstance(product, dict):
                identity = self._parse_product(product)
                if identity is not None:
                    identities.append(identity)
        return identities[:limit]

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Get audiobook details by Audible ASIN."""
        asin = external_id.strip().upper()
        if not asin:
            return None
        data = await self._get_json(
            f"/1.0/catalog/products/{asin}",
            {"response_groups": self.RESPONSE_GROUPS},
        )
        product = data.get("product") if data else None
        if not isinstance(product, dict):
            return None
        return self._parse_product(product)

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

    def _parse_product(self, product: dict[str, Any]) -> ProviderIdentity | None:
        """Convert a catalog API product into a ProviderIdentity."""
        asin = str(product.get("asin") or "").strip().upper()
        title = self._clean_text(str(product.get("title") or ""))
        if not asin or not title:
            return None

        def names(key: str) -> list[str]:
            people = product.get(key) or []
            return [
                self._clean_text(str(person.get("name")))
                for person in people
                if isinstance(person, dict) and person.get("name")
            ]

        authors = filter_author_names(names("authors"))
        narrators = names("narrators")

        series_name = None
        series_index = None
        series = product.get("series") or []
        if isinstance(series, list) and series and isinstance(series[0], dict):
            series_name = self._clean_text(str(series[0].get("title") or "")) or None
            sequence = series[0].get("sequence")
            series_index = str(sequence).strip() if sequence else None

        year = None
        release = str(product.get("release_date") or product.get("issue_date") or "")
        year_match = re.match(r"(\d{4})", release)
        if year_match:
            year = int(year_match.group(1))

        runtime = product.get("runtime_length_min")
        runtime_minutes = runtime if isinstance(runtime, int) and runtime > 0 else None

        images = product.get("product_images") or {}
        cover_urls: list[str] = []
        if isinstance(images, dict) and images:
            largest = max(images, key=lambda size: int(size) if size.isdigit() else 0)
            cover_urls.append(str(images[largest]))

        format_type = product.get("format_type")
        description = product.get("merchandising_summary") or product.get(
            "publisher_summary"
        )

        return ProviderIdentity(
            provider=self.name,
            external_id=asin,
            title=title,
            authors=authors,
            narrator=", ".join(narrators) if narrators else None,
            series_name=series_name,
            series_index=series_index,
            year=year,
            language=product.get("language") or None,
            publisher=product.get("publisher_name") or None,
            description=self._clean_text(re.sub(r"<[^>]+>", " ", str(description)))
            if description
            else None,
            edition=str(format_type) if format_type else None,
            asin=asin,
            runtime_minutes=runtime_minutes,
            cover_urls=cover_urls,
            raw_data={
                "marketplace": self.marketplace,
                "product_url": f"https://www.{self.base_domain}/pd/{asin}",
                "subtitle": product.get("subtitle"),
                "narrator": ", ".join(narrators) if narrators else None,
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
