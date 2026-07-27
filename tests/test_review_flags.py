"""Tests for match review flag detection."""

from pathlib import Path

from bookbot.core.matching import AdvancedMatcher
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    AudioTags,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    ReviewFlagKind,
    Track,
)
from bookbot.core.review import apply_ambiguous_top2_flag, detect_review_flags


def _track(path: str, artist: str | None = None) -> Track:
    return Track(
        src_path=Path(path),
        disc=1,
        track_index=1,
        audio_format=AudioFormat.MP3,
        existing_tags=AudioTags(artist=artist),
    )


def _candidate(
    *,
    title: str,
    authors: list[str],
    narrator: str | None = None,
    isbn_13: str | None = None,
    asin: str | None = None,
    runtime_minutes: int | None = None,
) -> MatchCandidate:
    return MatchCandidate(
        identity=ProviderIdentity(
            provider="provider",
            external_id="candidate-1",
            title=title,
            authors=authors,
            narrator=narrator,
            isbn_13=isbn_13,
            asin=asin,
            runtime_minutes=runtime_minutes,
        ),
        confidence=0.93,
        confidence_level=MatchConfidence.HIGH,
        match_reasons=["stub"],
    )


def test_detects_narrator_as_author_mixup() -> None:
    audiobook = AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="The Way of Kings",
        author_guess="Brandon Sanderson",
        tracks=[
            _track("/tmp/book/01.mp3", "Brandon Sanderson, Michael Kramer"),
        ],
    )
    flags = detect_review_flags(
        audiobook,
        _candidate(
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            narrator="Michael Kramer",
        ),
        AdvancedMatcher(),
    )

    assert [flag.kind for flag in flags] == [ReviewFlagKind.NARRATOR_AS_AUTHOR]


def test_detects_author_as_narrator_mixup() -> None:
    audiobook = AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="The Way of Kings",
        author_guess="Brandon Sanderson",
        narrator_guess="Brandon Sanderson",
    )
    flags = detect_review_flags(
        audiobook,
        _candidate(
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            narrator="Michael Kramer",
        ),
        AdvancedMatcher(),
    )

    assert [flag.kind for flag in flags] == [ReviewFlagKind.AUTHOR_AS_NARRATOR]


def test_detects_author_conflict() -> None:
    audiobook = AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="The Left Hand of Darkness",
        author_guess="Ursula K. Le Guin",
    )
    flags = detect_review_flags(
        audiobook,
        _candidate(
            title="The Left Hand of Darkness",
            authors=["Stephen King"],
        ),
        AdvancedMatcher(),
    )

    assert [flag.kind for flag in flags] == [ReviewFlagKind.AUTHOR_CONFLICT]


def test_detects_title_conflict() -> None:
    audiobook = AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="Dune",
        author_guess="Frank Herbert",
    )
    flags = detect_review_flags(
        audiobook,
        _candidate(
            title="The Stand",
            authors=["Frank Herbert"],
        ),
        AdvancedMatcher(),
    )

    assert [flag.kind for flag in flags] == [ReviewFlagKind.TITLE_CONFLICT]


def test_detects_runtime_mismatch_and_abridged_suspect() -> None:
    audiobook = AudiobookSet(
        source_path=Path("/tmp/book"),
        raw_title_guess="Book",
        author_guess="Author",
        total_duration=600 * 60.0,
    )
    flags = detect_review_flags(
        audiobook,
        _candidate(
            title="Book",
            authors=["Author"],
            runtime_minutes=1000,
        ),
        AdvancedMatcher(),
    )

    assert {flag.kind for flag in flags} == {
        ReviewFlagKind.RUNTIME_MISMATCH,
        ReviewFlagKind.ABRIDGED_SUSPECT,
    }


def test_detects_ambiguous_top_two_with_conflicting_identifiers() -> None:
    candidates = apply_ambiguous_top2_flag(
        [
            _candidate(
                title="Book",
                authors=["Author"],
                isbn_13="9780000000001",
            ),
            MatchCandidate(
                identity=ProviderIdentity(
                    provider="provider",
                    external_id="candidate-2",
                    title="Book",
                    authors=["Author"],
                    isbn_13="9780000000002",
                ),
                confidence=0.91,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=["stub"],
            ),
        ]
    )

    assert candidates[0].review_flags[0].kind == ReviewFlagKind.AMBIGUOUS_TOP2
    assert candidates[1].review_flags[0].kind == ReviewFlagKind.AMBIGUOUS_TOP2
