"""Audio file discovery and metadata extraction."""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from .matching import AdvancedMatcher, clean_query_title
from .models import AudiobookSet, AudioFormat, AudioTags, Track, TrackStatus

# ISBN validation: 10 digits (trailing X allowed) or 13 digits, after stripping
# hyphens/spaces
_ISBN_RE = re.compile(r"^\d{9}[\dXx]$|^\d{13}$")
# ASIN validation: B0 followed by 8 alphanumeric chars
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$", re.IGNORECASE)
_ASIN_SEARCH_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)
_NFO_NARRATOR_RE = re.compile(
    r"^\s*(?:read by|narrated by|narrator)\s*[:=-]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_IMPLAUSIBLE_AUTHOR_RE = re.compile(r"^[A-Z]{1,4}\s*\d+$")
# "Book 1 - Title" / "Vol. 2 - Title": a volume number, never an author.
_VOLUME_PREFIX_RE = re.compile(
    r"^(?:book|vol(?:ume)?|part)\.?\s*(\d+)\s*[-–—:]\s*(.+)$", re.IGNORECASE
)
_VOLUME_ONLY_RE = re.compile(r"^(?:book|vol(?:ume)?|part)\.?\s*\d+$", re.IGNORECASE)
# Encoding noise such as "128kbps", "64 kbps" or "64k" in folder names.
_BITRATE_NOISE_RE = re.compile(r"\b\d{2,3}\s*k(?:bps|bits?|bts)?\b", re.IGNORECASE)
# Rip-group suffixes: runtime "07.58.53" and size "{220mb}" / "1.81gb".
_RUNTIME_NOISE_RE = re.compile(r"\b\d{1,3}[.:]\d{2}[.:]\d{2}\b")
_SIZE_NOISE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b", re.IGNORECASE)
_TRAILING_FORMAT_NOISE_RE = re.compile(
    r"[\s_-]+(?:m4b|m4a|mp3|aac|flac|epub|mobi|azw3)\s*$", re.IGNORECASE
)
# "1992 - Author - Title": a release year in front of an Author - Title name.
_LEADING_YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s*[-–—]\s+(?=.+\s[-–—]\s)")
# "Earthsea 01 - A Wizard of Earthsea" or
# "Matt Dinniman - Dungeon Crawler Carl 01 - Dungeon Crawler Carl": a series
# name and number in front of the title, optionally after an author.
_SERIES_NUMBER_TITLE_RE = re.compile(
    r"^(?:(?P<author>[^-–—]+?)\s+[-–—]\s+)?"
    r"(?P<series>[^-–—]*?[A-Za-z][^-–—]*?)[\s_-]*#?(?P<number>\d{1,3})"
    r"\s*[-–—]\s*(?P<title>.+)$"
)
_JUNK_SIDECAR_SERIES = {"unabridged", "abridged", "adult", "audiobook"}
_SIDECAR_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
# "Last, First" shaped folder names (bookbot's own {AuthorLastFirst} output).
# Digits are rejected on either side so series artifacts like
# "05, Encyclopedia Brown" never read as authors.
_LAST_FIRST_AUTHOR_RE = re.compile(r"^[^\d,]{2,}?,\s+[^\d,]{2,}$")
_PERSONAL_NAME_TOKEN_RE = re.compile(
    r"^(?:[A-Z][A-Za-z'`-]*|[A-Z](?:\.[A-Z])+\.?|[A-Z]\.)$"
)
_PERSONAL_NAME_STOPWORDS = {
    "a",
    "an",
    "and",
    "book",
    "cd",
    "chapter",
    "disc",
    "disk",
    "for",
    "in",
    "of",
    "on",
    "part",
    "pt",
    "the",
    "to",
    "track",
    "vol",
    "volume",
    "with",
}
QUARANTINE_DIRNAME = ".bookbot-quarantine"

# Operating-system litter that is never part of a book: macOS AppleDouble
# resource forks ("._name", which keep the audio extension of the file they
# shadow), Finder and Explorer metadata.
_IGNORED_FILE_NAMES = {".ds_store", "desktop.ini", "thumbs.db", "ehthumbs.db"}

# Words that say nothing about which book a folder or file holds:
# "audiobook_final_FINAL", "New Folder (3)", "audio.m4b", "book.m4b".
_GENERIC_NAME_WORDS = {
    "audio",
    "audiobook",
    "audiobooks",
    "book",
    "books",
    "cd",
    "complete",
    "copy",
    "disc",
    "disk",
    "done",
    "download",
    "downloads",
    "edit",
    "file",
    "files",
    "final",
    "fixed",
    "folder",
    "m4a",
    "m4b",
    "misc",
    "mp3",
    "new",
    "part",
    "rip",
    "temp",
    "test",
    "tmp",
    "track",
    "tracks",
    "unknown",
    "untitled",
    "v",
    "version",
}
_HEX_NAME_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
# Tag values rippers and players fill in when they know nothing:
# "Unknown Book-Part1", "Unknown", "Unknown Album", "Track 01", "Untitled".
_PLACEHOLDER_TAG_RE = re.compile(
    r"^(?:unknown(?:\s+(?:book|title|album|artist|author))?|untitled|no\s+title|"
    r"track|chapter|title|album|audio(?:book)?|various(?:\s+artists)?)"
    r"(?:[\s_.:#-]*(?:part|pt|disc|cd|track)?[\s_.-]*\d*)?$",
    re.IGNORECASE,
)
# Release noise inside tag values: "Extreme Ownership (Unabridged)".
_TAG_NOISE_RE = re.compile(
    r"\s*[\(\[]\s*(?:un)?abridged(?:\s+edition)?\s*[\)\]]"
    r"|\s*[\(\[]\s*(?:audio(?:book)?|retail|mp3|m4b)\s*[\)\]]",
    re.IGNORECASE,
)
# "NOS4A2 (Disc 01)", "NOS4A2 [CD 3]", "NOS4A2 (Disc 2 of 17)".
_BRACKETED_DISC_RE = re.compile(
    r"^(?P<stem>.+?)\s*[\(\[]\s*(?:disc|disk|cd)\.?\s*(?P<num>\d+)"
    r"(?:\s*(?:of|/)\s*\d+)?\s*[\)\]]$",
    re.IGNORECASE,
)
# "Title by Author" folder names.
_TITLE_BY_AUTHOR_RE = re.compile(r"^(?P<title>.+)\s+by\s+(?P<author>.+?)$")
# "Alex Cross 01" as an album tag: a series name and number, not a title.
_SERIES_NUMBER_ONLY_RE = re.compile(
    r"^(?P<series>.*[A-Za-z].*?)\s+#?(?P<number>\d{1,3})$"
)
# Track order hidden in title tags when the track-number tag is missing.
_TITLE_PART_OF_RE = re.compile(
    r"\b(?:pt|part)\.?\s*(\d{1,4})\s*(?:of|/)\s*\d{1,4}\b", re.IGNORECASE
)
_TITLE_N_OF_M_RE = re.compile(r"\b(\d{1,4})\s+of\s+\d{1,4}\b", re.IGNORECASE)
_TITLE_N_DASH_M_RE = re.compile(r"^(\d{1,4})-(\d{1,4})\s")
_TITLE_PART_RE = re.compile(r"\b(?:pt|part)\.?\s*(\d{1,4})\b", re.IGNORECASE)
# "... - 012.mp3": a numeric suffix after a separator.
_TRAILING_TRACK_NUMBER_RE = re.compile(r"[\s_.-](\d{1,4})$")
# Comment tags such as "Read by Pam Ward" name the narrator.
_COMMENT_NARRATOR_RE = re.compile(
    r"\b(?:read|narrated|performed)\s+by\s*:?\s*(?P<name>[^.;\n(]+)",
    re.IGNORECASE,
)
_SMALL_TITLE_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "in",
    "nor",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _split_people(value: str) -> list[str]:
    """Split "A, B & C" / "A and B" into individual names."""
    return [
        part.strip()
        for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", value)
        if part.strip()
    ]


def is_ignored_file(path: Path) -> bool:
    """Whether a file is operating-system litter rather than book content."""
    name = path.name
    return name.startswith("._") or name.lower() in _IGNORED_FILE_NAMES


def natural_sort_key(path: Path) -> tuple[tuple[int, int, str], ...]:
    """Sort key that orders "CD 2" before "CD 10" and "a2" before "a10"."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
        for part in re.split(r"(\d+)", str(path))
        if part
    )


def is_generic_name(name: str) -> bool:
    """Whether a folder/file name carries no book identity.

    "audiobook_final_FINAL", "New Folder (3)", "audio", "book", "001" and
    random hex names ("3fa2c91e") are generic.
    """
    cleaned = name.strip()
    if not cleaned or _HEX_NAME_RE.fullmatch(cleaned):
        return True
    words = re.findall(r"[^\W\d_]+", cleaned.casefold())
    if not words:
        # "01" / "001" are track numbers; "1984" or "11/22/63" are titles.
        return bool(re.fullmatch(r"\W*\d{1,3}\W*", cleaned))
    return all(word in _GENERIC_NAME_WORDS for word in words)


def is_placeholder_tag(value: str | None) -> bool:
    """Whether a tag value is filler such as "Unknown" or "Track 01"."""
    if value is None:
        return True
    cleaned = value.strip()
    return (
        not cleaned
        or bool(_PLACEHOLDER_TAG_RE.fullmatch(cleaned))
        or is_generic_name(cleaned)
    )


def tidy_name_title(name: str) -> str:
    """Title-case a name that was written all in lower case ("the inmate")."""
    if not name or name != name.lower() or not re.search(r"[a-z]", name):
        return name
    words = name.split(" ")
    tidied = []
    for index, word in enumerate(words):
        if index > 0 and word in _SMALL_TITLE_WORDS:
            tidied.append(word)
        else:
            tidied.append(word[:1].upper() + word[1:])
    return " ".join(tidied)


def strip_decorations(text: str) -> str:
    """Fold full-width forms and drop decorative symbols ("★", "♥", emoji)."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(
        char for char in folded if unicodedata.category(char) not in {"So", "Cs"}
    )


def is_within_quarantine_tree(path: Path) -> bool:
    """Check whether a path lives under a BookBot quarantine directory."""
    return QUARANTINE_DIRNAME in path.parts


def iter_files_excluding_quarantine(root: Path) -> Iterator[Path]:
    """Yield files under ``root`` while skipping quarantine trees entirely."""
    if root.name == QUARANTINE_DIRNAME:
        return

    try:
        for item in root.iterdir():
            if item.is_dir():
                if item.name == QUARANTINE_DIRNAME:
                    continue
                yield from iter_files_excluding_quarantine(item)
            elif item.is_file():
                yield item
    except PermissionError:
        return


@dataclass(frozen=True)
class DiscFolderMatch:
    """Parsed disc-folder naming details used during grouping."""

    number: int
    stem: str | None = None


class AudioFileScanner:
    """Scans directories for audio files and extracts metadata."""

    SUPPORTED_EXTENSIONS = {
        ".mp3": AudioFormat.MP3,
        ".m4a": AudioFormat.M4A,
        ".m4b": AudioFormat.M4B,
        ".flac": AudioFormat.FLAC,
        ".ogg": AudioFormat.OGG,
        ".opus": AudioFormat.OPUS,
        ".aac": AudioFormat.AAC,
        ".wav": AudioFormat.WAV,
    }

    # Regex patterns for extracting track/disc numbers from filenames
    TRACK_PATTERNS = [
        re.compile(r"^(\d{1,3})", re.IGNORECASE),  # Leading digits
        re.compile(r"track\s*(\d+)", re.IGNORECASE),
        re.compile(r"ch(?:apter)?\s*(\d+)", re.IGNORECASE),
        re.compile(r"part\s*(\d+)", re.IGNORECASE),
        re.compile(r"(\d+)\s*[-_.]\s*", re.IGNORECASE),  # Number followed by separator
    ]

    DISC_PATTERNS = [
        re.compile(r"^disc(?:\s*|[-_])?(\d+)$", re.IGNORECASE),
        re.compile(r"^cd(?:\s*|[-_])?(\d+)$", re.IGNORECASE),
        re.compile(r"^book\s*(\d+)$", re.IGNORECASE),
        re.compile(r"^volume\s*(\d+)$", re.IGNORECASE),
        re.compile(r"^vol\.?\s*(\d+)$", re.IGNORECASE),
    ]
    SUFFIX_DISC_PATTERN = re.compile(
        r"^(?P<stem>.+?)[\s\-_]+(?:disc|cd|disk|part|pt)[\s\-_]*(?P<num>\d+)$",
        re.IGNORECASE,
    )
    STANDALONE_BOOK_MIN_DURATION = 60 * 60

    def _album_disc_split(self, album: str | None) -> tuple[str | None, int | None]:
        """Split an album tag like 'On Combat Disc 3' into (stem, disc_number).

        Per-disc album tags are common in CD rips and survive bookbot's own
        apply (which renames files but never rewrites tags). Treating each
        per-disc album as a distinct book makes a just-applied library
        re-fragment on the next scan, so the disc suffix must be recognized
        as disc evidence rather than book identity.
        """
        if album is None:
            return None, None
        split = self._disc_suffix_split(album.strip())
        if split is None:
            return album, None
        stem, number = split
        return (stem or album), number

    def _disc_suffix_split(self, name: str) -> tuple[str, int] | None:
        """Split "Stem Disc 3" / "Stem (Disc 03)" into ("Stem", 3)."""
        match = self.SUFFIX_DISC_PATTERN.match(name) or _BRACKETED_DISC_RE.match(name)
        if match is None:
            return None
        try:
            number = int(match.group("num"))
        except ValueError:
            return None
        return match.group("stem").strip(), number

    def __init__(self, recursive: bool = True, max_depth: int = 5):
        self.recursive = recursive
        self.max_depth = max_depth

    def scan_directory(
        self,
        path: Path,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[AudiobookSet]:
        """Scan a directory for audio files and group them into audiobook sets.

        ``progress`` is called as ``progress(done, total, name)`` before each
        audiobook set is read, so long scans can report how far along they are.
        """
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Path does not exist or is not a directory: {path}")

        audio_files = self._find_audio_files(path)
        if not audio_files:
            return []

        # Group files by their likely audiobook sets
        grouped_files = self._group_files_by_audiobook(audio_files)

        # Convert groups to AudiobookSet objects
        audiobook_sets = []
        total = len(grouped_files)
        source_paths = {
            group_path: (
                group_path
                if group_path.exists() and group_path.is_dir()
                else group_path.parent
            )
            for group_path in grouped_files
        }
        sets_per_folder = Counter(source_paths.values())
        for index, (group_path, files) in enumerate(grouped_files.items()):
            source_path = source_paths[group_path]
            if progress is not None:
                progress(index, total, source_path.name)
            # Files lying directly in a folder that also holds other books
            # (the library root, usually) are "loose": that folder's name is
            # not theirs.
            loose = all(file.parent == source_path for file in files) and (
                sets_per_folder[source_path] > 1
                or (source_path == path and len(grouped_files) > 1)
            )
            audiobook_set = self._create_audiobook_set(
                source_path,
                files,
                # A metadata.json describes one book; loose files sharing a
                # folder must not all inherit it.
                read_sidecar=sets_per_folder[source_path] == 1 and not loose,
                loose=loose,
            )
            audiobook_sets.append(audiobook_set)

        return audiobook_sets

    def _find_audio_files(self, path: Path, current_depth: int = 0) -> list[Path]:
        """Recursively find all audio files in a directory."""
        if path.name == QUARANTINE_DIRNAME:
            return []

        audio_files = []

        try:
            for item in path.iterdir():
                if is_ignored_file(item):
                    continue
                if item.is_file() and item.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    audio_files.append(item)
                elif item.is_dir():
                    if item.name == QUARANTINE_DIRNAME:
                        continue
                    if (
                        self.recursive
                        and current_depth < self.max_depth
                        and not item.name.startswith(".")
                    ):
                        audio_files.extend(
                            self._find_audio_files(item, current_depth + 1)
                        )
        except PermissionError:
            # Skip directories we can't read
            pass

        return sorted(audio_files, key=natural_sort_key)

    def _group_files_by_audiobook(self, files: list[Path]) -> dict[Path, list[Path]]:
        """Group audio files by their likely audiobook sets."""
        groups: dict[Path, list[Path]] = {}

        for file_path in files:
            group_root = self._group_root_for_file(file_path)

            if group_root not in groups:
                groups[group_root] = []
            groups[group_root].append(file_path)

        split_groups: dict[Path, list[Path]] = {}
        synthetic_group_index = 0

        for group_root, group_files in groups.items():
            subgroups = self._split_group_by_album_evidence(group_root, group_files)
            if len(subgroups) == 1:
                split_groups[group_root] = subgroups[0]
                continue

            for subgroup in subgroups:
                synthetic_group_index += 1
                split_groups[
                    group_root / f".__bookbot_group_{synthetic_group_index}__"
                ] = subgroup

        return split_groups

    def _group_root_for_file(self, file_path: Path) -> Path:
        """Resolve the logical group root for a file."""
        parent = file_path.parent
        if parent.parent != parent and self._disc_folder_match(parent) is not None:
            return parent.parent
        return parent

    def _split_group_by_album_evidence(
        self, group_root: Path, files: list[Path]
    ) -> list[list[Path]]:
        """Split same-folder files when tag evidence shows multiple books."""
        if self._is_collapsed_disc_group(group_root, files):
            return [sorted(files)]

        tagged_groups: dict[str, list[Path]] = {}
        untagged_files: list[Path] = []

        for file_path in sorted(files):
            try:
                tags = self._extract_audio_tags(file_path)
            except Exception:
                tags = AudioTags()
            album_stem, _ = self._album_disc_split(tags.album)
            album_key = self._normalize_grouping_text(album_stem)
            if album_key is None:
                untagged_files.append(file_path)
                continue
            tagged_groups.setdefault(album_key, []).append(file_path)

        if not tagged_groups:
            return self._split_untagged_files(untagged_files)

        grouped_files = [sorted(paths) for paths in tagged_groups.values()]
        residual_files: list[Path] = []

        for file_path in untagged_files:
            if self._looks_like_standalone_audiobook_file(file_path):
                grouped_files.append([file_path])
            else:
                residual_files.append(file_path)

        if residual_files:
            if len(grouped_files) == 1:
                grouped_files[0].extend(residual_files)
                grouped_files[0].sort()
            else:
                grouped_files.append(sorted(residual_files))

        return grouped_files

    def _is_collapsed_disc_group(self, group_root: Path, files: list[Path]) -> bool:
        """Detect groups that came from disc-named child folders."""
        if not files:
            return False

        saw_disc_child = False
        for file_path in files:
            parent = file_path.parent
            if parent == group_root:
                continue
            if parent.parent != group_root:
                return False
            if self._disc_folder_match(parent) is None:
                return False
            saw_disc_child = True

        return saw_disc_child

    def _split_untagged_files(self, files: list[Path]) -> list[list[Path]]:
        """Peel clearly standalone files out of fully untagged groups."""
        groups: list[list[Path]] = []
        residual_files: list[Path] = []

        for file_path in sorted(files):
            if self._looks_like_standalone_audiobook_file(file_path):
                groups.append([file_path])
            else:
                residual_files.append(file_path)

        if residual_files:
            groups.append(residual_files)

        return groups or [[]]

    def _normalize_grouping_text(self, value: str | None) -> str | None:
        """Normalize grouping text like album tags for comparisons."""
        if value is None:
            return None

        normalized = value.strip().lower()
        return normalized or None

    def _looks_like_standalone_audiobook_file(self, file_path: Path) -> bool:
        """Detect files that are very likely to represent complete books."""
        if file_path.suffix.lower() == ".m4b":
            return True

        duration, _, _, _ = self._extract_audio_properties(file_path)
        return duration is not None and duration >= self.STANDALONE_BOOK_MIN_DURATION

    def _looks_like_disc_folder(self, folder_name: str) -> bool:
        """Detect folders like CD1 or Disc 2 that should collapse into one book."""
        return any(pattern.match(folder_name) for pattern in self.DISC_PATTERNS)

    def _disc_folder_match(self, folder: Path) -> DiscFolderMatch | None:
        """Parse a folder name as an exact or suffix-style disc folder."""
        exact_match = self._exact_disc_folder_match(folder.name)
        if exact_match is not None:
            return exact_match

        suffix_split = self._disc_suffix_split(folder.name)
        if suffix_split is None:
            return None

        stem = self._clean_metadata_name(suffix_split[0])
        number = suffix_split[1]

        if not self._suffix_disc_folder_qualifies(folder, stem, number):
            return None

        return DiscFolderMatch(number=number, stem=stem)

    def _exact_disc_folder_match(self, folder_name: str) -> DiscFolderMatch | None:
        """Parse exact disc folder names like `CD1` or `Disc 2`."""
        for pattern in self.DISC_PATTERNS:
            match = pattern.match(folder_name)
            if match is None:
                continue
            try:
                return DiscFolderMatch(number=int(match.group(1)))
            except ValueError:
                return None
        return None

    def _suffix_disc_folder_qualifies(
        self, folder: Path, stem: str, number: int
    ) -> bool:
        """Require sibling or parent evidence before collapsing suffix folders."""
        parent_name = self._clean_metadata_name(folder.parent.name).lower()
        stem_name = stem.lower()
        if stem_name and (parent_name == stem_name or stem_name in parent_name):
            return True

        for sibling in folder.parent.iterdir():
            if not sibling.is_dir() or sibling == folder:
                continue
            sibling_split = self._disc_suffix_split(sibling.name)
            if sibling_split is None:
                continue
            sibling_stem = self._clean_metadata_name(sibling_split[0])
            if sibling_stem.lower() != stem_name:
                continue
            if sibling_split[1] != number:
                return True

        return False

    def _create_audiobook_set(
        self,
        source_path: Path,
        files: list[Path],
        *,
        read_sidecar: bool = True,
        loose: bool = False,
    ) -> AudiobookSet:
        """Create an AudiobookSet from a group of files."""
        tracks = []
        total_duration = 0.0
        disc_numbers = set()

        for file_path in files:
            track = self._create_track_from_file(file_path)
            if track:
                tracks.append(track)
                disc_numbers.add(track.disc)
                if track.duration:
                    total_duration += track.duration

        # Sort tracks by disc, then by track index; files without any order
        # evidence keep a natural filename order ("CD 2" before "CD 10").
        tracks.sort(key=lambda t: (t.disc, t.track_index, natural_sort_key(t.src_path)))

        # Extract metadata guesses from folder name and tracks
        title_guess, author_guess, series_guess, volume_guess = (
            self._extract_metadata_guesses(source_path, tracks, loose=loose)
        )
        if title_guess:
            title_guess = tidy_name_title(title_guess)

        disc_count = len(disc_numbers) if disc_numbers else 1

        # Extract majority ISBN/ASIN from track tags
        isbn_guess = self._majority_identifier(tracks, "isbn")
        asin_guess = self._majority_identifier(tracks, "asin")
        narrator_guess = self._consistent_track_tag_value(tracks, "narrator")
        if narrator_guess is None and not loose:
            narrator_guess = self._nfo_narrator_guess(source_path)
        if narrator_guess is None:
            comment_narrators = self._comment_narrators(tracks[:5])
            narrator_guess = ", ".join(comment_narrators) or None

        audiobook_set = AudiobookSet(
            source_path=source_path,
            raw_title_guess=title_guess,
            author_guess=author_guess,
            series_guess=series_guess,
            volume_guess=volume_guess,
            narrator_guess=narrator_guess,
            isbn_guess=isbn_guess,
            asin_guess=asin_guess,
            disc_count=disc_count,
            total_tracks=len(tracks),
            total_duration=total_duration if total_duration > 0 else None,
            tracks=tracks,
            loose_files=loose,
        )

        if read_sidecar:
            self._apply_sidecar_evidence(audiobook_set)

        if not audiobook_set.raw_title_guess:
            # "New Folder (3)/audio.m4b" with empty tags: guessing a title
            # (or borrowing one from a stray cover) files the book wrong.
            audiobook_set.needs_review = True
            audiobook_set.review_reason = (
                "No usable title: the folder and file names are generic "
                f"({self._describe_names(source_path, tracks)}) and the tags "
                "name no book. Identify it manually."
            )

        # Validate track ordering and add warnings
        validation_issues = audiobook_set.validate_track_order()
        audiobook_set.warnings.extend(validation_issues)

        return audiobook_set

    @staticmethod
    def _describe_names(source_path: Path, tracks: list[Track]) -> str:
        names = [f"'{source_path.name}'"]
        names.extend(f"'{track.src_path.name}'" for track in tracks[:2])
        if len(tracks) > 2:
            names.append("...")
        return ", ".join(names)

    def _apply_sidecar_evidence(self, audiobook_set: AudiobookSet) -> None:
        """Fold a metadata.json/OPF/NFO sidecar into the set's guesses.

        Library managers such as Audiobookshelf leave a curated
        ``metadata.json`` next to each book; its title, authors, narrators,
        identifiers and series are far better search evidence than rip-group
        folder names. Audiobookshelf data is not always clean either
        (narrators listed as authors, "Unabridged" as a series), so obvious
        junk is dropped rather than trusted.
        """
        source_path = audiobook_set.source_path
        if not source_path.is_dir():
            return
        try:
            from ..io.sidecar import SidecarManager

            sidecar = SidecarManager().auto_detect_sidecar(source_path)
        except Exception:
            return
        if sidecar is None:
            return

        narrators = [
            name.strip()
            for name in re.split(r"\s*[,;&]\s*", sidecar.narrator or "")
            if name.strip()
        ]
        narrator_keys = {name.casefold() for name in narrators}
        authors = [
            author
            for author in sidecar.authors
            if author.casefold() not in narrator_keys
            and not self._is_implausible_author_guess(author)
        ]

        title = clean_query_title(sidecar.title)
        if title:
            audiobook_set.raw_title_guess = title

        if authors:
            current = audiobook_set.author_guess
            matcher = AdvancedMatcher()
            agreeing = (
                [a for a in authors if matcher.match_author(current, a) >= 0.85]
                if current
                else []
            )
            audiobook_set.author_guess = agreeing[0] if agreeing else authors[0]

        series = sidecar.series_name
        series_index = sidecar.series_index
        if series and not series_index:
            # NFO files write "Series: Alex Cross, Book 1".
            numbered = re.match(
                r"^(?P<name>.+?)[\s,]+(?:book|vol(?:ume)?|#)\.?\s*(?P<num>\d+)\s*$",
                series,
                re.IGNORECASE,
            )
            if numbered:
                series = numbered.group("name").strip()
                series_index = numbered.group("num")
        if series and audiobook_set.series_guess is None:
            series_key = series.casefold()
            known_people = {a.casefold() for a in sidecar.authors} | narrator_keys
            if (
                series_key not in _JUNK_SIDECAR_SERIES
                and series_key not in known_people
                and series_key != (audiobook_set.author_guess or "").casefold()
            ):
                audiobook_set.series_guess = series
                audiobook_set.volume_guess = series_index or audiobook_set.volume_guess

        if audiobook_set.narrator_guess is None and narrators:
            audiobook_set.narrator_guess = narrators[0]
        if audiobook_set.asin_guess is None and sidecar.asin:
            asin = sidecar.asin.strip().upper()
            if _SIDECAR_ASIN_RE.fullmatch(asin):
                audiobook_set.asin_guess = asin
        if audiobook_set.isbn_guess is None:
            audiobook_set.isbn_guess = sidecar.isbn_13 or sidecar.isbn_10
        if audiobook_set.year_guess is None and sidecar.year:
            audiobook_set.year_guess = sidecar.year

    def _nfo_narrator_guess(self, source_path: Path) -> str | None:
        """Read nearby NFO sidecars for narrator labels when tags are absent."""
        if not source_path.is_dir():
            return None

        for nfo_path in sorted(source_path.glob("*.nfo")):
            try:
                content = nfo_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for line in content.splitlines():
                match = _NFO_NARRATOR_RE.match(line)
                if match is None:
                    continue
                narrator = match.group(1).strip()
                if narrator:
                    return narrator

        return None

    def _create_track_from_file(self, file_path: Path) -> Track | None:
        """Create a Track object from an audio file."""
        stat = None
        try:
            # Get basic file info
            stat = file_path.stat()
            audio_format = self.SUPPORTED_EXTENSIONS.get(file_path.suffix.lower())
            if not audio_format:
                return None

            # Extract metadata using mutagen
            audio_tags = self._extract_audio_tags(file_path)

            # Try to get track and disc numbers from tags first, then filename
            track_num = self._get_track_number(file_path, audio_tags)
            disc_num = self._get_disc_number(file_path, audio_tags)

            # Get audio properties
            duration, bitrate, channels, sample_rate = self._extract_audio_properties(
                file_path
            )

            track = Track(
                src_path=file_path,
                disc=disc_num,
                track_index=track_num,
                duration=duration,
                bitrate=bitrate,
                channels=channels,
                sample_rate=sample_rate,
                file_size=stat.st_size,
                audio_format=audio_format,
                existing_tags=audio_tags,
                status=TrackStatus.VALID,
            )

            return track

        except Exception as e:
            # Return a track with error status for problematic files
            file_size = stat.st_size if stat else 0
            return Track(
                src_path=file_path,
                disc=1,
                track_index=999,  # Put error tracks at the end
                file_size=file_size,
                audio_format=self.SUPPORTED_EXTENSIONS.get(
                    file_path.suffix.lower(), AudioFormat.MP3
                ),
                status=TrackStatus.ERROR,
                warnings=[f"Error reading file: {str(e)}"],
            )

    def _extract_audio_tags(self, file_path: Path) -> AudioTags:
        """Extract metadata tags from an audio file."""
        tags = AudioTags()

        try:
            audio_file = MutagenFile(file_path)
            if audio_file is None:
                return tags

            # Map common tag fields
            tag_mapping = {
                "title": ["TIT2", "TITLE", "\xa9nam"],
                "album": ["TALB", "ALBUM", "\xa9alb"],
                "artist": ["TPE1", "ARTIST", "\xa9ART"],
                "albumartist": ["TPE2", "ALBUMARTIST", "aART"],
                "date": ["TDRC", "DATE", "\xa9day"],
                "genre": ["TCON", "GENRE", "\xa9gen"],
                "track": ["TRCK", "TRACKNUMBER", "trkn"],
                "disc": ["TPOS", "DISCNUMBER", "disk"],
                "isbn": [
                    "TXXX:ISBN",
                    "ISBN",
                    "----:com.apple.iTunes:ISBN",
                ],
                "asin": [
                    "TXXX:ASIN",
                    "TXXX:AUDIBLE_ASIN",
                    "ASIN",
                    "asin",
                    "----:com.apple.iTunes:ASIN",
                    "----:com.pilabor.tone:AUDIBLE_ASIN",
                    "CDEK",
                    "WOAS",
                ],
                "narrator": [
                    "\xa9nrt",
                    "----:com.apple.iTunes:NARRATOR",
                    "TXXX:NARRATEDBY",
                    "TXXX:NARRATOR",
                    "narrator",
                    "narratedby",
                    "TCOM",
                    "\xa9wrt",
                ],
            }

            for field, tag_keys in tag_mapping.items():
                for key in tag_keys:
                    try:
                        if key not in audio_file:
                            continue
                        raw_value = audio_file[key]
                    except Exception:
                        continue

                    value: str | int | None
                    if field in ["track", "disc"]:
                        value = self._normalize_numeric_tag(raw_value)
                    else:
                        value = self._normalize_text_tag(raw_value)
                    if value is None:
                        continue

                    # Validate ISBN/ASIN before setting
                    if field == "isbn" and isinstance(value, str):
                        cleaned = re.sub(r"[-\s]", "", value)
                        if not _ISBN_RE.match(cleaned):
                            continue
                        value = cleaned
                    elif field == "asin" and isinstance(value, str):
                        normalized_asin = self._extract_asin_from_text(value)
                        if normalized_asin is None:
                            continue
                        value = normalized_asin

                    setattr(tags, field, value)
                    break

            if tags.asin is None:
                tags.asin = self._extract_asin_from_url_tags(audio_file)
            if tags.comment is None:
                tags.comment = self._extract_comment_tag(audio_file)

            # Store raw tags for preservation
            try:
                tags.raw_tags = {
                    str(key): normalized
                    for key, value in dict(audio_file).items()
                    if (normalized := self._normalize_raw_tag_value(value)) is not None
                }
            except Exception:
                tags.raw_tags = {}

        except (ID3NoHeaderError, Exception):
            # File has no tags or error reading them
            pass

        return tags

    def _normalize_tag_scalar(self, value: object) -> object | None:
        """Flatten mutagen tag containers into a single scalar value."""
        if value is None:
            return None

        if hasattr(value, "text"):
            return self._normalize_tag_scalar(value.text)

        if hasattr(value, "url"):
            return self._normalize_tag_scalar(value.url)

        if isinstance(value, (list, tuple)):
            for item in value:
                normalized = self._normalize_tag_scalar(item)
                if normalized is not None:
                    return normalized
            return None

        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")

        if isinstance(value, str):
            value = value.strip()
            return value or None

        return value

    def _normalize_text_tag(self, value: object) -> str | None:
        """Normalize a text tag to a plain string."""
        normalized = self._normalize_tag_scalar(value)
        if normalized is None:
            return None

        text = str(normalized).strip()
        return text or None

    def _normalize_raw_tag_value(self, value: object) -> str | None:
        """Normalize arbitrary mutagen values into JSON-safe plain strings."""
        if value is None:
            return None

        if hasattr(value, "text"):
            return self._normalize_raw_tag_value(value.text)

        if hasattr(value, "url"):
            return self._normalize_raw_tag_value(value.url)

        if isinstance(value, (list, tuple)):
            normalized_items = [
                normalized
                for item in value
                if (normalized := self._normalize_raw_tag_value(item)) is not None
            ]
            return "; ".join(normalized_items) or None

        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")

        text = str(value).strip()
        return text or None

    def _extract_asin_from_text(self, value: str) -> str | None:
        """Extract a valid Audible-style ASIN from freeform tag text."""
        cleaned = value.strip()
        if _ASIN_RE.match(cleaned):
            return cleaned.upper()

        match = _ASIN_SEARCH_RE.search(cleaned)
        if match is None:
            return None

        candidate = match.group(1).upper()
        return candidate if _ASIN_RE.match(candidate) else None

    def _extract_comment_tag(self, audio_file: Mapping[Any, Any]) -> str | None:
        """Read a comment tag (ID3 COMM frames carry a language suffix)."""
        try:
            keys = list(audio_file.keys())
        except Exception:
            return None
        for key in keys:
            name = str(key)
            if name.startswith("COMM") or name in {"COMMENT", "comment", "\xa9cmt"}:
                try:
                    value = self._normalize_text_tag(audio_file[key])
                except Exception:
                    continue
                if value:
                    return value
        return None

    def _extract_asin_from_url_tags(self, audio_file: Mapping[Any, Any]) -> str | None:
        """Search URL-style tag frames for Audible product links."""
        try:
            items = audio_file.items()
        except Exception:
            return None

        for key, raw_value in items:
            key_name = str(key).upper()
            if not (key_name.startswith("WOAS") or key_name.startswith("WXXX")):
                continue

            value = self._normalize_text_tag(raw_value)
            if value is None:
                continue

            if (asin := self._extract_asin_from_text(value)) is not None:
                return asin

        return None

    def _extract_audio_properties(
        self, file_path: Path
    ) -> tuple[float | None, int | None, int | None, int | None]:
        """Extract audio properties (duration, bitrate, channels, sample rate)."""
        try:
            audio_file = MutagenFile(file_path)
            if audio_file is None or audio_file.info is None:
                return None, None, None, None

            info = audio_file.info
            duration = getattr(info, "length", None)
            bitrate = getattr(info, "bitrate", None)
            channels = getattr(info, "channels", None)
            sample_rate = getattr(info, "sample_rate", None)

            return duration, bitrate, channels, sample_rate

        except Exception:
            return None, None, None, None

    def _normalize_numeric_tag(self, value: object) -> int | None:
        """Attempt to normalize a numeric tag value to an integer."""
        normalized = self._normalize_tag_scalar(value)
        if normalized is None:
            return None

        if isinstance(normalized, int):
            return normalized

        if isinstance(normalized, str):
            # Accept common patterns such as ``1/10`` or ``01``
            match = re.search(r"\d+", normalized)
            if match:
                try:
                    return int(match.group())
                except ValueError:
                    return None

        return None

    def _get_track_number(self, file_path: Path, tags: AudioTags) -> int:
        """Extract track number from tags or filename."""
        # Try tags first
        from_tags = self._normalize_numeric_tag(tags.track)
        if from_tags is not None:
            return from_tags

        # Title tags that spell the position out ("The Inmate Pt07 of 58",
        # "21-91 Along Came a Spider") beat file names, which are often
        # renamed or shuffled.
        title = str(tags.title or "")
        explicit = self._explicit_title_position(title)
        if explicit is not None:
            return explicit

        # Try filename patterns
        filename = file_path.stem
        for pattern in self.TRACK_PATTERNS:
            match = pattern.search(filename)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue

        # "... (Unabridged) - 012.mp3": a numeric suffix after a separator.
        trailing = _TRAILING_TRACK_NUMBER_RE.search(filename)
        if trailing:
            return int(trailing.group(1))

        # A bare "Part 12" in the title tag.
        part = _TITLE_PART_RE.search(title)
        if part:
            return int(part.group(1))

        # Default to 1 if no track number found
        return 1

    @staticmethod
    def _explicit_title_position(title: str) -> int | None:
        """Position spelled out with its total in a title tag, if any."""
        for pattern in (_TITLE_PART_OF_RE, _TITLE_N_OF_M_RE):
            match = pattern.search(title)
            if match:
                return int(match.group(1))
        dashed = _TITLE_N_DASH_M_RE.match(title)
        if dashed and int(dashed.group(1)) <= int(dashed.group(2)):
            return int(dashed.group(1))
        return None

    def _get_disc_number(self, file_path: Path, tags: AudioTags) -> int:
        """Extract disc number from tags, filename, or directory structure."""
        # Try tags first
        from_tags = self._normalize_numeric_tag(tags.disc)
        if from_tags is not None:
            return from_tags

        # Per-disc album tags ("On Combat Disc 3") are disc evidence too;
        # without this, merged multi-disc folders lose disc identity after
        # apply and track indices collide on rescan.
        _, album_disc = self._album_disc_split(tags.album)
        if album_disc is not None:
            return album_disc

        exact_filename_match = self._exact_disc_folder_match(file_path.stem)
        if exact_filename_match is not None:
            return exact_filename_match.number

        parent = file_path.parent
        depth = 0
        while parent != parent.parent and depth < 3:
            folder_match = self._disc_number_from_folder_name(parent.name)
            if folder_match is not None:
                return folder_match
            parent = parent.parent
            depth += 1

        # Default to disc 1
        return 1

    def _disc_number_from_folder_name(self, folder_name: str) -> int | None:
        """Extract a disc number from an exact or suffix-style folder name."""
        exact_match = self._exact_disc_folder_match(folder_name)
        if exact_match is not None:
            return exact_match.number

        suffix_split = self._disc_suffix_split(folder_name)
        return suffix_split[1] if suffix_split is not None else None

    @staticmethod
    def _majority_identifier(tracks: list[Track], field: str) -> str | None:
        """Return the majority ISBN/ASIN across tracks that carry the tag.

        Requires agreement from >50% of tracks that have the tag set.
        """
        values: list[str] = []
        for track in tracks:
            val = getattr(track.existing_tags, field, None)
            if val:
                values.append(str(val))

        if not values:
            return None

        counter = Counter(values)
        most_common_val, count = counter.most_common(1)[0]
        # Require >50% agreement among tracks that carry the tag
        if count > len(values) / 2:
            return most_common_val
        return None

    @staticmethod
    def _consistent_track_tag_value(tracks: list[Track], field: str) -> str | None:
        """Return a shared text tag when the sampled tracks agree."""
        values = {
            str(value)
            for track in tracks[:5]
            if (value := getattr(track.existing_tags, field, None))
        }
        return next(iter(values)) if len(values) == 1 else None

    def _extract_metadata_guesses(
        self, source_path: Path, tracks: list[Track], *, loose: bool = False
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract title, author, series, and volume guesses from path and tracks.

        ``loose`` marks files lying directly in a folder shared with other
        books (typically the library root); that folder's name says nothing
        about them.
        """
        if len(tracks) == 1:
            return self._extract_single_track_guesses(tracks[0], loose=loose)

        tag_author = self._tag_author_guess(tracks)
        tag_title = self._tag_title_guess(tracks)
        folder_name, prefer_folder_title, _collapsed_disc_group = (
            self._group_title_source(source_path, tracks)
        )

        if loose or is_generic_name(folder_name):
            # "audiobook_final_FINAL", "New Folder (3)" or the library root:
            # only the tags know which book this is.
            return self._guesses_from_tags(tracks, tag_title, tag_author)

        folder_volume: str | None = None
        volume_prefix = _VOLUME_PREFIX_RE.match(folder_name)
        if volume_prefix:
            folder_volume = volume_prefix.group(1)
            folder_name = volume_prefix.group(2).strip()
        else:
            series_split = self._series_number_title_guess(folder_name)
            if series_split is not None:
                series_name, volume, title, folder_author = series_split
                author = (
                    folder_author
                    or tag_author
                    or self._parent_author_guess(source_path.parent)
                )
                return title, author, series_name, volume

        # "Along Came a Spider by James Patterson"
        title_by_author = self._title_by_author_guess(folder_name, tag_author)
        if title_by_author is not None:
            by_title, by_author = title_by_author
            by_series, by_volume = self._album_series_guess(tracks, by_title)
            return by_title, by_author, by_series, by_volume or folder_volume

        # Try to extract series and volume info
        series_match = re.search(
            r"(.+?)\s+(?:book|vol|volume)\s*(\d+)", folder_name, re.IGNORECASE
        )
        if series_match:
            return (
                folder_name,
                None,
                series_match.group(1).strip(),
                series_match.group(2),
            )

        # Author - Title, or Title - Author when the tags or the shape of
        # the names say so.
        title_guess, author_guess = self._oriented_title_author(folder_name, tag_author)
        if title_guess is not None:
            if author_guess is not None:
                album_series, album_volume = self._album_series_guess(
                    tracks, title_guess
                )
                return (
                    title_guess,
                    author_guess,
                    album_series,
                    album_volume or folder_volume,
                )
            folder_name = title_guess

        parent_author = self._parent_author_guess(source_path.parent)
        # Inside an Author/Title layout (e.g. bookbot's own output), the
        # folder name *is* the title; preferring a stray album tag over it
        # makes a just-organized library propose fresh moves on rescan.
        in_author_title_layout = parent_author is not None and bool(
            _LAST_FIRST_AUTHOR_RE.fullmatch(
                self._clean_metadata_name(source_path.parent.name)
            )
        )

        title_guess = (
            folder_name
            if prefer_folder_title or in_author_title_layout or tag_title is None
            else tag_title
        )
        author_guess = tag_author or parent_author

        if folder_volume is None and title_guess:
            series_split = self._series_number_title_guess(title_guess)
            if series_split is not None:
                series_name, volume, title, split_author = series_split
                return title, author_guess or split_author, series_name, volume

        return title_guess, author_guess, None, folder_volume

    def _guesses_from_tags(
        self, tracks: list[Track], tag_title: str | None, tag_author: str | None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Guesses for a set whose folder name is useless."""
        author = tag_author
        if tag_title is not None:
            series_split = self._series_number_title_guess(tag_title)
            if series_split is not None:
                series_name, volume, title, split_author = series_split
                return title, author or split_author, series_name, volume
            album_series, album_volume = self._album_series_guess(tracks, tag_title)
            return tag_title, author, album_series, album_volume

        stem = self._track_title_stem(tracks)
        if stem is None:
            return None, author, None, None
        series_split = self._series_number_title_guess(stem)
        if series_split is not None:
            series_name, volume, title, split_author = series_split
            return title, author or split_author, series_name, volume
        # Track titles such as "Freida McFadden - The Inmate Pt01 of 58".
        stem_title, stem_author = self._oriented_title_author(stem, tag_author)
        if stem_title is not None and stem_author is not None:
            return stem_title, author or stem_author, None, None
        return stem, author, None, None

    def _group_title_source(
        self, source_path: Path, tracks: list[Track]
    ) -> tuple[str, bool, bool]:
        """Choose the best folder-derived title source for a grouped set."""
        folder_name = self._clean_metadata_name(source_path.name)
        disc_stems: set[str] = set()
        collapsed_disc_group = False

        for track in tracks:
            parent = track.src_path.parent
            if parent.parent != source_path:
                continue
            match = self._disc_folder_match(parent)
            if match is None:
                continue
            collapsed_disc_group = True
            if match.stem:
                disc_stems.add(self._clean_metadata_name(match.stem))

        if not collapsed_disc_group:
            return folder_name, False, False

        if len(disc_stems) == 1:
            stem = next(iter(disc_stems))
            normalized_folder = folder_name.lower()
            normalized_stem = stem.lower()
            if (
                normalized_folder == normalized_stem
                or normalized_stem in normalized_folder
            ):
                return folder_name, True, True
            return stem, True, True

        return folder_name, True, True

    def _extract_single_track_guesses(
        self, track: Track, *, loose: bool = False
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Prefer file-level metadata for single-file audiobook sets.

        Placeholder tags ("Unknown Book-Part1", "Track 01") are ignored, and
        a generic file name ("audio.m4b") falls back to the folder name
        unless the file lies loose in a folder shared with other books.
        """
        tags = track.existing_tags
        tag_author = self._tag_author_guess([track])
        tag_title: str | None = None
        for value in (tags.album, tags.title):
            if not value:
                continue
            candidate = self._clean_tag_value(str(value))
            candidate = self._album_disc_split(candidate)[0] or candidate
            if not is_placeholder_tag(candidate):
                tag_title = candidate
                break

        folder = track.src_path.parent
        name_title, name_author, series_guess, volume_guess = (
            None,
            None,
            None,
            None,
        )
        if not is_generic_name(self._clean_metadata_name(track.src_path.stem)):
            name_title, name_author, series_guess, volume_guess = (
                self._extract_name_guesses(track.src_path.stem, tag_author)
            )
        if name_title is None and not loose:
            if not is_generic_name(self._clean_metadata_name(folder.name)):
                name_title, name_author, series_guess, volume_guess = (
                    self._extract_name_guesses(folder.name, tag_author)
                )
        parent_author = None if loose else self._parent_author_guess(folder)

        title_guess = tag_title
        author_guess = tag_author
        if title_guess:
            series_split = self._series_number_title_guess(
                self._clean_metadata_name(title_guess)
            )
            if series_split is not None:
                series_guess, volume_guess, title_guess, split_author = series_split
                author_guess = author_guess or split_author

        return (
            title_guess or name_title,
            author_guess or name_author or parent_author,
            series_guess,
            volume_guess,
        )

    def _extract_name_guesses(
        self, raw_name: str, tag_author: str | None = None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract metadata guesses from a folder or filename stem."""
        cleaned_name = self._clean_metadata_name(raw_name)

        series_split = self._series_number_title_guess(cleaned_name)
        if series_split is not None:
            series_name, volume, title, author = series_split
            return title, author or tag_author, series_name, volume

        title_by_author = self._title_by_author_guess(cleaned_name, tag_author)
        if title_by_author is not None:
            return title_by_author[0], title_by_author[1], None, None

        series_match = re.search(
            r"(.+?)\s+(?:book|vol|volume)\s*(\d+)", cleaned_name, re.IGNORECASE
        )
        if series_match:
            series_name = series_match.group(1).strip()
            volume = series_match.group(2)
            return cleaned_name, None, series_name, volume

        title_guess, author_guess = self._oriented_title_author(
            cleaned_name, tag_author
        )
        if title_guess is not None:
            return title_guess, author_guess, None, None

        return cleaned_name or None, None, None, None

    def _split_dash_name(self, cleaned_name: str) -> tuple[str, str] | None:
        """Split names that use a dash as the primary separator."""
        author_title_match = re.search(r"^(.+?)\s*[-–—]\s*(.+)$", cleaned_name)
        if author_title_match is None:
            return None

        return (
            author_title_match.group(1).strip(),
            author_title_match.group(2).strip(),
        )

    def _oriented_title_author(
        self, cleaned_name: str, tag_author: str | None = None
    ) -> tuple[str | None, str | None]:
        """Split "X - Y" into (title, author), deciding which side is which.

        The artist/album-artist tag decides first ("The Silent Patient -
        Alex Michaelides" with artist Alex Michaelides). Without tag
        evidence, a personal name on exactly one side marks the author;
        otherwise the usual "Author - Title" order applies.
        """
        split_name = self._split_dash_name(cleaned_name)
        if split_name is None:
            return None, None

        left, right = split_name
        if tag_author:
            left_is_author = self._names_match(tag_author, left)
            right_is_author = self._names_match(tag_author, right)
            if right_is_author and not left_is_author:
                return left, right
            if left_is_author and not right_is_author:
                return right, left

        left_is_personal_name = self._looks_like_author(left)
        right_is_personal_name = self._looks_like_author(right)
        if right_is_personal_name and not left_is_personal_name:
            return left, right
        if left_is_personal_name and not right_is_personal_name:
            return right, left

        return self._author_title_guess(cleaned_name)

    def _collapsed_group_author_title_guess(
        self, cleaned_name: str
    ) -> tuple[str | None, str | None]:
        """Prefer the personal-name side when collapsed disc groups use `X - Y`."""
        return self._oriented_title_author(cleaned_name)

    def _title_by_author_guess(
        self, cleaned_name: str, tag_author: str | None = None
    ) -> tuple[str, str] | None:
        """Parse "Title by Author" when the part after "by" is a person."""
        match = _TITLE_BY_AUTHOR_RE.match(cleaned_name)
        if match is None:
            return None
        title = match.group("title").strip(" -")
        author = match.group("author").strip(" -")
        if not title or not author:
            return None
        if tag_author and self._names_match(tag_author, author):
            return title, author
        if self._looks_like_author(author):
            return title, author
        return None

    def _author_title_guess(self, cleaned_name: str) -> tuple[str | None, str | None]:
        """Split `Author - Title` names while rejecting implausible authors."""
        split_name = self._split_dash_name(cleaned_name)
        if split_name is None:
            return None, None

        author_guess, title_guess = split_name
        if self._is_implausible_author_guess(author_guess):
            return title_guess, None
        return title_guess, author_guess

    def _parent_author_guess(self, parent_path: Path) -> str | None:
        """Extract a plausible author from a parent folder name."""
        cleaned = self._clean_metadata_name(parent_path.name)
        _, parent_author = self._author_title_guess(cleaned)
        if parent_author is not None:
            return parent_author

        # A parent folder that IS an author name in "Last, First" form is
        # exactly what bookbot's own {AuthorLastFirst}/{Title} output looks
        # like; without accepting it, rescanning an organized library loses
        # the author and proposes moves into "Unknown Author".
        if _LAST_FIRST_AUTHOR_RE.fullmatch(cleaned) and not (
            self._is_implausible_author_guess(cleaned)
        ):
            return cleaned
        return None

    def _is_implausible_author_guess(self, author_guess: str) -> bool:
        """Reject short codes and heavily numeric prefixes as author guesses."""
        cleaned_author = author_guess.strip()
        if not cleaned_author:
            return False

        if _IMPLAUSIBLE_AUTHOR_RE.fullmatch(cleaned_author):
            return True
        if _VOLUME_ONLY_RE.fullmatch(cleaned_author):
            return True

        non_space = [char for char in cleaned_author if not char.isspace()]
        if non_space:
            digit_ratio = sum(char.isdigit() for char in non_space) / len(non_space)
            if digit_ratio > 0.5:
                return True

        tokens = cleaned_author.split()
        return len(tokens) == 1 and len(tokens[0]) <= 3

    def _looks_like_personal_name(self, value: str) -> bool:
        """Detect human-name shapes to disambiguate `Title - Author` folders."""
        tokens = value.split()
        if not 2 <= len(tokens) <= 4:
            return False

        word_like_tokens = 0
        for token in tokens:
            stripped_letters = re.sub(r"[^A-Za-z]", "", token)
            if not stripped_letters or token[0].isdigit():
                return False
            if stripped_letters.lower() in _PERSONAL_NAME_STOPWORDS:
                return False
            if not _PERSONAL_NAME_TOKEN_RE.fullmatch(token):
                return False
            if len(stripped_letters) > 1:
                word_like_tokens += 1

        return word_like_tokens >= 1

    def _clean_metadata_name(self, raw_name: str) -> str:
        """Remove common noise from folder names and filename stems."""
        # Replace noise with a space (not "") so neighbouring words never fuse:
        # "dune (unabridged) [2007] 128kbps" used to become "dune128kbps".
        cleaned_name = re.sub(r"\[.*?\]", " ", strip_decorations(raw_name))
        cleaned_name = re.sub(r"\{.*?\}", " ", cleaned_name)
        cleaned_name = re.sub(r"\(.*?\)", " ", cleaned_name)
        cleaned_name = _BITRATE_NOISE_RE.sub(" ", cleaned_name)
        cleaned_name = _RUNTIME_NOISE_RE.sub(" ", cleaned_name)
        cleaned_name = _SIZE_NOISE_RE.sub(" ", cleaned_name)
        cleaned_name = re.sub(r"[\s_]+", " ", cleaned_name).strip()
        cleaned_name = _TRAILING_FORMAT_NOISE_RE.sub("", cleaned_name)
        cleaned_name = _LEADING_YEAR_RE.sub("", cleaned_name)
        cleaned_name = re.sub(r"\s*(?:\.{3,})$", "", cleaned_name)
        return re.sub(r"[\s~@\-\u2013\u2014]+$", "", cleaned_name).strip()

    def _series_number_title_guess(
        self, name: str
    ) -> tuple[str, str, str, str | None] | None:
        """Split "[Author - ]Series NN - Title" into (series, volume, title, author).

        "Earthsea 01 - A Wizard of Earthsea" is a series and book number, not
        an author named "Earthsea 01". Names such as "Catch 22 - Joseph
        Heller" (an unpadded number followed by a personal name) are left for
        the plain Title - Author handling.
        """
        match = _SERIES_NUMBER_TITLE_RE.match(name)
        if match is None:
            return None
        series = match.group("series").strip(" _-#")
        title = match.group("title").strip()
        number = match.group("number")
        author = match.group("author")
        if not series or not title or _VOLUME_ONLY_RE.fullmatch(f"{series} 1"):
            return None
        if not number.startswith("0") and self._looks_like_personal_name(title):
            return None
        if author is not None:
            author = author.strip()
            if not author or self._is_implausible_author_guess(author):
                author = None
        return series, str(int(number)), title, author

    def _tag_author_guess(self, tracks: list[Track]) -> str | None:
        """Album artist, else artist, when the first tracks agree on it.

        Placeholders ("Unknown Artist") are ignored, and so is an artist the
        comment tag names as the reader ("Read by Wil Wheaton"): rippers
        often put the narrator in the artist field.
        """
        narrators = {name.casefold() for name in self._comment_narrators(tracks[:5])}
        for field in ("albumartist", "artist"):
            values = {
                str(value).strip()
                for track in tracks[:5]
                if (value := getattr(track.existing_tags, field, None))
            }
            if len(values) != 1:
                continue
            value = values.pop()
            if is_placeholder_tag(value):
                continue
            people = {person.casefold() for person in _split_people(value)}
            if people and people <= narrators:
                continue
            return value
        return None

    def _comment_narrators(self, tracks: list[Track]) -> list[str]:
        """Narrators named in comment tags ("Read by Pam Ward")."""
        names: list[str] = []
        for track in tracks:
            comment = track.existing_tags.comment
            if not comment:
                continue
            match = _COMMENT_NARRATOR_RE.search(str(comment))
            if match is None:
                continue
            for name in _split_people(match.group("name")):
                if self._looks_like_personal_name(name) and name not in names:
                    names.append(name)
        return names

    def _tag_title_guess(self, tracks: list[Track]) -> str | None:
        """The album tag shared by every track, minus disc suffix and noise."""
        albums: set[str] = set()
        for track in tracks:
            album = track.existing_tags.album
            if not album:
                continue
            cleaned = self._clean_tag_value(str(album))
            stem = self._album_disc_split(cleaned)[0] or cleaned
            albums.add(stem)
        if len(albums) != 1:
            return None
        album = albums.pop()
        return None if is_placeholder_tag(album) else album

    def _track_title_stem(self, tracks: list[Track]) -> str | None:
        """Common title-tag stem once part numbers are removed.

        "Freida McFadden - The Inmate Pt01 of 58" and "...Pt02 of 58" share
        the stem "Freida McFadden - The Inmate".
        """
        stems: set[str] = set()
        for track in tracks:
            title = track.existing_tags.title
            if not title:
                return None
            stem = re.sub(r"^\d{1,4}-\d{1,4}\s+", "", str(title))
            stem = re.sub(
                r"[\s_-]*\b(?:pt|part)\.?\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$",
                "",
                stem,
                flags=re.IGNORECASE,
            )
            stem = re.sub(r"[\s_-]+\d{1,4}$", "", stem)
            stems.add(self._clean_tag_value(stem))
        if len(stems) != 1:
            return None
        stem = stems.pop()
        return None if is_placeholder_tag(stem) else stem

    def _album_series_guess(
        self, tracks: list[Track], title: str
    ) -> tuple[str | None, str | None]:
        """Series from an album tag such as "Alex Cross 01" next to a real title."""
        albums = {
            self._clean_tag_value(str(track.existing_tags.album))
            for track in tracks
            if track.existing_tags.album
        }
        if len(albums) != 1:
            return None, None
        album = albums.pop()
        match = _SERIES_NUMBER_ONLY_RE.fullmatch(album)
        if match is None or album.casefold() == title.casefold():
            return None, None
        series = match.group("series").strip(" -#,")
        if not series or is_placeholder_tag(series):
            return None, None
        return series, str(int(match.group("number")))

    @staticmethod
    def _clean_tag_value(value: str) -> str:
        """Drop release noise such as "(Unabridged)" from a tag value."""
        cleaned = _TAG_NOISE_RE.sub("", strip_decorations(value))
        return " ".join(cleaned.split())

    def _names_match(self, first: str, second: str) -> bool:
        """Whether two author strings name the same people."""
        matcher = getattr(self, "_matcher", None)
        if matcher is None:
            matcher = AdvancedMatcher()
            self._matcher = matcher
        if matcher.match_author(first, second) >= 0.85:
            return True
        first_people = {p.casefold() for p in _split_people(first)}
        second_people = {p.casefold() for p in _split_people(second)}
        return bool(first_people) and first_people == second_people

    def _looks_like_author(self, value: str) -> bool:
        """A personal name, or a list of them ("Jocko Willink, Leif Babin")."""
        people = _split_people(value)
        return bool(people) and all(
            self._looks_like_personal_name(person) for person in people
        )
