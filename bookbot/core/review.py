"""Review flag detection for candidate matching."""

from __future__ import annotations

import re

from ..config.models import MatchingConfig
from .matching import AdvancedMatcher
from .models import AudiobookSet, MatchCandidate, ReviewFlag, ReviewFlagKind

SEVERITY_ORDER = {"info": 0, "warn": 1, "block": 2}
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:,|;|/| & | and )\s*", re.IGNORECASE)


def detect_review_flags(
    audiobook_set: AudiobookSet,
    candidate: MatchCandidate,
    matcher: AdvancedMatcher,
) -> list[ReviewFlag]:
    """Detect review-worthy conflicts between local guesses and provider data."""
    flags: list[ReviewFlag] = []
    identity = candidate.identity
    local_title = audiobook_set.raw_title_guess or audiobook_set.source_path.name
    local_author = audiobook_set.author_guess
    local_narrator = audiobook_set.narrator_guess

    title_score = matcher.match_title(local_title, identity.title)
    if title_score < 0.70:
        flags.append(
            ReviewFlag(
                kind=ReviewFlagKind.TITLE_CONFLICT,
                severity="block",
                message="Local title guess conflicts with provider title",
                local_value=local_title,
                provider_value=identity.title,
            )
        )

    if local_author and identity.authors:
        best_author_score = max(
            matcher.match_author(local_author, provider_author)
            for provider_author in identity.authors
        )
        if best_author_score < 0.60:
            flags.append(
                ReviewFlag(
                    kind=ReviewFlagKind.AUTHOR_CONFLICT,
                    severity="block",
                    message="Local author guess conflicts with provider author",
                    local_value=local_author,
                    provider_value=", ".join(identity.authors),
                )
            )

    local_author_candidates = _local_author_candidates(audiobook_set)
    if (
        len(local_author_candidates) >= 2
        and len(identity.authors) == 1
        and identity.narrator
    ):
        narrator_match = max(
            matcher.match_author(local_author_candidate, identity.narrator)
            for local_author_candidate in local_author_candidates
        )
        if narrator_match >= 0.85:
            flags.append(
                ReviewFlag(
                    kind=ReviewFlagKind.NARRATOR_AS_AUTHOR,
                    severity="warn",
                    message="A local author tag matches the provider narrator",
                    local_value=", ".join(local_author_candidates),
                    provider_value=identity.narrator,
                )
            )

    if local_narrator and identity.authors:
        author_match = max(
            matcher.match_author(local_narrator, provider_author)
            for provider_author in identity.authors
        )
        narrator_match = (
            matcher.match_author(local_narrator, identity.narrator)
            if identity.narrator
            else 0.0
        )
        if author_match >= 0.85 and narrator_match < 0.85:
            flags.append(
                ReviewFlag(
                    kind=ReviewFlagKind.AUTHOR_AS_NARRATOR,
                    severity="warn",
                    message="Local narrator guess matches the provider author",
                    local_value=local_narrator,
                    provider_value=", ".join(identity.authors),
                )
            )

    runtime_flag = _runtime_flags(audiobook_set, candidate)
    flags.extend(runtime_flag)

    return flags


def apply_ambiguous_top2_flag(
    candidates: list[MatchCandidate],
) -> list[MatchCandidate]:
    """Flag a close top-two ranking when identifiers disagree."""
    if len(candidates) < 2:
        return candidates

    top = candidates[0]
    runner_up = candidates[1]
    if abs(top.confidence - runner_up.confidence) >= 0.05:
        return candidates

    if not _identifiers_conflict(top.identity, runner_up.identity):
        return candidates

    top.review_flags.append(
        ReviewFlag(
            kind=ReviewFlagKind.AMBIGUOUS_TOP2,
            severity="block",
            message="Top two candidates are close in score but disagree on identifiers",
            local_value=None,
            provider_value=_identifier_summary(runner_up.identity),
        )
    )
    runner_up.review_flags.append(
        ReviewFlag(
            kind=ReviewFlagKind.AMBIGUOUS_TOP2,
            severity="block",
            message="Top two candidates are close in score but disagree on identifiers",
            local_value=None,
            provider_value=_identifier_summary(top.identity),
        )
    )
    return candidates


def worst_flag_severity(flags: list[ReviewFlag]) -> str | None:
    """Return the highest review severity present."""
    if not flags:
        return None
    return max(flags, key=lambda flag: SEVERITY_ORDER[flag.severity]).severity


def has_flag_at_or_above(flags: list[ReviewFlag], severity: str) -> bool:
    """Check whether any flag meets or exceeds the requested severity."""
    threshold = SEVERITY_ORDER[severity]
    return any(SEVERITY_ORDER[flag.severity] >= threshold for flag in flags)


def review_reason(flags: list[ReviewFlag]) -> str | None:
    """Build a concise human-readable reason from the current flags."""
    if not flags:
        return None
    highest = worst_flag_severity(flags) or "warn"
    messages = []
    for flag in flags:
        if flag.message not in messages:
            messages.append(flag.message)
    return f"{highest.upper()}: {'; '.join(messages)}"


def auto_accept_candidate(
    candidate: MatchCandidate,
    matching: MatchingConfig,
) -> tuple[bool, str | None]:
    """Decide whether a candidate can be selected without manual review."""
    if candidate.confidence < matching.auto_accept_threshold:
        return (
            False,
            (
                f"Confidence {candidate.confidence:.2f} is below "
                f"auto-accept threshold {matching.auto_accept_threshold:.2f}"
            ),
        )
    if has_flag_at_or_above(candidate.review_flags, matching.block_on_severity):
        return False, review_reason(candidate.review_flags)
    if matching.require_review_on_flag and candidate.review_flags:
        return False, review_reason(candidate.review_flags)
    return True, None


def accept_candidate_for_set(
    audiobook_set: AudiobookSet,
    candidate: MatchCandidate,
) -> None:
    """Assign a candidate to a set and clear review state."""
    audiobook_set.chosen_identity = candidate.identity
    audiobook_set.needs_review = False
    audiobook_set.review_skipped = False
    audiobook_set.review_reason = None


def mark_set_needs_review(audiobook_set: AudiobookSet, reason: str) -> None:
    """Mark a set as needing manual review."""
    audiobook_set.chosen_identity = None
    audiobook_set.needs_review = True
    audiobook_set.review_skipped = False
    audiobook_set.review_reason = reason


def skip_set_from_plans(
    audiobook_set: AudiobookSet,
    reason: str = "Skipped by reviewer",
) -> None:
    """Mark a set as intentionally skipped from future plans."""
    audiobook_set.chosen_identity = None
    audiobook_set.needs_review = False
    audiobook_set.review_skipped = True
    audiobook_set.review_reason = reason


def _local_author_candidates(audiobook_set: AudiobookSet) -> list[str]:
    values: list[str] = []
    if audiobook_set.author_guess:
        values.append(audiobook_set.author_guess)

    for track in audiobook_set.tracks:
        for tag_value in (
            track.existing_tags.albumartist,
            track.existing_tags.artist,
        ):
            if tag_value:
                values.extend(
                    author.strip()
                    for author in _AUTHOR_SPLIT_RE.split(tag_value)
                    if author.strip()
                )

    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_values.append(value)
    return unique_values


def _runtime_flags(
    audiobook_set: AudiobookSet, candidate: MatchCandidate
) -> list[ReviewFlag]:
    if not audiobook_set.total_duration or not candidate.identity.runtime_minutes:
        return []

    provider_minutes = float(candidate.identity.runtime_minutes)
    if provider_minutes <= 0:
        return []

    local_minutes = audiobook_set.total_duration / 60.0
    relative_error = abs(local_minutes - provider_minutes) / provider_minutes
    flags: list[ReviewFlag] = []

    if relative_error > 0.10:
        flags.append(
            ReviewFlag(
                kind=ReviewFlagKind.RUNTIME_MISMATCH,
                severity="warn",
                message="Local runtime differs materially from provider runtime",
                local_value=f"{local_minutes:.0f} min",
                provider_value=f"{provider_minutes:.0f} min",
            )
        )

    ratio = local_minutes / provider_minutes
    if 0.55 <= ratio <= 0.75:
        flags.append(
            ReviewFlag(
                kind=ReviewFlagKind.ABRIDGED_SUSPECT,
                severity="warn",
                message="Local runtime looks short for the matched edition",
                local_value=f"{local_minutes:.0f} min",
                provider_value=f"{provider_minutes:.0f} min",
            )
        )

    return flags


def _identifiers_conflict(left, right) -> bool:  # type: ignore[no-untyped-def]
    return bool(
        left.isbn_13 and right.isbn_13 and left.isbn_13 != right.isbn_13
    ) or bool(left.asin and right.asin and left.asin != right.asin)


def _identifier_summary(identity) -> str:  # type: ignore[no-untyped-def]
    parts = []
    if identity.asin:
        parts.append(f"ASIN {identity.asin}")
    if identity.isbn_13:
        parts.append(f"ISBN-13 {identity.isbn_13}")
    return " · ".join(parts) or identity.external_id
