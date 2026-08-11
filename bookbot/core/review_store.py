"""Persistence and state resolution for manual review decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ..config.manager import ConfigManager
from ..config.models import MatchingConfig
from .matching import AdvancedMatcher
from .models import AudiobookSet, MatchCandidate
from .review import (
    accept_candidate_for_set,
    auto_accept_candidate,
    mark_set_needs_review,
    skip_set_from_plans,
)


class StoredCandidateSelector(BaseModel):
    """Persistent identifiers for a chosen candidate."""

    provider: str
    external_id: str
    title: str
    authors: list[str]
    asin: str | None = None
    isbn_10: str | None = None
    isbn_13: str | None = None


class ReviewDecision(BaseModel):
    """Stored manual decision for a review-required set."""

    action: Literal["accept", "skip"]
    candidate: StoredCandidateSelector | None = None
    candidate_index: int | None = None
    reason: str | None = None
    decided_at: str


class ReviewDecisionStore:
    """Persist and apply review decisions across CLI/TUI sessions."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.path = config_manager.config_dir / "review-decisions.json"

    def review_key(self, audiobook_set: AudiobookSet) -> str:
        """Build a stable key for one scanned audiobook set."""
        source_path = str(audiobook_set.source_path.resolve(strict=False))
        track_names = sorted(
            self._track_identity(audiobook_set, track) for track in audiobook_set.tracks
        )
        digest = hashlib.sha256("\n".join(track_names).encode("utf-8")).hexdigest()
        return f"{source_path}::{digest}"

    def load(self) -> dict[str, ReviewDecision]:
        """Load the on-disk decision map."""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        decisions: dict[str, ReviewDecision] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                decisions[key] = ReviewDecision(**value)
            except Exception:
                continue
        return decisions

    def save(self, decisions: dict[str, ReviewDecision]) -> None:
        """Persist the decision map."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: decision.model_dump(mode="json", exclude_none=True)
            for key, decision in decisions.items()
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def get(self, audiobook_set: AudiobookSet) -> ReviewDecision | None:
        """Return the stored decision for a set, if any."""
        return self.load().get(self.review_key(audiobook_set))

    def set_accept(
        self,
        audiobook_set: AudiobookSet,
        candidate: MatchCandidate,
        *,
        candidate_index: int,
    ) -> None:
        """Persist an accepted candidate choice."""
        decisions = self.load()
        decisions[self.review_key(audiobook_set)] = ReviewDecision(
            action="accept",
            candidate=self._selector_from_candidate(candidate),
            candidate_index=candidate_index,
            decided_at=datetime.now().isoformat(),
        )
        self.save(decisions)

    def set_skip(
        self,
        audiobook_set: AudiobookSet,
        *,
        reason: str = "Skipped by reviewer",
    ) -> None:
        """Persist an explicit skip decision."""
        decisions = self.load()
        decisions[self.review_key(audiobook_set)] = ReviewDecision(
            action="skip",
            reason=reason,
            decided_at=datetime.now().isoformat(),
        )
        self.save(decisions)

    def clear(self, audiobook_set: AudiobookSet) -> None:
        """Remove any saved decision for a set."""
        decisions = self.load()
        decisions.pop(self.review_key(audiobook_set), None)
        self.save(decisions)

    def apply_saved_decision(
        self,
        audiobook_set: AudiobookSet,
        candidates: list[MatchCandidate],
    ) -> tuple[str, MatchCandidate | None] | None:
        """Apply a persisted decision to a matched set, if possible."""
        decision = self.get(audiobook_set)
        if decision is None:
            return None
        if decision.action == "skip":
            skip_set_from_plans(audiobook_set, decision.reason or "Skipped by reviewer")
            return "skip", None
        if decision.candidate is None:
            return None
        matched_candidate = self._find_candidate(candidates, decision.candidate)
        if matched_candidate is None:
            return None
        accept_candidate_for_set(audiobook_set, matched_candidate)
        return "accept", matched_candidate

    def resolve_match_state(
        self,
        audiobook_set: AudiobookSet,
        candidates: list[MatchCandidate],
        matching: MatchingConfig,
    ) -> tuple[str, MatchCandidate | None]:
        """Apply saved decisions, otherwise fall back to auto-accept rules."""
        audiobook_set.provider_candidates = candidates
        audiobook_set.chosen_identity = None
        audiobook_set.needs_review = False
        audiobook_set.review_skipped = False
        audiobook_set.review_reason = None

        if not candidates:
            mark_set_needs_review(audiobook_set, "No matches found")
            return "unmatched", None

        saved = self.apply_saved_decision(audiobook_set, candidates)
        if saved is not None:
            action, chosen_candidate = saved
            if action == "skip":
                return "skipped", candidates[0]
            return "accepted_saved", chosen_candidate

        best_match = candidates[0]
        auto_accept, reason = auto_accept_candidate(best_match, matching)
        if auto_accept:
            accept_candidate_for_set(audiobook_set, best_match)
            return "accepted_auto", best_match

        mark_set_needs_review(audiobook_set, reason or "Review required")
        return "review", best_match

    @staticmethod
    def _selector_from_candidate(
        candidate: MatchCandidate,
    ) -> StoredCandidateSelector:
        identity = candidate.identity
        return StoredCandidateSelector(
            provider=identity.provider,
            external_id=identity.external_id,
            title=identity.title,
            authors=identity.authors,
            asin=identity.asin,
            isbn_10=identity.isbn_10,
            isbn_13=identity.isbn_13,
        )

    @staticmethod
    def _track_identity(audiobook_set: AudiobookSet, track) -> str:  # type: ignore[no-untyped-def]
        track_path = track.src_path.resolve(strict=False)
        source_path = audiobook_set.source_path.resolve(strict=False)
        try:
            return str(track_path.relative_to(source_path))
        except ValueError:
            name: str = track.src_path.name
            return name

    @staticmethod
    def _find_candidate(
        candidates: list[MatchCandidate],
        selector: StoredCandidateSelector,
    ) -> MatchCandidate | None:
        matcher = AdvancedMatcher()
        for candidate in candidates:
            identity = candidate.identity
            if (
                identity.provider == selector.provider
                and identity.external_id == selector.external_id
            ):
                return candidate
            if selector.asin and identity.asin == selector.asin:
                return candidate
            if selector.isbn_13 and identity.isbn_13 == selector.isbn_13:
                return candidate
            if selector.isbn_10 and identity.isbn_10 == selector.isbn_10:
                return candidate
            if matcher.match_title(identity.title, selector.title) >= 0.99 and sorted(
                author.casefold() for author in identity.authors
            ) == sorted(author.casefold() for author in selector.authors):
                return candidate
        return None
