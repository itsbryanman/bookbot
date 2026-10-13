"""Template engine for filename generation."""

import re
import unicodedata
from pathlib import Path

from ..config.models import CasePolicy
from .models import AudiobookSet, ProviderIdentity, Track


class TemplateEngine:
    """Generates filenames from templates and metadata."""

    # Lowercase surname particles kept with the family name in "Last, First".
    _SURNAME_PARTICLES = frozenset(
        {"da", "de", "del", "della", "der", "des", "di", "du", "la", "le", "van", "von"}
    )

    # Characters that are forbidden in filenames on various filesystems
    FORBIDDEN_CHARS = {"windows": r'<>:"/\\|?*', "unix": r"/", "common": r'<>:"/\\|?*'}
    PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]+))?\}")

    def __init__(
        self,
        case_policy: CasePolicy = CasePolicy.TITLE_CASE,
        unicode_normalize: bool = True,
        max_path_length: int = 255,
    ):
        self.case_policy = case_policy
        self.unicode_normalize = unicode_normalize
        self.max_path_length = max_path_length

    def generate_folder_name(
        self,
        audiobook_set: AudiobookSet,
        identity: ProviderIdentity | None = None,
        template: str = "{AuthorLastFirst}/{Title} ({Year})",
    ) -> str:
        """Generate folder name from template."""
        tokens = self._build_tokens(audiobook_set, identity)
        folder_name = self._apply_template(template, tokens)
        return self._normalize_path(folder_name)

    def generate_filename(
        self,
        track: Track,
        audiobook_set: AudiobookSet,
        identity: ProviderIdentity | None = None,
        template: str = "{DiscPad}{TrackPad} - {Title}",
        zero_padding_width: int = 0,
    ) -> str:
        """Generate filename from template."""
        tokens = self._build_tokens(audiobook_set, identity, track, zero_padding_width)
        filename = self._apply_template(template, tokens)

        # Add file extension
        extension = track.src_path.suffix
        if not filename.endswith(extension):
            filename += extension

        return self._normalize_filename(filename)

    def _build_tokens(
        self,
        audiobook_set: AudiobookSet,
        identity: ProviderIdentity | None = None,
        track: Track | None = None,
        zero_padding_width: int = 0,
    ) -> dict[str, str]:
        """Build template tokens from metadata."""
        tokens = {}

        # Use identity if available, otherwise fall back to audiobook_set guesses
        if identity:
            title = identity.title or audiobook_set.raw_title_guess or "Unknown Title"
            author = (
                identity.authors[0] if identity.authors else audiobook_set.author_guess
            )
            series_name = identity.series_name or audiobook_set.series_guess
            series_index = identity.series_index or audiobook_set.volume_guess
            year = str(identity.year) if identity.year else ""
            narrator = identity.narrator or audiobook_set.narrator_guess
            language = identity.language or audiobook_set.language_guess
            isbn = identity.isbn_13 or identity.isbn_10
        else:
            title = audiobook_set.raw_title_guess or "Unknown Title"
            author = audiobook_set.author_guess
            series_name = audiobook_set.series_guess
            series_index = audiobook_set.volume_guess
            year = ""
            narrator = audiobook_set.narrator_guess
            language = audiobook_set.language_guess
            isbn = None

        # Basic tokens
        tokens["Title"] = title
        tokens["ShortTitle"] = self._shorten_title(title)
        tokens["Year"] = year
        tokens["Language"] = language or ""
        tokens["Narrator"] = narrator or ""
        tokens["ISBN"] = isbn or ""

        # Author tokens
        if author:
            tokens["Author"] = author
            tokens["AuthorLastFirst"] = self._format_author_last_first(author)
        else:
            tokens["Author"] = "Unknown Author"
            tokens["AuthorLastFirst"] = "Unknown Author"

        # Series tokens
        tokens["SeriesName"] = series_name or ""
        tokens["SeriesIndex"] = str(series_index) if series_index is not None else ""

        # Track-specific tokens
        if track:
            # Determine zero padding width for tracks
            if zero_padding_width == 0:
                max_track = max(
                    (t.track_index for t in audiobook_set.tracks if t.track_index),
                    default=0,
                )
                if max_track == 0:
                    max_track = audiobook_set.total_tracks or 0
                if max_track == 0:
                    max_track = track.track_index or 0
                padding_width = len(str(max_track)) if max_track else 1
            else:
                padding_width = max(1, zero_padding_width)

            track_index = track.track_index if track.track_index else 1
            track_str = str(track_index).zfill(padding_width)
            tokens["TrackPad"] = track_str
            tokens["Track"] = str(track_index)

            if audiobook_set.disc_count > 1:
                disc_padding_width = len(str(audiobook_set.disc_count))
                disc_index = track.disc if track.disc else 1
                disc_str = str(disc_index).zfill(disc_padding_width)
                tokens["DiscPad"] = disc_str
                tokens["Disc"] = str(disc_index)
            else:
                tokens["DiscPad"] = ""
                tokens["Disc"] = ""

            # Track title from tags if available
            if track.existing_tags.title:
                tokens["TrackTitle"] = track.existing_tags.title
            else:
                tokens["TrackTitle"] = self._derive_track_title(track.stem)
        else:
            tokens["TrackPad"] = ""
            tokens["Track"] = ""
            tokens["DiscPad"] = ""
            tokens["Disc"] = ""
            tokens["TrackTitle"] = ""

        # Apply case policy
        for key, value in tokens.items():
            if value:
                tokens[key] = self._apply_case_policy(value)

        # Lowercase and snake_case aliases for declarative profile templates
        alias_tokens = {
            "author": tokens["Author"],
            "authors": tokens["Author"],
            "author_sort": tokens["AuthorLastFirst"],
            "title": tokens["Title"],
            "short_title": tokens["ShortTitle"],
            "series": tokens["SeriesName"],
            "series_index": tokens["SeriesIndex"],
            "year": tokens["Year"],
            "narrator": tokens["Narrator"],
            "disc_pad": tokens["DiscPad"],
            "track_pad": tokens["TrackPad"],
            "disc": tokens["Disc"],
            "track": tokens["Track"],
            "track_title": tokens["TrackTitle"],
            "language": tokens["Language"],
            "isbn": tokens["ISBN"],
        }
        tokens.update(alias_tokens)

        return tokens

    def _apply_template(self, template: str, tokens: dict[str, str]) -> str:
        """Apply tokens to template string."""

        def replace(match: re.Match[str]) -> str:
            token_name = match.group(1)
            format_spec = match.group(2)
            value = tokens.get(token_name, "")
            if not format_spec or not value:
                return value
            if format_spec.isdigit():
                return value.zfill(int(format_spec))
            return value

        result = self.PLACEHOLDER_PATTERN.sub(replace, template)

        # Remove punctuation left behind by empty tokens
        result = re.sub(r"\(\s*\)", "", result)
        result = re.sub(r"(?<=/)\s*-\s+", "", result)
        result = re.sub(r"\s+-\s+(?=/|$)", "", result)
        result = re.sub(r"/{2,}", "/", result)

        # Clean up repeated whitespace without touching punctuation
        result = re.sub(r"\s+", " ", result)
        result = result.strip(" /-")

        return result

    def _apply_case_policy(self, text: str) -> str:
        """Apply case policy to text."""
        if not text:
            return text

        if self.case_policy == CasePolicy.TITLE_CASE:
            return self._smart_title_case(text)
        elif self.case_policy == CasePolicy.LOWER_CASE:
            return text.lower()
        elif self.case_policy == CasePolicy.UPPER_CASE:
            return text.upper()
        else:  # AS_IS
            return text

    # Articles, conjunctions, and short prepositions stay lowercase in titles
    # unless they start or end the title or follow a colon or dash.
    _SMALL_WORDS = frozenset(
        {
            "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor",
            "of", "on", "or", "per", "the", "to", "via", "vs",
        }
    )  # fmt: skip

    def _smart_title_case(self, text: str) -> str:
        """Apply smart title case that handles articles and prepositions."""
        if not text:
            return text

        words = text.split()
        result: list[str] = []
        for index, word in enumerate(words):
            starts_phrase = index == 0 or words[index - 1].endswith((":", "-", "—"))
            is_last = index == len(words) - 1
            if (
                not starts_phrase
                and not is_last
                and word.lower() in self._SMALL_WORDS
                and (word.islower() or word.istitle())
            ):
                result.append(word.lower())
            else:
                result.append(self._title_case_word(word))
        return " ".join(result)

    @staticmethod
    def _title_case_word(word: str) -> str:
        """Capitalize a word, keeping initials (J.R.R.) and names like McCarthy."""
        rest = word[1:]
        has_mixed_case = any(c.isupper() for c in rest) and any(
            c.islower() for c in rest
        )
        if "." in word or has_mixed_case:
            return word[:1].upper() + rest
        if any(c.isdigit() for c in word) and word.upper() == word:
            # Alphanumeric names such as "NOS4A2" are written as given.
            return word
        return word.capitalize()

    def _primary_author(self, author: str) -> str:
        """Pick the first name out of a list like "Jocko Willink, Leif Babin"."""
        parts = [
            part.strip()
            for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", author)
            if part.strip()
        ]
        if len(parts) < 2:
            return author.strip()
        if ";" in author or "&" in author or re.search(r"\band\b", author):
            return parts[0]
        # "Le Guin, Ursula K." and "Tolkien, J.R.R." are one author written
        # surname-first; "Jocko Willink, Leif Babin" is two authors.
        first_words = parts[0].split()
        surname_first = len(parts) == 2 and (
            len(first_words) == 1 or first_words[0].lower() in self._SURNAME_PARTICLES
        )
        return author.strip() if surname_first else parts[0]

    def _format_author_last_first(self, author: str) -> str:
        """Format author name as 'Last, First'."""
        author = self._primary_author(author)
        # Already in "Last, First" form (e.g. re-scanning our own output):
        # pass through unchanged instead of re-flipping into "J., Sobol, Donald".
        if "," in author:
            return author.strip()
        # Simple heuristic: assume last word is surname, keeping lowercase-able
        # particles with it ("Le Guin", "van Beethoven", "de la Cruz").
        parts = author.strip().split()
        if len(parts) <= 1:
            return author

        surname_start = len(parts) - 1
        while (
            surname_start > 1
            and parts[surname_start - 1].lower() in self._SURNAME_PARTICLES
        ):
            surname_start -= 1
        last_name = " ".join(parts[surname_start:])
        first_names = " ".join(parts[:surname_start])
        return f"{last_name}, {first_names}"

    def _shorten_title(self, title: str, max_length: int = 30) -> str:
        """Create a shortened version of the title."""
        if len(title) <= max_length:
            return title

        # Try to break at word boundaries
        words = title.split()
        result = ""
        for word in words:
            if len(result + " " + word) > max_length:
                break
            if result:
                result += " "
            result += word

        return result if result else title[:max_length]

    def _derive_track_title(self, stem: str) -> str:
        """Build a readable track title from the source filename."""
        cleaned = re.sub(r"^[\d\s._-]+", "", stem).replace("_", " ").strip()
        return cleaned or stem or "Track"

    @staticmethod
    def _soften_punctuation(text: str) -> str:
        """Rewrite characters that filesystems reject into readable equivalents.

        "What If?: Serious Answers" becomes "What If - Serious Answers" rather
        than "What If__ Serious Answers".
        """
        text = re.sub(r"\s*:\s*", " - ", text)
        text = text.replace("?", "").replace('"', "'")
        text = re.sub(r"\s*\|\s*", " - ", text)
        text = text.replace("<", "").replace(">", "")
        text = re.sub(r"(?:\s+-)+\s+", " - ", text)
        return re.sub(r"\s{2,}", " ", text).strip()

    def _normalize_path(self, path: str) -> str:
        """Normalize a path string for filesystem safety."""
        if self.unicode_normalize:
            path = unicodedata.normalize("NFC", path)
        path = "/".join(
            self._soften_punctuation(part) for part in re.split(r"[\\/]", path)
        )

        parts = re.split(r"[\\/]", path)
        normalized_parts: list[str] = []
        forbidden = self.FORBIDDEN_CHARS["common"]

        for raw_part in parts:
            part = raw_part.strip()
            for char in forbidden:
                if char == "/":
                    continue
                part = part.replace(char, "_")
            if len(part) > 100:  # Individual component limit
                part = self._truncate_intelligently(part, 100)
            normalized_parts.append(part)

        result = "/".join(filter(None, normalized_parts))

        # Overall path length check
        if len(result) > self.max_path_length:
            result = self._truncate_intelligently(result, self.max_path_length)

        return result

    def _normalize_filename(self, filename: str) -> str:
        """Normalize a filename for filesystem safety."""
        if self.unicode_normalize:
            filename = unicodedata.normalize("NFC", filename)
        filename = self._soften_punctuation(filename)

        # Replace forbidden characters
        forbidden = self.FORBIDDEN_CHARS["common"]
        for char in forbidden:
            filename = filename.replace(char, "_")

        # Handle filename length limits (255 chars is common limit)
        max_filename_length = 255
        if len(filename) > max_filename_length:
            # Preserve extension
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            max_stem_length = max_filename_length - len(suffix)
            stem = self._truncate_intelligently(stem, max_stem_length)
            filename = stem + suffix

        return filename.strip()

    def _truncate_intelligently(self, text: str, max_length: int) -> str:
        """Truncate text while trying to preserve meaning."""
        if len(text) <= max_length:
            return text

        # Try to break at word boundaries
        if " " in text:
            words = text.split()
            result = ""
            for word in words:
                if len(result + " " + word) > max_length - 3:  # Leave room for "..."
                    break
                if result:
                    result += " "
                result += word

            if result:
                return result + "..."

        # Fallback: simple truncation with ellipsis
        return text[: max_length - 3] + "..."

    def validate_template(self, template: str) -> tuple[bool, list[str]]:
        """Validate a template string."""
        errors = []

        # Check for invalid characters in template
        forbidden_chars = [c for c in self.FORBIDDEN_CHARS["common"] if c != "/"]
        if any(char in template for char in forbidden_chars):
            errors.append("Template contains forbidden characters")

        # Check for unmatched braces
        open_braces = template.count("{")
        close_braces = template.count("}")
        if open_braces != close_braces:
            errors.append("Template has unmatched braces")

        # Extract and validate token names
        tokens = self.PLACEHOLDER_PATTERN.findall(template)
        valid_tokens = {
            "Author",
            "AuthorLastFirst",
            "Title",
            "ShortTitle",
            "SeriesName",
            "SeriesIndex",
            "Year",
            "Narrator",
            "DiscPad",
            "TrackPad",
            "Disc",
            "Track",
            "TrackTitle",
            "Language",
            "ISBN",
            "author",
            "authors",
            "author_sort",
            "title",
            "short_title",
            "series",
            "series_index",
            "year",
            "narrator",
            "disc_pad",
            "track_pad",
            "disc",
            "track",
            "track_title",
            "language",
            "isbn",
        }

        for token, format_spec in tokens:
            if token not in valid_tokens:
                errors.append(f"Unknown token: {{{token}}}")
            if format_spec and not format_spec.isdigit():
                errors.append(f"Unsupported format specifier: {format_spec}")

        return len(errors) == 0, errors
