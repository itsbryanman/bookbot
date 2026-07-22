"""Audio file discovery and metadata extraction."""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from .models import AudiobookSet, AudioFormat, AudioTags, Track, TrackStatus

# ISBN validation: 10 digits (trailing X allowed) or 13 digits, after stripping
# hyphens/spaces
_ISBN_RE = re.compile(r"^\d{9}[\dXx]$|^\d{13}$")
# ASIN validation: B0 followed by 8 alphanumeric chars
_ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$", re.IGNORECASE)


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

    def __init__(self, recursive: bool = True, max_depth: int = 5):
        self.recursive = recursive
        self.max_depth = max_depth

    def scan_directory(self, path: Path) -> list[AudiobookSet]:
        """Scan a directory for audio files and group them into audiobook sets."""
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Path does not exist or is not a directory: {path}")

        audio_files = self._find_audio_files(path)
        if not audio_files:
            return []

        # Group files by their likely audiobook sets
        grouped_files = self._group_files_by_audiobook(audio_files)

        # Convert groups to AudiobookSet objects
        audiobook_sets = []
        for group_path, files in grouped_files.items():
            source_path = (
                group_path
                if group_path.exists() and group_path.is_dir()
                else group_path.parent
            )
            audiobook_set = self._create_audiobook_set(source_path, files)
            audiobook_sets.append(audiobook_set)

        return audiobook_sets

    def _find_audio_files(self, path: Path, current_depth: int = 0) -> list[Path]:
        """Recursively find all audio files in a directory."""
        audio_files = []

        try:
            for item in path.iterdir():
                if item.is_file() and item.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    audio_files.append(item)
                elif (
                    item.is_dir()
                    and self.recursive
                    and current_depth < self.max_depth
                    and not item.name.startswith(".")
                ):
                    audio_files.extend(self._find_audio_files(item, current_depth + 1))
        except PermissionError:
            # Skip directories we can't read
            pass

        return sorted(audio_files)

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
            album_key = self._normalize_grouping_text(tags.album)
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
        return (
            duration is not None and duration >= self.STANDALONE_BOOK_MIN_DURATION
        )

    def _looks_like_disc_folder(self, folder_name: str) -> bool:
        """Detect folders like CD1 or Disc 2 that should collapse into one book."""
        return any(pattern.match(folder_name) for pattern in self.DISC_PATTERNS)

    def _disc_folder_match(self, folder: Path) -> DiscFolderMatch | None:
        """Parse a folder name as an exact or suffix-style disc folder."""
        exact_match = self._exact_disc_folder_match(folder.name)
        if exact_match is not None:
            return exact_match

        suffix_match = self.SUFFIX_DISC_PATTERN.match(folder.name)
        if suffix_match is None:
            return None

        stem = self._clean_metadata_name(suffix_match.group("stem"))
        try:
            number = int(suffix_match.group("num"))
        except ValueError:
            return None

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
            sibling_match = self.SUFFIX_DISC_PATTERN.match(sibling.name)
            if sibling_match is None:
                continue
            sibling_stem = self._clean_metadata_name(sibling_match.group("stem"))
            if sibling_stem.lower() != stem_name:
                continue
            try:
                sibling_number = int(sibling_match.group("num"))
            except ValueError:
                continue
            if sibling_number != number:
                return True

        return False

    def _create_audiobook_set(
        self, source_path: Path, files: list[Path]
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

        # Sort tracks by disc, then by track index
        tracks.sort(key=lambda t: (t.disc, t.track_index))

        # Extract metadata guesses from folder name and tracks
        title_guess, author_guess, series_guess, volume_guess = (
            self._extract_metadata_guesses(source_path, tracks)
        )

        disc_count = max(disc_numbers) if disc_numbers else 1

        # Extract majority ISBN/ASIN from track tags
        isbn_guess = self._majority_identifier(tracks, "isbn")
        asin_guess = self._majority_identifier(tracks, "asin")
        narrator_guess = self._consistent_track_tag_value(tracks, "narrator")

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
        )

        # Validate track ordering and add warnings
        validation_issues = audiobook_set.validate_track_order()
        audiobook_set.warnings.extend(validation_issues)

        return audiobook_set

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
                    "ASIN",
                    "----:com.apple.iTunes:ASIN",
                    "CDEK",
                ],
                "narrator": [
                    "\xa9nrt",
                    "----:com.apple.iTunes:NARRATOR",
                    "TXXX:NARRATEDBY",
                    "TXXX:NARRATOR",
                    "narrator",
                    "narratedby",
                    "TCOM",
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

                    if field in ["track", "disc"]:
                        value = self._normalize_numeric_tag(raw_value)
                    else:
                        value = self._normalize_text_tag(raw_value)
                    if value is None:
                        continue

                    # Validate ISBN/ASIN before setting
                    if field == "isbn":
                        cleaned = re.sub(r"[-\s]", "", value)
                        if not _ISBN_RE.match(cleaned):
                            continue
                        value = cleaned
                    elif field == "asin":
                        cleaned = value.strip()
                        if not _ASIN_RE.match(cleaned):
                            continue
                        value = cleaned.upper()

                    setattr(tags, field, value)
                    break

            # Store raw tags for preservation
            try:
                tags.raw_tags = {
                    str(key): normalized
                    for key, value in dict(audio_file).items()
                    if (
                        normalized := self._normalize_raw_tag_value(value)
                    ) is not None
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

        # Try filename patterns
        filename = file_path.stem
        for pattern in self.TRACK_PATTERNS:
            match = pattern.search(filename)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue

        # Default to 1 if no track number found
        return 1

    def _get_disc_number(self, file_path: Path, tags: AudioTags) -> int:
        """Extract disc number from tags, filename, or directory structure."""
        # Try tags first
        from_tags = self._normalize_numeric_tag(tags.disc)
        if from_tags is not None:
            return from_tags

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

        suffix_match = self.SUFFIX_DISC_PATTERN.match(folder_name)
        if suffix_match is None:
            return None

        try:
            return int(suffix_match.group("num"))
        except ValueError:
            return None

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
        self, source_path: Path, tracks: list[Track]
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract title, author, series, and volume guesses from path and tracks."""
        if len(tracks) == 1:
            title_guess, author_guess, series_guess, volume_guess = (
                self._extract_single_track_guesses(tracks[0])
            )
            return title_guess, author_guess, series_guess, volume_guess

        folder_name, prefer_folder_title = self._group_title_source(source_path, tracks)

        # Try to extract series and volume info
        series_match = re.search(
            r"(.+?)\s+(?:book|vol|volume)\s*(\d+)", folder_name, re.IGNORECASE
        )
        if series_match:
            series_name = series_match.group(1).strip()
            volume = series_match.group(2)
            title_guess = folder_name
            return title_guess, None, series_name, volume

        # Try author - title pattern
        author_title_match = re.search(r"^(.+?)\s*[-–—]\s*(.+)$", folder_name)
        if author_title_match:
            author_guess = author_title_match.group(1).strip()
            title_guess = author_title_match.group(2).strip()
            return title_guess, author_guess, None, None

        # Look for consistent album/artist info in track tags
        albums = set()
        artists = set()
        albumartists = set()

        for track in tracks[:5]:  # Check first 5 tracks for consistency
            if track.existing_tags.album:
                albums.add(str(track.existing_tags.album))
            if track.existing_tags.artist:
                artists.add(str(track.existing_tags.artist))
            if track.existing_tags.albumartist:
                albumartists.add(str(track.existing_tags.albumartist))

        title_guess = (
            folder_name
            if prefer_folder_title
            else albums.pop() if len(albums) == 1 else folder_name
        )
        author_guess = (
            albumartists.pop()
            if len(albumartists) == 1
            else artists.pop()
            if len(artists) == 1
            else None
        )

        return title_guess, author_guess, None, None

    def _group_title_source(
        self, source_path: Path, tracks: list[Track]
    ) -> tuple[str, bool]:
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
            return folder_name, False

        if len(disc_stems) == 1:
            stem = next(iter(disc_stems))
            normalized_folder = folder_name.lower()
            normalized_stem = stem.lower()
            if (
                normalized_folder == normalized_stem
                or normalized_stem in normalized_folder
            ):
                return folder_name, True
            return stem, True

        return folder_name, True

    def _extract_single_track_guesses(
        self, track: Track
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Prefer file-level metadata for single-file audiobook sets."""
        tags = track.existing_tags
        title_guess = tags.album or tags.title
        author_guess = tags.albumartist or tags.artist
        filename_title, filename_author, series_guess, volume_guess = (
            self._extract_name_guesses(track.src_path.stem)
        )

        return (
            title_guess or filename_title,
            author_guess or filename_author,
            series_guess,
            volume_guess,
        )

    def _extract_name_guesses(
        self, raw_name: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract metadata guesses from a folder or filename stem."""
        cleaned_name = self._clean_metadata_name(raw_name)

        series_match = re.search(
            r"(.+?)\s+(?:book|vol|volume)\s*(\d+)", cleaned_name, re.IGNORECASE
        )
        if series_match:
            series_name = series_match.group(1).strip()
            volume = series_match.group(2)
            return cleaned_name, None, series_name, volume

        author_title_match = re.search(r"^(.+?)\s*[-–—]\s*(.+)$", cleaned_name)
        if author_title_match:
            author_guess = author_title_match.group(1).strip()
            title_guess = author_title_match.group(2).strip()
            return title_guess, author_guess, None, None

        return cleaned_name or None, None, None, None

    def _clean_metadata_name(self, raw_name: str) -> str:
        """Remove common noise from folder names and filename stems."""
        cleaned_name = re.sub(r"\s*\[.*?\]\s*", "", raw_name)
        cleaned_name = re.sub(r"\s*\(.*?\)\s*", "", cleaned_name)
        return cleaned_name.strip()
