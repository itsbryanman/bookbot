"""Template engine for filename generation."""

import re
import string
import unicodedata
from pathlib import Path

from ..config.models import CasePolicy
from .models import AudiobookSet, ProviderIdentity, Track


class TemplateEngine:
    """Generates filenames from templates and metadata."""

    # Characters that are forbidden in filenames on various filesystems
    FORBIDDEN_CHARS = {"windows": r'<>:"/\\|?*', "unix": r"/", "common": r'<>:"/\\|?*'}

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
                tokens["TrackTitle"] = f"Track {track.track_index}"
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

        return tokens

    def _apply_template(self, template: str, tokens: dict[str, str]) -> str:
        """Apply tokens to template string."""
        result = template

        for token, value in tokens.items():
            placeholder = f"{{{token}}}"
            result = result.replace(placeholder, value or "")

        # Clean up repeated whitespace without touching punctuation
        result = re.sub(r"\s+", " ", result)
        result = result.strip()

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

    def _smart_title_case(self, text: str) -> str:
        """Apply smart title case that handles articles and prepositions."""
        if not text:
            return text

        return string.capwords(text)

    def _format_author_last_first(self, author: str) -> str:
        """Format author name as 'Last, First'."""
        # Simple heuristic: assume last word is surname
        parts = author.strip().split()
        if len(parts) <= 1:
            return author

        last_name = parts[-1]
        first_names = " ".join(parts[:-1])
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

    def _normalize_path(self, path: str) -> str:
        """Normalize a path string for filesystem safety."""
        if self.unicode_normalize:
            path = unicodedata.normalize("NFC", path)

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
        tokens = re.findall(r"\{([^}]+)\}", template)
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
        }

        for token in tokens:
            if token not in valid_tokens:
                errors.append(f"Unknown token: {{{token}}}")

        return len(errors) == 0, errors
