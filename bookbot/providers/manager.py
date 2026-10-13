"""Provider manager for handling multiple metadata sources."""

import asyncio
import re
from collections.abc import Iterable

from ..config.manager import ConfigManager
from ..core.exceptions import MetadataError, ProviderUnavailableError
from ..core.logging import get_logger
from ..core.matching import AdvancedMatcher
from ..core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)
from ..core.review import (
    RUNTIME_MATCH_TOLERANCE,
    apply_ambiguous_top2_flag,
    detect_review_flags,
    narrator_agreement,
    runtime_relative_error,
)
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

    # After this many consecutive unreachable/timed-out lookups a provider is
    # skipped for the rest of the run, so one dead service costs a few
    # connect timeouts in total instead of one per book.
    CIRCUIT_BREAKER_THRESHOLD = 3

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.cache_manager = CacheManager(config_manager)
        self.providers: dict[str, MetadataProvider] = {}
        self._consecutive_failures: dict[str, int] = {}
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

    def _failure_counts(self) -> dict[str, int]:
        # Managers built without __init__ (tests, embedding) start clean.
        counts = getattr(self, "_consecutive_failures", None)
        if counts is None:
            counts = {}
            self._consecutive_failures = counts
        return counts

    def is_provider_tripped(self, provider: MetadataProvider) -> bool:
        """Whether a provider is being skipped after repeated outages."""
        return (
            self._failure_counts().get(provider.name, 0)
            >= self.CIRCUIT_BREAKER_THRESHOLD
        )

    def _record_provider_outcome(
        self, provider: MetadataProvider, error: BaseException | None
    ) -> None:
        counts = self._failure_counts()
        if error is None:
            counts[provider.name] = 0
            return
        if not self._is_outage(error):
            return
        counts[provider.name] = counts.get(provider.name, 0) + 1
        if counts[provider.name] == self.CIRCUIT_BREAKER_THRESHOLD:
            logger.warning(
                f"Provider {provider.name} unreachable "
                f"{self.CIRCUIT_BREAKER_THRESHOLD} times in a row; "
                "skipping it for the rest of this run",
                error=str(error),
            )

    @staticmethod
    def _can_search(provider: MetadataProvider, audiobook_set: AudiobookSet) -> bool:
        can_search = getattr(provider, "can_search", None)
        return bool(can_search(audiobook_set)) if callable(can_search) else True

    @staticmethod
    def _is_outage(error: BaseException) -> bool:
        if isinstance(error, ProviderUnavailableError):
            return True
        if isinstance(error, MetadataError):
            return error.details.get("kind") in {"timeout", "network"}
        return False

    async def find_matches_merged(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        """Fan out find_matches to all enabled providers, merge candidates.

        Merge groups by isbn_13, then asin, then (normalized_author, normalized_title).
        Applies corroboration boost and duration cross-check.

        Providers that were unreachable several times in a row are skipped.
        Raises :class:`MetadataError` ("All metadata providers unavailable")
        when no provider could be asked, so callers can report an outage
        instead of "no matches".
        """
        enabled = self.get_enabled_providers()
        if not enabled:
            return []

        # Providers that cannot look this set up (Audnexus without an ASIN)
        # are left out entirely: their empty answer says nothing about
        # whether the network is reachable.
        applicable = [p for p in enabled if self._can_search(p, audiobook_set)]
        if not applicable:
            return []
        active = [p for p in applicable if not self.is_provider_tripped(p)]
        skipped = [p.name for p in applicable if self.is_provider_tripped(p)]
        provider_failures: list[dict[str, object]] = [
            {"provider": name, "error": "skipped after repeated connection failures"}
            for name in skipped
        ]

        # Fan out concurrently
        tasks = [
            self._find_matches_with_timeout(p, audiobook_set, limit) for p in active
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all candidates, tagging each with its provider priority
        all_candidates: list[tuple[int, MatchCandidate]] = []
        for i, result in enumerate(results):
            provider = active[i]
            if isinstance(result, BaseException):
                self._record_provider_outcome(provider, result)
                provider_failures.append(
                    {
                        "provider": provider.name,
                        "error": str(result),
                    }
                )
                logger.warning(
                    f"Provider {provider.name} failed during find_matches",
                    error=str(result),
                )
                continue
            self._record_provider_outcome(provider, None)
            for candidate in result:
                all_candidates.append((i, candidate))  # i = priority index

        if not all_candidates:
            if provider_failures and len(provider_failures) == len(applicable):
                raise MetadataError(
                    "All metadata providers unavailable",
                    details={"provider_errors": provider_failures},
                    recoverable=True,
                )
            return []

        matcher = AdvancedMatcher()
        marketplace = self._marketplace()
        groups: dict[str, list[tuple[int, MatchCandidate]]] = {}

        for priority, candidate in all_candidates:
            key = self._group_key(candidate.identity, matcher)
            groups.setdefault(key, []).append((priority, candidate))

        compatible_groups: list[list[tuple[int, MatchCandidate]]] = []
        for group in groups.values():
            for priority, candidate in group:
                for compatible in compatible_groups:
                    if self._group_key(
                        compatible[0][1].identity, matcher
                    ) != self._group_key(candidate.identity, matcher):
                        continue
                    if all(
                        not self._edition_metadata_conflicts(
                            existing.identity, candidate.identity, matcher
                        )
                        for _, existing in compatible
                    ):
                        compatible.append((priority, candidate))
                        break
                else:
                    compatible_groups.append([(priority, candidate)])

        merged: list[MatchCandidate] = []
        for group_members, group_reasons in self._merge_related_groups(
            compatible_groups, matcher
        ):
            surviving = self._merge_group(group_members, extra_reasons=group_reasons)
            surviving = self._apply_duration_check(
                surviving, audiobook_set.total_duration
            )
            surviving = self._apply_narrator_check(
                surviving, audiobook_set.narrator_guess, matcher
            )
            surviving = self._apply_language_check(
                surviving, audiobook_set.language_guess, marketplace
            )
            surviving.review_flags = detect_review_flags(
                audiobook_set, surviving, matcher
            )
            merged.append(surviving)

        merged.sort(
            key=lambda c: self._ranking_key(c, audiobook_set, matcher), reverse=True
        )
        merged = apply_ambiguous_top2_flag(merged, audiobook_set, matcher)
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
        if cls._edition_metadata_conflicts(left, right, matcher):
            return False
        if matcher.match_title(left.title, right.title) < 0.92:
            return False
        if cls._author_similarity(left, right, matcher) < 0.85:
            return False
        return True

    @classmethod
    def _edition_metadata_conflicts(
        cls,
        left: ProviderIdentity,
        right: ProviderIdentity,
        matcher: AdvancedMatcher,
    ) -> bool:
        """Reject merges that would collapse distinct audiobook editions."""
        if left.narrator and right.narrator:
            left_narrators = cls._split_people(left.narrator)
            right_narrators = cls._split_people(right.narrator)
            if not any(
                matcher.match_author(left_name, right_name) >= 0.85
                for left_name in left_narrators
                for right_name in right_narrators
            ):
                return True

        if left.language and right.language:
            if cls._normalize_language(left.language) != cls._normalize_language(
                right.language
            ):
                return True

        left_edition = cls._normalize_edition(left.edition)
        right_edition = cls._normalize_edition(right.edition)
        if left_edition and right_edition and left_edition != right_edition:
            return True

        return False

    @staticmethod
    def _split_people(value: str) -> list[str]:
        return [
            name.strip()
            for name in re.split(r"\s*(?:,|;|&|\band\b)\s*", value, flags=re.I)
            if name.strip()
        ]

    @staticmethod
    def _normalize_language(value: str) -> str:
        normalized = value.strip().casefold().replace("_", "-")
        aliases = {
            "eng": "en",
            "english": "en",
            "deu": "de",
            "ger": "de",
            "german": "de",
            "fra": "fr",
            "fre": "fr",
            "french": "fr",
            "spa": "es",
            "spanish": "es",
            "swe": "sv",
            "swedish": "sv",
            "ita": "it",
            "italian": "it",
            "dut": "nl",
            "nld": "nl",
            "dutch": "nl",
            "dan": "da",
            "danish": "da",
            "nor": "no",
            "nob": "no",
            "norwegian": "no",
            "fin": "fi",
            "finnish": "fi",
            "por": "pt",
            "portuguese": "pt",
            "pol": "pl",
            "polish": "pl",
            "rus": "ru",
            "russian": "ru",
            "jpn": "ja",
            "japanese": "ja",
            "chi": "zh",
            "zho": "zh",
            "chinese": "zh",
        }
        return aliases.get(normalized, normalized.split("-", 1)[0])

    @staticmethod
    def _normalize_edition(value: str | None) -> str | None:
        if not value:
            return None
        normalized = " ".join(value.casefold().split())
        if "unabridged" in normalized:
            return "unabridged"
        if "abridged" in normalized:
            return "abridged"
        return normalized

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

    @classmethod
    def _apply_duration_check(
        cls, candidate: MatchCandidate, total_duration: float | None
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

        return cls._rescored(candidate, conf, reasons)

    @staticmethod
    def _rescored(
        candidate: MatchCandidate, conf: float, reasons: list[str]
    ) -> MatchCandidate:
        """Return the candidate with an adjusted confidence and reasons."""
        if conf == candidate.confidence:
            return candidate
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
            review_flags=candidate.review_flags,
        )

    @classmethod
    def _apply_narrator_check(
        cls,
        candidate: MatchCandidate,
        local_narrator: str | None,
        matcher: AdvancedMatcher,
    ) -> MatchCandidate:
        """Boost a narrator overlap with the local guess, penalize a clash."""
        agreement = narrator_agreement(
            local_narrator, candidate.identity.narrator, matcher
        )
        if agreement is None:
            return candidate
        reasons = list(candidate.match_reasons)
        if agreement:
            reasons.append("Narrator match")
            return cls._rescored(
                candidate, min(1.0, candidate.confidence + 0.05), reasons
            )
        reasons.append("Narrator mismatch")
        return cls._rescored(candidate, candidate.confidence * 0.92, reasons)

    @classmethod
    def _apply_language_check(
        cls,
        candidate: MatchCandidate,
        local_language: str | None,
        marketplace: str | None,
    ) -> MatchCandidate:
        """Penalize editions in a language other than the local one.

        With no local language known, a non-English edition only drops
        slightly on English-language marketplaces, enough to rank it below
        an otherwise equal English one.
        """
        provider_language = candidate.identity.language
        if not provider_language:
            return candidate
        provider_code = cls._normalize_language(provider_language)
        reasons = list(candidate.match_reasons)
        if local_language:
            if cls._normalize_language(local_language) == provider_code:
                return candidate
            reasons.append("Language mismatch")
            return cls._rescored(candidate, candidate.confidence * 0.85, reasons)
        if provider_code != "en" and (marketplace or "us").casefold() in {
            "us",
            "uk",
        }:
            reasons.append("Non-English edition")
            return cls._rescored(
                candidate, max(0.0, candidate.confidence - 0.02), reasons
            )
        return candidate

    def _marketplace(self) -> str | None:
        if self.config_manager is None:
            return None
        try:
            return str(self.config_manager.load_config().providers.audible.marketplace)
        except Exception:  # noqa: BLE001 - scoring must not fail on config
            return None

    @staticmethod
    def _ranking_key(
        candidate: MatchCandidate,
        audiobook_set: AudiobookSet,
        matcher: AdvancedMatcher,
    ) -> tuple[float, int, float]:
        """Order by confidence, then by local corroboration on ties.

        Confidence is capped at 1.0, so two strong candidates can tie even
        when only one of them matches the local narrator or runtime.
        """
        corroboration = 0
        if narrator_agreement(
            audiobook_set.narrator_guess, candidate.identity.narrator, matcher
        ):
            corroboration += 1
        error = runtime_relative_error(audiobook_set, candidate)
        if error is not None and error <= RUNTIME_MATCH_TOLERANCE:
            corroboration += 1
        closeness = -error if error is not None else -1.0
        return (round(candidate.confidence, 6), corroboration, closeness)

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
