"""Audio file discovery and metadata extraction."""

import re
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from .models import AudiobookSet, AudioFormat, AudioTags, Track, TrackStatus


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
            audiobook_set = self._create_audiobook_set(group_path, files)
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
            # Use the immediate parent directory as the grouping key
            # This handles most common audiobook organization patterns
            parent = file_path.parent

            if parent not in groups:
                groups[parent] = []
            groups[parent].append(file_path)

        # TODO: Implement duration-based splitting for mixed books in one folder
        # This would use k-means clustering on track durations to detect
        # multiple books in a single directory

        return groups

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

        audiobook_set = AudiobookSet(
            source_path=source_path,
            raw_title_guess=title_guess,
            author_guess=author_guess,
            series_guess=series_guess,
            volume_guess=volume_guess,
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
            }

            for field, tag_keys in tag_mapping.items():
                for key in tag_keys:
                    if key in audio_file:
                        value = (
                            audio_file[key][0]
                            if isinstance(audio_file[key], list)
                            else audio_file[key]
                        )

                        # Handle track/disc numbers that might be "1/10" format
                        if field in ["track", "disc"] and isinstance(value, str):
                            try:
                                value = int(value.split("/")[0])
                            except (ValueError, IndexError):
                                continue
                        elif field in ["track", "disc"] and hasattr(
                            value, "__getitem__"
                        ):
                            # Handle tuple format like (1, 10)
                            value = value[0]

                        setattr(tags, field, value)
                        break

            # Store raw tags for preservation
            tags.raw_tags = dict(audio_file)

        except (ID3NoHeaderError, Exception):
            # File has no tags or error reading them
            pass

        return tags

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
        if value is None:
            return None

        if isinstance(value, int):
            return value

        # Mutagen ID3 frames expose a ``text`` attribute with the raw values
        if hasattr(value, "text"):
            # Accessing .text may return a list of strings
            normalized = self._normalize_numeric_tag(value.text)
            if normalized is not None:
                return normalized

        # Handle containers like lists/tuples provided by different tag formats
        if isinstance(value, (list, tuple)):
            for item in value:
                normalized = self._normalize_numeric_tag(item)
                if normalized is not None:
                    return normalized
            return None

        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="ignore")
            except Exception:
                return None

        if isinstance(value, str):
            # Accept common patterns such as ``1/10`` or ``01``
            match = re.search(r"\d+", value)
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

        # Check filename and a few parent directory levels for disc hints
        search_targets: list[str] = [file_path.stem.lower()]

        parent = file_path.parent
        depth = 0
        while parent != parent.parent and depth < 3:
            search_targets.append(parent.name.lower())
            parent = parent.parent
            depth += 1

        for target in search_targets:
            for pattern in self.DISC_PATTERNS:
                match = pattern.match(target)
                if match:
                    try:
                        return int(match.group(1))
                    except ValueError:
                        continue

        # Default to disc 1
        return 1

    def _extract_metadata_guesses(
        self, source_path: Path, tracks: list[Track]
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract title, author, series, and volume guesses from path and tracks."""
        # Use folder name as primary source
        folder_name = source_path.name

        # Clean up common patterns
        folder_name = re.sub(r"\s*\[.*?\]\s*", "", folder_name)  # Remove [brackets]
        folder_name = re.sub(r"\s*\(.*?\)\s*", "", folder_name)  # Remove (parentheses)

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
            # Keep full folder name as the initial title guess while extracting the
            # author name.
            title_guess = folder_name.strip()
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

        title_guess = albums.pop() if len(albums) == 1 else folder_name
        author_guess = (
            albumartists.pop()
            if len(albumartists) == 1
            else artists.pop() if len(artists) == 1 else None
        )

        return title_guess, author_guess, None, None
