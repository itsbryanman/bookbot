"""Fuzzy matching for audiobook metadata."""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .logging import get_logger

logger = get_logger("matching")


@dataclass
class MatchScore:
    """Detailed scoring breakdown."""

    title_score: float
    author_score: float
    series_score: float
    year_score: float
    combined_score: float
    confidence: str  # 'high' | 'medium' | 'low'
    reasons: list[str]


class AdvancedMatcher:
    """Fuzzy matching for audiobook metadata."""

    # Common author aliases (expandable via JSON file)
    AUTHOR_ALIASES: dict[str, set[str]] = {
        "j.k. rowling": {"joanne rowling", "robert galbraith"},
        "stephen king": {"richard bachman"},
        "iain banks": {"iain m. banks"},
    }

    # Articles to strip from titles
    ARTICLES: set[str] = {"the", "a", "an", "el", "la", "le", "der", "die"}

    # Precompiled article-stripping regex (single alternation)
    _ARTICLE_RE: re.Pattern[str] = re.compile(
        r"^(?:" + "|".join(sorted(ARTICLES, key=len, reverse=True)) + r")\s+",
        re.I,
    )
    # Trailing article pattern: "Stand, The" → "The Stand"
    _TRAILING_ARTICLE_RE: re.Pattern[str] = re.compile(
        r",\s*(?:" + "|".join(sorted(ARTICLES, key=len, reverse=True)) + r")$",
        re.I,
    )

    # Series detection patterns (pattern, confidence)
    SERIES_PATTERNS: list[tuple[re.Pattern[str], float]] = [
        (re.compile(r"(.+?)\s+(?:book|vol(?:ume)?)\s+(\d+)", re.I), 0.95),
        (re.compile(r"(.+?)\s+#(\d+)", re.I), 0.90),
        (re.compile(r"(.+?)\s+part\s+(\d+)", re.I), 0.85),
    ]

    def __init__(self) -> None:
        self._aliases_loaded = False
        self._merged_aliases: dict[str, set[str]] | None = None

    @property
    def _effective_aliases(self) -> dict[str, set[str]]:
        """Lazily load and cache merged author aliases."""
        if self._merged_aliases is not None:
            return self._merged_aliases
        self._merged_aliases = self._load_merged_aliases()
        return self._merged_aliases

    def _load_merged_aliases(self) -> dict[str, set[str]]:
        """Load aliases from builtin + optional config JSON, merged."""
        # Start with a copy of the builtins
        merged: dict[str, set[str]] = {
            k: set(v) for k, v in self.AUTHOR_ALIASES.items()
        }

        # Try loading from config dir
        json_path = self._find_aliases_json()
        if json_path and json_path.is_file():
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for canonical, aliases in raw.items():
                        key = canonical.lower().strip()
                        alias_set = {a.lower().strip() for a in aliases}
                        if key in merged:
                            merged[key] |= alias_set
                        else:
                            merged[key] = alias_set
                    logger.info(f"Loaded author aliases from {json_path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load author aliases JSON: {e}")

        return merged

    @staticmethod
    def _find_aliases_json() -> Path | None:
        """Find author_aliases.json in config dir or bundled data."""
        import os

        # Check config dir
        if os.environ.get("XDG_CONFIG_HOME"):
            config_path = Path(os.environ["XDG_CONFIG_HOME"]) / "bookbot"
        elif os.name == "nt":
            config_path = Path(
                os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
            ) / "bookbot"
        else:
            config_path = Path.home() / ".config" / "bookbot"

        config_json = config_path / "author_aliases.json"
        if config_json.is_file():
            return config_json

        # Check bundled data dir
        bundled = Path(__file__).parent.parent / "data" / "author_aliases.json"
        if bundled.is_file():
            return bundled

        return None

    def normalize_author(self, author: str) -> str:
        """Normalize author name for comparison."""
        norm = author.lower().strip()

        # Remove suffixes
        for suffix in [" jr.", " sr.", " ii", " iii", " iv"]:
            if norm.endswith(suffix):
                norm = norm[: -len(suffix)].strip()

        # Unicode normalization
        norm = unicodedata.normalize("NFKD", norm)
        norm = norm.encode("ascii", "ignore").decode("ascii")

        # Convert "Last, First" to "First Last"
        if "," in norm:
            parts = [p.strip() for p in norm.split(",", 1)]
            if len(parts) == 2:
                norm = f"{parts[1]} {parts[0]}"

        return norm

    def match_author(self, author1: str, author2: str) -> float:
        """Author similarity [0.0-1.0]."""
        norm1 = self.normalize_author(author1)
        norm2 = self.normalize_author(author2)

        if norm1 == norm2:
            return 1.0

        # Check aliases
        for canonical, aliases in self._effective_aliases.items():
            all_names = aliases | {canonical}
            if norm1 in all_names and norm2 in all_names:
                return 1.0

        # Fuzzy match
        ratio = fuzz.ratio(norm1, norm2) / 100.0
        token_sort = fuzz.token_sort_ratio(norm1, norm2) / 100.0
        partial = fuzz.partial_ratio(norm1, norm2) / 100.0

        return ratio * 0.4 + token_sort * 0.4 + partial * 0.2

    def normalize_title(self, title: str) -> str:
        """Normalize title for comparison."""
        norm = title.lower().strip()

        # Handle trailing article: "Stand, The" → strip the trailing ", the"
        norm = self._TRAILING_ARTICLE_RE.sub("", norm)

        # Remove leading article using precompiled alternation
        norm = self._ARTICLE_RE.sub("", norm)

        # Remove punctuation except hyphens
        norm = re.sub(r"[^\w\s-]", "", norm)

        # Normalize whitespace
        norm = " ".join(norm.split())

        # Unicode normalization
        norm = unicodedata.normalize("NFKD", norm)
        norm = norm.encode("ascii", "ignore").decode("ascii")

        return norm

    def match_title(self, title1: str, title2: str) -> float:
        """Title similarity [0.0-1.0]."""
        norm1 = self.normalize_title(title1)
        norm2 = self.normalize_title(title2)

        if norm1 == norm2:
            return 1.0

        ratio = fuzz.ratio(norm1, norm2) / 100.0
        token_set = fuzz.token_set_ratio(norm1, norm2) / 100.0

        return ratio * 0.4 + token_set * 0.6

    def extract_series(self, title: str) -> tuple[str, int | None, float] | None:
        """Extract (series_name, book_number, confidence) from title."""
        for pattern, confidence in self.SERIES_PATTERNS:
            match = pattern.search(title)
            if match:
                series_name = match.group(1).strip()
                try:
                    book_num = int(match.group(2))
                except (ValueError, IndexError):
                    book_num = None
                return (series_name, book_num, confidence)
        return None

    def calculate_match(
        self,
        query_title: str,
        query_author: str | None,
        query_series: str | None,
        query_year: int | None,
        result_title: str,
        result_authors: list[str],
        result_series: str | None,
        result_year: int | None,
    ) -> MatchScore:
        """Calculate comprehensive match score."""

        # Title (50%)
        title_score = self.match_title(query_title, result_title)

        # Author (30%)
        author_score = 0.0
        if query_author and result_authors:
            scores = [self.match_author(query_author, ra) for ra in result_authors]
            author_score = max(scores) if scores else 0.0

        # Series (15%)
        series_score = 0.0
        if query_series and result_series:
            series_score = self.match_title(query_series, result_series)

        # Year (5%)
        year_score = 0.0
        if query_year and result_year:
            diff = abs(query_year - result_year)
            if diff == 0:
                year_score = 1.0
            elif diff <= 2:
                year_score = 0.7
            elif diff <= 5:
                year_score = 0.4

        # Combined
        combined = (
            title_score * 0.50
            + author_score * 0.30
            + series_score * 0.15
            + year_score * 0.05
        )

        # Confidence
        if combined > 0.85:
            confidence = "high"
        elif combined > 0.65:
            confidence = "medium"
        else:
            confidence = "low"

        # Reasons
        reasons = []
        if title_score > 0.9:
            reasons.append("Excellent title match")
        if author_score > 0.9:
            reasons.append("Author confirmed")
        if series_score > 0.8:
            reasons.append("Series match")
        if year_score > 0.8:
            reasons.append("Year match")

        return MatchScore(
            title_score=title_score,
            author_score=author_score,
            series_score=series_score,
            year_score=year_score,
            combined_score=combined,
            confidence=confidence,
            reasons=reasons,
        )
