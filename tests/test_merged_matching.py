"""Tests for Phase 3: multi-provider merged matching."""

import asyncio
from pathlib import Path
from unittest.mock import patch

from bookbot.core.models import (
    AudiobookSet,
    ProviderIdentity,
)
from bookbot.providers.base import MetadataProvider
from bookbot.providers.manager import ProviderManager

# ── Helper mock providers ──


class _MockProviderA(MetadataProvider):
    def __init__(self) -> None:
        super().__init__("ProviderA")

    async def search(
        self, *, title=None, author=None, series=None,
        isbn=None, year=None, language=None, limit=10,
    ) -> list[ProviderIdentity]:
        return [
            ProviderIdentity(
                provider="ProviderA",
                external_id="a1",
                title="The Stand",
                authors=["Stephen King"],
                isbn_13="9780385199575",
                year=1978,
            ),
        ]

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


class _MockProviderB(MetadataProvider):
    def __init__(self) -> None:
        super().__init__("ProviderB")

    async def search(
        self, *, title=None, author=None, series=None,
        isbn=None, year=None, language=None, limit=10,
    ) -> list[ProviderIdentity]:
        return [
            ProviderIdentity(
                provider="ProviderB",
                external_id="b1",
                title="The Stand",
                authors=["Stephen King"],
                isbn_13="9780385199575",
                narrator="Grover Gardner",
                runtime_minutes=960,
            ),
        ]

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


class _FailingProvider(MetadataProvider):
    def __init__(self) -> None:
        super().__init__("FailingProvider")

    async def search(
        self, *, title=None, author=None, series=None,
        isbn=None, year=None, language=None, limit=10,
    ) -> list[ProviderIdentity]:
        raise RuntimeError("Provider down")

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


def _make_audiobook(
    title: str = "The Stand",
    author: str = "Stephen King",
    total_duration: float | None = None,
) -> AudiobookSet:
    return AudiobookSet(
        source_path=Path("/tmp/test"),
        raw_title_guess=title,
        author_guess=author,
        total_duration=total_duration,
    )


def _create_manager_with_providers(
    providers: list[MetadataProvider],
) -> ProviderManager:
    """Create a ProviderManager with injected providers, bypassing config."""
    manager = object.__new__(ProviderManager)
    manager.providers = {f"p{i}": p for i, p in enumerate(providers)}
    manager.config_manager = None
    manager.cache_manager = None
    return manager


# ── Merge groups by ISBN across two providers ──


class TestMergeByISBN:
    def test_merge_candidates_with_same_isbn(self) -> None:
        manager = _create_manager_with_providers([
            _MockProviderA(), _MockProviderB()
        ])

        # Patch get_enabled_providers to return our mocks
        with patch.object(
            manager, "get_enabled_providers",
            return_value=list(manager.providers.values()),
        ):
            candidates = asyncio.get_event_loop().run_until_complete(
                manager.find_matches_merged(_make_audiobook())
            )

        # Should merge into one candidate (same ISBN-13)
        assert len(candidates) == 1
        c = candidates[0]
        # Should have the first provider's identity as base
        assert c.identity.provider == "ProviderA"
        # Backfilled narrator from ProviderB
        assert c.identity.narrator == "Grover Gardner"
        # Backfilled year from ProviderA (1978)
        assert c.identity.year == 1978
        # Backfilled runtime from ProviderB
        assert c.identity.runtime_minutes == 960


# ── Corroboration boost ──


class TestCorroborationBoost:
    def test_two_provider_boost(self) -> None:
        manager = _create_manager_with_providers([
            _MockProviderA(), _MockProviderB()
        ])

        with patch.object(
            manager, "get_enabled_providers",
            return_value=list(manager.providers.values()),
        ):
            candidates = asyncio.get_event_loop().run_until_complete(
                manager.find_matches_merged(_make_audiobook())
            )

        c = candidates[0]
        # With 2 providers, boost = 0.07 * (2-1) = 0.07
        assert "Corroborated by 2 providers" in c.match_reasons
        # Confidence should be higher than the max individual score
        # (individual max is about 0.8 for title+author only)
        assert c.confidence >= 0.8


# ── Duration cross-check ──


class TestDurationCheck:
    def test_runtime_match_boost(self) -> None:
        # ProviderB returns runtime_minutes=960 (=57600 seconds)
        # Set total_duration close to that (within 2%)
        audiobook = _make_audiobook(total_duration=57600.0)
        manager = _create_manager_with_providers([_MockProviderB()])

        with patch.object(
            manager, "get_enabled_providers",
            return_value=list(manager.providers.values()),
        ):
            candidates = asyncio.get_event_loop().run_until_complete(
                manager.find_matches_merged(audiobook)
            )

        c = candidates[0]
        assert "Runtime match" in c.match_reasons

    def test_runtime_mismatch_penalty(self) -> None:
        # 200 minutes of local audio vs 960 minutes from provider — big mismatch
        audiobook = _make_audiobook(total_duration=12000.0)  # 200 minutes
        manager = _create_manager_with_providers([_MockProviderB()])

        with patch.object(
            manager, "get_enabled_providers",
            return_value=list(manager.providers.values()),
        ):
            candidates = asyncio.get_event_loop().run_until_complete(
                manager.find_matches_merged(audiobook)
            )

        c = candidates[0]
        assert "Runtime mismatch (possible wrong edition/abridged)" in c.match_reasons


# ── Failing provider does not sink the merge ──


class TestFailingProvider:
    def test_one_provider_raises(self) -> None:
        manager = _create_manager_with_providers([
            _MockProviderA(), _FailingProvider()
        ])

        with patch.object(
            manager, "get_enabled_providers",
            return_value=list(manager.providers.values()),
        ):
            candidates = asyncio.get_event_loop().run_until_complete(
                manager.find_matches_merged(_make_audiobook())
            )

        # Should still get results from ProviderA
        assert len(candidates) >= 1
        assert candidates[0].identity.provider == "ProviderA"


# ── Fallback ladder ──


class _EmptyThenTitleProvider(MetadataProvider):
    """Returns empty for title+author, results for title-only."""

    def __init__(self) -> None:
        super().__init__("EmptyThenTitle")
        self.search_calls: list[dict] = []

    async def search(
        self, *, title=None, author=None, series=None,
        isbn=None, year=None, language=None, limit=10,
    ) -> list[ProviderIdentity]:
        self.search_calls.append({
            "title": title, "author": author,
            "series": series, "isbn": isbn,
        })
        # Return results only when searching by title alone (fallback b)
        if title and not author and not series and not isbn:
            return [
                ProviderIdentity(
                    provider="EmptyThenTitle",
                    external_id="f1",
                    title="The Stand",
                    authors=["Stephen King"],
                ),
            ]
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


class TestFallbackLadder:
    def test_fallback_to_title_only(self) -> None:
        provider = _EmptyThenTitleProvider()
        audiobook = _make_audiobook()

        candidates = asyncio.get_event_loop().run_until_complete(
            provider.find_matches(audiobook)
        )

        # Should have found results via fallback
        assert len(candidates) >= 1
        # Should have made multiple search calls
        assert len(provider.search_calls) >= 2
        # The last successful call should be title-only
        last_call = provider.search_calls[-1]
        assert last_call["title"] is not None
