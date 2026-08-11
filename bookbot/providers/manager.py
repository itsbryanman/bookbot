"""Provider manager for handling multiple metadata sources."""

import asyncio
from collections.abc import Iterable

from ..config.manager import ConfigManager
from ..core.exceptions import MetadataError
from ..core.logging import get_logger
from ..core.matching import AdvancedMatcher
from ..core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)
from ..core.review import apply_ambiguous_top2_flag, detect_review_flags
from ..io.cache import CacheManager
from .audible import AudibleProvider
from .audnexus import AudnexusProvider
from .base import MetadataProvider
from .googlebooks import GoogleBooksProvider
from .hardcover import HardcoverProvider
from .librivox import LibriVoxProvider
from .openlibrary import OpenLibraryProvider

logger = get_logger("provider_manager")


class ProviderManager:
    """Manages multiple metadata providers."""

    AUDIO_PROVIDERS = frozenset({"audible", "audnexus", "librivox"})
    PRINT_PROVIDERS = frozenset({"openlibrary", "googlebooks", "hardcover"})

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.cache_manager = CacheManager(config_manager)
        self.providers: dict[str, MetadataProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize enabled providers from the current config."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        if provider_config.openlibrary.enabled:
            self.providers["openlibrary"] = OpenLibraryProvider(
                cache_manager=self.cache_manager
            )
            logger.info("Initialized Open Library provider")

        # Audnexus - no API key needed, enabled by default
        if provider_config.audnexus.enabled:
            marketplace = provider_config.audnexus.marketplace
            self.providers["audnexus"] = AudnexusProvider(
                marketplace=marketplace,
                cache_manager=self.cache_manager,
            )
            logger.info(f"Initialized Audnexus provider (marketplace: {marketplace})")

        # Optional providers only if configured
        google_books_config = provider_config.google_books
        if google_books_config.enabled and google_books_config.api_key:
            self.providers["googlebooks"] = GoogleBooksProvider(
                api_key=google_books_config.api_key,
                cache_manager=self.cache_manager,
            )
            logger.info("Initialized Google Books provider")

        if provider_config.librivox.enabled:
            self.providers["librivox"] = LibriVoxProvider(
                cache_manager=self.cache_manager
            )
            logger.info("Initialized LibriVox provider")

        if provider_config.audible.enabled:
            marketplace = provider_config.audible.marketplace
            self.providers["audible"] = AudibleProvider(
                marketplace=marketplace,
                cache_manager=self.cache_manager,
            )
            logger.info(f"Initialized Audible provider (marketplace: {marketplace})")

        # Hardcover - requires API key
        hardcover_config = provider_config.hardcover
        if hardcover_config.enabled and hardcover_config.api_key:
            self.providers["hardcover"] = HardcoverProvider(
                api_key=hardcover_config.api_key,
                cache_manager=self.cache_manager,
            )
            logger.info("Initialized Hardcover provider")

    def get_enabled_providers(self) -> list[MetadataProvider]:
        """Get enabled providers in effective priority order."""
        config = self.config_manager.load_config()
        enabled: list[MetadataProvider] = []
        seen: set[str] = set()

        for provider_name in config.providers.priority_order:
            if provider_name in self.providers:
                enabled.append(self.providers[provider_name])
                seen.add(provider_name)

        for provider_name, provider in self.providers.items():
            if provider_name in seen:
                continue
            enabled.append(provider)

        return enabled

    def get_provider(self, name: str) -> MetadataProvider | None:
        """Get a specific provider by name."""
        return self.providers.get(name.lower())

    def get_primary_provider(self) -> MetadataProvider | None:
        """Get the primary (highest priority) provider."""
        enabled = self.get_enabled_providers()
        return enabled[0] if enabled else None

    def _request_timeout_seconds(self) -> float:
        """Return the configured per-provider request timeout."""
        if self.config_manager is None:
            return max(1.0, float(getattr(self, "request_timeout", 15)))

        timeout = self.config_manager.load_config().providers.request_timeout
        return max(1.0, float(timeout or 15))

    async def _find_matches_with_timeout(
        self,
        provider: MetadataProvider,
        audiobook_set: AudiobookSet,
        limit: int,
    ) -> list[MatchCandidate]:
        """Bound provider match lookups so unavailable services fail cleanly."""
        timeout = self._request_timeout_seconds()
        try:
            return await asyncio.wait_for(
                provider.find_matches(audiobook_set, limit),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MetadataError(
                f"Provider {provider.name} timed out after {timeout:.0f}s",
                details={
                    "provider": provider.name,
                    "timeout": timeout,
                    "kind": "timeout",
                },
                recoverable=True,
            ) from exc
        except MetadataError:
            raise
        except Exception as exc:
            raise MetadataError(
                f"Provider {provider.name} unavailable: {exc}",
                details={
                    "provider": provider.name,
                    "error": str(exc),
                    "kind": "network",
                },
                recoverable=True,
            ) from exc

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        """Fan out find_matches to all enabled providers, merge candidates.

        Merge groups by isbn_13, then asin, then (normalized_author, normalized_title).
        Applies corroboration boost and duration cross-check.
        """
        enabled = self.get_enabled_providers()
        if not enabled:
            return []

        # Fan out concurrently
        tasks = [
            self._find_matches_with_timeout(p, audiobook_set, limit) for p in enabled
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all candidates, tagging each with its provider priority
        all_candidates: list[tuple[int, MatchCandidate]] = []
        provider_failures: list[dict[str, object]] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                provider_name = enabled[i].name
                provider_failures.append(
                    {
                        "provider": provider_name,
                        "error": str(result),
                    }
                )
                logger.warning(
                    f"Provider {provider_name} failed during find_matches",
                    error=str(result),
                )
                continue
            for candidate in result:
                all_candidates.append((i, candidate))  # i = priority index

        if not all_candidates:
            if provider_failures and len(provider_failures) == len(enabled):
                raise MetadataError(
                    "All metadata providers unavailable",
                    details={"provider_errors": provider_failures},
                    recoverable=True,
                )
            return []

        matcher = AdvancedMatcher()
        groups: dict[str, list[tuple[int, MatchCandidate]]] = {}

        for priority, candidate in all_candidates:
            key = self._group_key(candidate.identity, matcher)
            groups.setdefault(key, []).append((priority, candidate))

        merged: list[MatchCandidate] = []
        for group_members, group_reasons in self._merge_related_groups(
            list(groups.values()), matcher
        ):
            surviving = self._merge_group(group_members, extra_reasons=group_reasons)
            surviving = self._apply_duration_check(
                surviving, audiobook_set.total_duration
            )
            surviving.review_flags = detect_review_flags(
                audiobook_set, surviving, matcher
            )
            merged.append(surviving)

        merged.sort(key=lambda c: c.confidence, reverse=True)
        merged = apply_ambiguous_top2_flag(merged)
        return merged[:limit]

    @staticmethod
    def _group_key(identity: ProviderIdentity, matcher: AdvancedMatcher) -> str:
        """Generate a grouping key for merging candidates."""
        if identity.isbn_13:
            return f"isbn13:{identity.isbn_13}"
        if identity.asin:
            return f"asin:{identity.asin}"
        norm_author = (
            matcher.normalize_author(identity.authors[0]) if identity.authors else ""
        )
        norm_title = matcher.normalize_title(identity.title)
        return f"fuzzy:{norm_author}|{norm_title}"

    def _merge_related_groups(
        self,
        groups: list[list[tuple[int, MatchCandidate]]],
        matcher: AdvancedMatcher,
    ) -> list[tuple[list[tuple[int, MatchCandidate]], list[str]]]:
        """Merge exact-key groups that clearly describe the same book."""
        pending = [list(group) for group in groups]
        merge_reasons: list[list[str]] = [[] for _ in pending]
        changed = True

        while changed:
            changed = False
            i = 0
            while i < len(pending):
                left_identity = self._merge_group(
                    pending[i], extra_reasons=merge_reasons[i]
                ).identity
                j = i + 1
                while j < len(pending):
                    right_identity = self._merge_group(
                        pending[j], extra_reasons=merge_reasons[j]
                    ).identity
                    if self._identities_can_merge(
                        left_identity, right_identity, matcher
                    ):
                        pending[i].extend(pending.pop(j))
                        reason = (
                            "Corroborated across audio + print providers"
                            if self._is_audio_print_corroboration(
                                left_identity, right_identity
                            )
                            else None
                        )
                        merged_reasons = merge_reasons.pop(j)
                        if reason:
                            merge_reasons[i].append(reason)
                        merge_reasons[i].extend(merged_reasons)
                        changed = True
                        left_identity = self._merge_group(
                            pending[i], extra_reasons=merge_reasons[i]
                        ).identity
                        continue
                    j += 1
                i += 1

        return list(zip(pending, merge_reasons, strict=False))

    def _merge_group(
        self,
        members: list[tuple[int, MatchCandidate]],
        extra_reasons: list[str] | None = None,
    ) -> MatchCandidate:
        """Merge a group of candidates into a single surviving candidate.

        Keeps the identity from the highest-priority provider (lowest index),
        backfills None fields from other members, and applies corroboration boost.
        """
        members.sort(key=lambda m: m[0])
        _best_priority, best_candidate = members[0]
        surviving_identity = best_candidate.identity.model_copy()

        provider_names: list[str] = []
        provider_seen: set[str] = set()
        confidence = best_candidate.confidence
        all_reasons: list[str] = list(best_candidate.match_reasons)
        if extra_reasons:
            all_reasons.extend(extra_reasons)

        for _priority, candidate in members:
            provider_name = candidate.identity.provider
            if provider_name not in provider_seen:
                provider_seen.add(provider_name)
                provider_names.append(provider_name)

            ident = candidate.identity
            if not surviving_identity.narrator and ident.narrator:
                surviving_identity.narrator = ident.narrator
            if not surviving_identity.year and ident.year:
                surviving_identity.year = ident.year
            if not surviving_identity.series_name and ident.series_name:
                surviving_identity.series_name = ident.series_name
            if not surviving_identity.series_index and ident.series_index:
                surviving_identity.series_index = ident.series_index
            if not surviving_identity.cover_urls and ident.cover_urls:
                surviving_identity.cover_urls = list(ident.cover_urls)
            if not surviving_identity.isbn_10 and ident.isbn_10:
                surviving_identity.isbn_10 = ident.isbn_10
            if not surviving_identity.isbn_13 and ident.isbn_13:
                surviving_identity.isbn_13 = ident.isbn_13
            if not surviving_identity.asin and ident.asin:
                surviving_identity.asin = ident.asin
            if not surviving_identity.runtime_minutes and ident.runtime_minutes:
                surviving_identity.runtime_minutes = ident.runtime_minutes

        n_providers = len(provider_names or [surviving_identity.provider])
        if n_providers >= 2:
            confidence = min(1.0, confidence + 0.07 * (n_providers - 1))
            all_reasons.append(f"Corroborated by {n_providers} providers")

        surviving_identity.raw_data = dict(surviving_identity.raw_data)
        surviving_identity.raw_data["providers"] = provider_names or [
            surviving_identity.provider
        ]

        seen: set[str] = set()
        unique_reasons: list[str] = []
        for r in all_reasons:
            if r not in seen:
                seen.add(r)
                unique_reasons.append(r)

        level = MatchConfidence.HIGH
        if confidence <= 0.85:
            level = MatchConfidence.MEDIUM
        if confidence <= 0.65:
            level = MatchConfidence.LOW

        return MatchCandidate(
            identity=surviving_identity,
            confidence=confidence,
            confidence_level=level,
            match_reasons=unique_reasons,
        )

    @classmethod
    def _identities_can_merge(
        cls,
        left: ProviderIdentity,
        right: ProviderIdentity,
        matcher: AdvancedMatcher,
    ) -> bool:
        """Check whether two exact-key groups corroborate the same title."""
        if cls._identifiers_conflict(left, right):
            return False
        if matcher.match_title(left.title, right.title) < 0.92:
            return False
        if cls._author_similarity(left, right, matcher) < 0.85:
            return False
        return True

    @staticmethod
    def _author_similarity(
        left: ProviderIdentity,
        right: ProviderIdentity,
        matcher: AdvancedMatcher,
    ) -> float:
        if not left.authors or not right.authors:
            return 0.0
        return max(
            matcher.match_author(left_author, right_author)
            for left_author in left.authors
            for right_author in right.authors
        )

    @staticmethod
    def _identifiers_conflict(left: ProviderIdentity, right: ProviderIdentity) -> bool:
        return bool(
            left.isbn_13 and right.isbn_13 and left.isbn_13 != right.isbn_13
        ) or bool(left.asin and right.asin and left.asin != right.asin)

    @classmethod
    def _is_audio_print_corroboration(
        cls, left: ProviderIdentity, right: ProviderIdentity
    ) -> bool:
        left_providers = cls._provider_names(left)
        right_providers = cls._provider_names(right)
        return (
            bool(left_providers & cls.AUDIO_PROVIDERS)
            and bool(right_providers & cls.PRINT_PROVIDERS)
        ) or (
            bool(left_providers & cls.PRINT_PROVIDERS)
            and bool(right_providers & cls.AUDIO_PROVIDERS)
        )

    @staticmethod
    def _provider_names(identity: ProviderIdentity) -> set[str]:
        providers = identity.raw_data.get("providers")
        if isinstance(providers, Iterable) and not isinstance(providers, str):
            return {str(provider).lower() for provider in providers}
        return {identity.provider.lower()}

    @staticmethod
    def _apply_duration_check(
        candidate: MatchCandidate, total_duration: float | None
    ) -> MatchCandidate:
        """Apply duration cross-check to adjust confidence."""
        if total_duration is None or total_duration <= 0:
            return candidate

        runtime_min = candidate.identity.runtime_minutes
        if runtime_min is None or runtime_min <= 0:
            return candidate

        # Convert total_duration (seconds) to minutes for comparison
        local_minutes = total_duration / 60.0
        relative_error = abs(local_minutes - runtime_min) / runtime_min

        reasons = list(candidate.match_reasons)
        conf = candidate.confidence

        if relative_error <= 0.02:
            conf = min(1.0, conf + 0.10)
            reasons.append("Runtime match")
        elif relative_error > 0.10:
            conf *= 0.6
            reasons.append("Runtime mismatch (possible wrong edition/abridged)")

        if conf != candidate.confidence:
            level = MatchConfidence.HIGH
            if conf <= 0.85:
                level = MatchConfidence.MEDIUM
            if conf <= 0.65:
                level = MatchConfidence.LOW

            return MatchCandidate(
                identity=candidate.identity,
                confidence=conf,
                confidence_level=level,
                match_reasons=reasons,
            )

        return candidate

    async def close_all(self) -> None:
        """Close all provider connections."""
        for provider in self.providers.values():
            if hasattr(provider, "close"):
                await provider.close()

    def list_providers(self) -> dict[str, dict[str, object]]:
        """List all available providers with their status."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        providers_info: dict[str, dict[str, object]] = {
            "openlibrary": {
                "name": "Open Library",
                "status": "enabled" if "openlibrary" in self.providers else "disabled",
                "description": "Free, comprehensive book database",
                "requires_api_key": "no",
            },
            "audnexus": {
                "name": "Audnexus",
                "status": "enabled" if "audnexus" in self.providers else "disabled",
                "description": "Audiobook metadata with chapter data (ASIN-based)",
                "requires_api_key": "no",
                "marketplace": provider_config.audnexus.marketplace,
            },
            "googlebooks": {
                "name": "Google Books",
                "status": "enabled" if "googlebooks" in self.providers else "disabled",
                "description": "Google's extensive book catalog",
                "requires_api_key": "yes",
                "api_key_provided": bool(provider_config.google_books.api_key),
            },
            "librivox": {
                "name": "LibriVox",
                "status": "enabled" if "librivox" in self.providers else "disabled",
                "description": "Public domain audiobooks",
                "requires_api_key": "no",
            },
            "audible": {
                "name": "Audible",
                "status": "enabled" if "audible" in self.providers else "disabled",
                "description": "Audible audiobook metadata",
                "requires_api_key": "no",
                "marketplace": provider_config.audible.marketplace,
            },
            "hardcover": {
                "name": "Hardcover",
                "status": "enabled" if "hardcover" in self.providers else "disabled",
                "description": "Hardcover book database (GraphQL API)",
                "requires_api_key": "yes",
                "api_key_provided": bool(provider_config.hardcover.api_key),
            },
        }

        return providers_info
