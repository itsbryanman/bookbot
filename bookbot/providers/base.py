"""Base provider interface for metadata sources."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

from ..core.models import AudiobookSet, MatchCandidate, ProviderIdentity

if TYPE_CHECKING:
    from ..io.cache import CacheManager


class MetadataProvider(ABC):
    """Abstract base class for metadata providers."""

    def __init__(
        self, name: str, cache_manager: Optional["CacheManager"] = None
    ) -> None:
        self.name = name
        self.cache_manager = cache_manager

    @abstractmethod
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
        """Search for books matching the given criteria."""
        pass

    @abstractmethod
    async def get_by_id(self, external_id: str) -> Optional[ProviderIdentity]:
        """Get a book by its external ID."""
        pass

    @abstractmethod
    def calculate_match_score(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity
    ) -> float:
        """Calculate a match score between an audiobook set and provider identity."""
        pass

    async def find_matches(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> List[MatchCandidate]:
        """Find potential matches for an audiobook set."""
        # Perform search using available metadata
        identities = await self.search(
            title=audiobook_set.raw_title_guess,
            author=audiobook_set.author_guess,
            series=audiobook_set.series_guess,
            language=audiobook_set.language_guess,
            limit=limit,
        )

        # Score and rank the candidates
        candidates = []
        for identity in identities:
            score = self.calculate_match_score(audiobook_set, identity)

            # Determine match reasons
            reasons = self._get_match_reasons(audiobook_set, identity, score)

            candidate = MatchCandidate(
                identity=identity,
                confidence=score,
                confidence_level=self._get_confidence_level(score),
                match_reasons=reasons,
            )
            candidates.append(candidate)

        # Sort by confidence score (highest first)
        candidates.sort(key=lambda c: c.confidence, reverse=True)

        return candidates

    def _get_match_reasons(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity, score: float
    ) -> List[str]:
        """Generate human-readable match reasons."""
        reasons = []

        if audiobook_set.raw_title_guess and identity.title:
            if audiobook_set.raw_title_guess.lower() in identity.title.lower():
                reasons.append("Title match")

        if audiobook_set.author_guess and identity.authors:
            for author in identity.authors:
                if audiobook_set.author_guess.lower() in author.lower():
                    reasons.append("Author match")
                    break

        if audiobook_set.series_guess and identity.series_name:
            if audiobook_set.series_guess.lower() in identity.series_name.lower():
                reasons.append("Series match")

        if score > 0.85:
            reasons.append("High confidence match")
        elif score > 0.65:
            reasons.append("Good match")
        else:
            reasons.append("Possible match")

        return reasons

    def _get_confidence_level(self, score: float) -> str:
        if score > 0.85:
            return "high"
        elif score > 0.65:
            return "medium"
        else:
            return "low"
