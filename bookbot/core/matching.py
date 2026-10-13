"""Fuzzy matching for audiobook metadata."""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .logging import get_logger

logger = get_logger("matching")

# Catalogs such as Audible list translators, editors and foreword writers next
# to the author as "Name - role". They are contributors, not authors.
_CONTRIBUTOR_ROLE_RE = re.compile(
    r"\s+[-\u2013\u2014]\s+(?:translator|translated by|translation|foreword|"
    r"afterword|introduction|editor|edited by|contributor|illustrator|"
    r"adaptation|adapted by|adapter|preface|narrator|reader|compiler)\s*$",
    re.IGNORECASE,
)


def filter_author_names(names: list[str]) -> list[str]:
    """Drop contributors listed with a role suffix (" - translator", ...).

    If every name carries a role (an edited anthology, say), the names are
    kept without their suffixes rather than leaving the book authorless.
    """
    authors = [name for name in names if not _CONTRIBUTOR_ROLE_RE.search(name)]
    if authors or not names:
        return authors
    return [_CONTRIBUTOR_ROLE_RE.sub("", name).strip() for name in names]


# Noise that release groups and rippers attach to titles. None of it is part
# of a title a catalog would know, and all of it makes search APIs return
# nothing at all.
_BRACKETED_NOISE_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)")
_LEADING_YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s*[-\u2013\u2014.]\s+")
_BITRATE_RE = re.compile(r"\b\d{2,3}\s*k(?:bps|bits?|bts)?\b", re.IGNORECASE)
_RUNTIME_RE = re.compile(r"\b\d{1,3}[.:]\d{2}[.:]\d{2}\b")
_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b", re.IGNORECASE)
_SUBTITLE_NOISE_RE = re.compile(
    r"(?:\s*[:\-\u2013\u2014]\s*|\s+)"
    r"(?:a|an)\s+(?:novel|novella|memoir|thriller|mystery|short story|love story)"
    r"\s*$",
    re.IGNORECASE,
)
_TRAILING_FORMAT_RE = re.compile(
    r"[\s_\-]+(?:m4b|m4a|mp3|aac|flac|epub|mobi|azw3|unabridged|abridged|"
    r"audiobooks?|audio)\s*$",
    re.IGNORECASE,
)
_EDGE_PUNCTUATION_RE = re.compile(
    r"^[\s\-\u2013\u2014:;,_.~]+|[\s\-\u2013\u2014:;,_.~@]+$"
)


def clean_query_title(title: str) -> str:
    """Strip release noise from a title before it is used as a search query.

    Removes bracketed/parenthesised/braced asides ("(Unabridged)", "[audio]",
    "{220mb}"), bitrate/runtime/size suffixes ("64k 07.58.53"), a leading
    release year ("1992 - "), generic subtitles (": A Novel") and trailing
    format words ("_M4B"). Returns the input unchanged when nothing would be
    left, so a title such as "1984" survives.
    """
    original = " ".join(title.split())
    cleaned = original
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _BRACKETED_NOISE_RE.sub(" ", cleaned)
        cleaned = _LEADING_YEAR_RE.sub("", cleaned)
        cleaned = _BITRATE_RE.sub(" ", cleaned)
        cleaned = _RUNTIME_RE.sub(" ", cleaned)
        cleaned = _SIZE_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.replace("_", " ").split())
        cleaned = _SUBTITLE_NOISE_RE.sub("", cleaned)
        cleaned = _TRAILING_FORMAT_RE.sub("", cleaned)
        cleaned = _EDGE_PUNCTUATION_RE.sub("", cleaned)
    return cleaned if re.search(r"\w", cleaned) else original


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
        from ..config.manager import get_runtime_config_dir

        # Same resolution as ConfigManager: --config-dir (exported as
        # BOOKBOT_CONFIG_DIR), then the platform default config directory.
        config_json = get_runtime_config_dir() / "author_aliases.json"
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

    def _title_similarity(self, norm1: str, norm2: str) -> float:
        if norm1 == norm2:
            return 1.0
        ratio = fuzz.ratio(norm1, norm2) / 100.0
        token_set = fuzz.token_set_ratio(norm1, norm2) / 100.0
        return ratio * 0.4 + token_set * 0.6

    def match_title(self, title1: str, title2: str) -> float:
        """Title similarity [0.0-1.0].

        Subtitles after a colon are optional on either side, so
        "What If?: Serious Scientific Answers" still matches "What If?". A
        volume number present on only one side ("What If? 2") is penalized so
        sequels don't outrank the book itself.
        """
        norm1 = self.normalize_title(title1)
        norm2 = self.normalize_title(title2)

        if norm1 == norm2:
            return 1.0

        score = self._title_similarity(norm1, norm2)
        main1 = self.normalize_title(title1.split(":", 1)[0])
        main2 = self.normalize_title(title2.split(":", 1)[0])
        if main1 and main2 and (main1 != norm1 or main2 != norm2):
            score = max(score, 0.95 * self._title_similarity(main1, main2))

        trailing1 = re.search(r"(\d+)$", main1 or norm1)
        trailing2 = re.search(r"(\d+)$", main2 or norm2)
        number1 = int(trailing1.group(1)) if trailing1 else None
        number2 = int(trailing2.group(1)) if trailing2 else None
        if number1 != number2:
            score *= 0.8

        return score

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
