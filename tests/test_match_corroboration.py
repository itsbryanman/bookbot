"""Narrator, language and contributor-role corroboration in match scoring."""

import asyncio
from pathlib import Path

from bookbot.core.matching import AdvancedMatcher
from bookbot.core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    ReviewFlagKind,
)
from bookbot.core.review import apply_ambiguous_top2_flag
from bookbot.providers.audible import AudibleProvider
from bookbot.providers.base import MetadataProvider
from bookbot.providers.manager import ProviderManager


class _StaticProvider(MetadataProvider):
    def __init__(self, name: str, identities: list[ProviderIdentity]) -> None:
        super().__init__(name)
        self._identities = identities

    async def search(
        self,
        *,
        title=None,
        author=None,
        series=None,
        isbn=None,
        year=None,
        language=None,
        limit=10,
    ) -> list[ProviderIdentity]:
        return list(self._identities)

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


def _manager(providers: list[MetadataProvider]) -> ProviderManager:
    manager = object.__new__(ProviderManager)
    manager.providers = {f"p{i}": p for i, p in enumerate(providers)}
    manager.config_manager = None
    manager.cache_manager = None
    manager._consecutive_failures = {}
    manager.get_enabled_providers = lambda: list(providers)  # type: ignore[method-assign]
    return manager


def _gone_girl_set(**overrides) -> AudiobookSet:
    values = {
        "source_path": Path("/tmp/gone girl - Copy"),
        "raw_title_guess": "Gone Girl",
        "author_guess": "Gillian Flynn",
        "narrator_guess": "Julia Whelan, Kirby Heyborne",
        "total_duration": 1151.5 * 60,
    }
    values.update(overrides)
    return AudiobookSet(**values)


def _identity(external_id: str, **overrides) -> ProviderIdentity:
    values = {
        "provider": "audible",
        "external_id": external_id,
        "title": "Gone Girl",
        "authors": ["Gillian Flynn"],
        "asin": external_id,
    }
    values.update(overrides)
    return ProviderIdentity(**values)


SWEDISH = _identity(
    "B0HLT397NH",
    narrator="Anna Maria Käll",
    runtime_minutes=1168,
    language="swedish",
)
ENGLISH = _identity(
    "B0085XRNWM",
    narrator="Julia Whelan, Kirby Heyborne",
    runtime_minutes=1197,
    language="english",
)


def _run(audiobook_set: AudiobookSet, identities: list[ProviderIdentity]):
    manager = _manager([_StaticProvider("audible", identities)])
    return asyncio.run(manager.find_matches_merged(audiobook_set))


# 1. Contributor roles


def test_audible_parse_drops_contributor_roles_from_authors() -> None:
    identity = AudibleProvider()._parse_product(
        {
            "asin": "B0HLT397NH",
            "title": "Gone Girl",
            "authors": [
                {"name": "Gillian Flynn"},
                {"name": "Ulla Danielsson - translator"},
                {"name": "Some One - foreword"},
                {"name": "Other Person - Introduction"},
                {"name": "Ed Itor - editor"},
            ],
            "narrators": [{"name": "Anna Maria Käll"}],
        }
    )
    assert identity is not None
    assert identity.authors == ["Gillian Flynn"]


def test_audible_parse_keeps_authors_when_only_roles_listed() -> None:
    identity = AudibleProvider()._parse_product(
        {
            "asin": "B000000001",
            "title": "Anthology",
            "authors": [{"name": "Jane Doe - editor"}],
        }
    )
    assert identity is not None
    assert identity.authors == ["Jane Doe"]


# 2. Narrator corroboration


def test_narrator_match_boosts_and_mismatch_penalizes() -> None:
    plain_set = _gone_girl_set(narrator_guess=None, total_duration=None)
    baseline = {c.identity.asin: c.confidence for c in _run(plain_set, [ENGLISH])}

    with_narrator = _gone_girl_set(total_duration=None)
    english = _run(with_narrator, [ENGLISH])[0]
    assert english.confidence > baseline[ENGLISH.asin]
    assert "Narrator match" in english.match_reasons

    other = ENGLISH.model_copy(update={"narrator": "Somebody Else"})
    mismatched = _run(with_narrator, [other])[0]
    assert mismatched.confidence < baseline[ENGLISH.asin]
    assert "Narrator mismatch" in mismatched.match_reasons


# 3. Language


def test_known_local_language_penalizes_different_candidate_language() -> None:
    audiobook_set = _gone_girl_set(
        narrator_guess=None, total_duration=None, language_guess="en"
    )
    swedish_only = SWEDISH.model_copy(update={"narrator": None})
    english_only = ENGLISH.model_copy(update={"narrator": None})
    results = _run(audiobook_set, [swedish_only, english_only])
    assert results[0].identity.asin == ENGLISH.asin
    swedish = next(c for c in results if c.identity.asin == SWEDISH.asin)
    assert "Language mismatch" in swedish.match_reasons


def test_unknown_language_ranks_non_english_below_equal_english() -> None:
    audiobook_set = _gone_girl_set(narrator_guess=None, total_duration=None)
    swedish_only = SWEDISH.model_copy(update={"narrator": None})
    english_only = ENGLISH.model_copy(update={"narrator": None})
    results = _run(audiobook_set, [swedish_only, english_only])
    assert results[0].identity.asin == ENGLISH.asin
    assert results[0].confidence - results[1].confidence <= 0.05


def test_gone_girl_english_whelan_edition_wins_over_swedish() -> None:
    results = _run(_gone_girl_set(), [SWEDISH, ENGLISH])
    top = results[0]
    assert top.identity.asin == ENGLISH.asin
    assert all(
        flag.severity != "block"
        for flag in top.review_flags
        if flag.kind == ReviewFlagKind.AMBIGUOUS_TOP2
    )


# 4. ambiguous_top2


def _candidate(identity: ProviderIdentity, confidence: float) -> MatchCandidate:
    return MatchCandidate(
        identity=identity,
        confidence=confidence,
        confidence_level=MatchConfidence.HIGH,
        match_reasons=[],
    )


def _ambiguous(candidates: list[MatchCandidate]) -> list[str]:
    return [
        flag.severity
        for flag in candidates[0].review_flags
        if flag.kind == ReviewFlagKind.AMBIGUOUS_TOP2
    ]


def test_ambiguous_top2_same_work_different_edition_is_warn() -> None:
    candidates = apply_ambiguous_top2_flag(
        [
            _candidate(_identity("B075F8MBMQ", title="Educated"), 0.90),
            _candidate(_identity("B079C1R3S2", title="Educated"), 0.90),
        ]
    )
    assert _ambiguous(candidates) == ["warn"]


def test_ambiguous_top2_not_raised_when_top_better_corroborated() -> None:
    audiobook_set = AudiobookSet(
        source_path=Path("/tmp/Stormbreaker"),
        raw_title_guess="Stormbreaker",
        total_duration=295 * 60,
    )
    top = _identity(
        "1528896769",
        title="Stormbreaker",
        authors=["Anthony Horowitz"],
        runtime_minutes=292,
    )
    runner_up = _identity(
        "B002V8MKTS",
        title="Other Book",
        authors=["Someone Else"],
        runtime_minutes=277,
    )
    candidates = apply_ambiguous_top2_flag(
        [_candidate(top, 1.0), _candidate(runner_up, 1.0)],
        audiobook_set=audiobook_set,
        matcher=AdvancedMatcher(),
    )
    assert _ambiguous(candidates) == []


def test_ambiguous_top2_still_blocks_different_works() -> None:
    candidates = apply_ambiguous_top2_flag(
        [
            _candidate(_identity("A1", title="Book One", authors=["Ann"]), 0.90),
            _candidate(_identity("A2", title="Book Two", authors=["Bob"]), 0.89),
        ]
    )
    assert _ambiguous(candidates) == ["block"]


def test_stormbreaker_runtime_match_breaks_capped_tie() -> None:
    audiobook_set = AudiobookSet(
        source_path=Path("/tmp/sb"),
        raw_title_guess="Stormbreaker",
        author_guess="Anthony Horowitz",
        total_duration=295 * 60,
    )
    parker = _identity(
        "B002V8MKTS",
        title="Stormbreaker",
        authors=["Anthony Horowitz"],
        narrator="Nathaniel Parker",
        runtime_minutes=277,
    )
    chris = _identity(
        "1528896769",
        title="Stormbreaker",
        authors=["Anthony Horowitz"],
        narrator="Oliver Chris",
        runtime_minutes=292,
    )
    results = _run(audiobook_set, [parker, chris])
    assert results[0].identity.asin == "1528896769"
    assert "block" not in _ambiguous(results)
