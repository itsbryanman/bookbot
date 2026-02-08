"""M4B conversion pipeline."""

import json
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles
import aiohttp

from ..config.manager import ConfigManager
from ..config.models import ConversionConfig
from ..core.models import AudiobookSet, ProviderIdentity
from .ffmpeg import FFmpegWrapper


class ConversionPlan:
    """Plan for converting audiobooks to M4B format."""

    def __init__(self, plan_id: str):
        self.plan_id = plan_id
        self.created_at = datetime.now()
        self.operations: list[ConversionOperation] = []

    def add_operation(self, operation: "ConversionOperation") -> None:
        """Add a conversion operation to the plan."""
        self.operations.append(operation)

    def to_dict(self) -> dict:
        """Convert plan to dictionary for serialization."""
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at.isoformat(),
            "operations": [op.to_dict() for op in self.operations],
        }


class ConversionOperation:
    """Single M4B conversion operation."""

    def __init__(
        self, audiobook_set: AudiobookSet, output_path: Path, config: ConversionConfig
    ):
        self.audiobook_set = audiobook_set
        self.output_path = output_path
        self.config = config
        self.chapters: list[dict] = []
        self.temp_files: list[Path] = []

    def to_dict(self) -> dict:
        """Convert operation to dictionary for serialization."""
        return {
            "source_path": str(self.audiobook_set.source_path),
            "output_path": str(self.output_path),
            "track_count": len(self.audiobook_set.tracks),
            "total_duration": self.audiobook_set.total_duration,
            "config": self.config.model_dump(),
        }


class ConversionPipeline:
    """Pipeline for converting audiobooks to M4B format."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self._ffmpeg: FFmpegWrapper | None = None

    def _get_ffmpeg(self) -> FFmpegWrapper:
        """Lazily instantiate and return the FFmpeg wrapper."""
        if self._ffmpeg is None:
            self._ffmpeg = FFmpegWrapper()
        return self._ffmpeg

    def create_conversion_plan(
        self, source_path: Path, config: ConversionConfig
    ) -> ConversionPlan:
        """Create a conversion plan for audiobooks in a directory."""
        plan = ConversionPlan(str(uuid.uuid4()))

        # Scan for audiobooks
        from ..core.discovery import AudioFileScanner

        scanner = AudioFileScanner()
        audiobook_sets = scanner.scan_directory(source_path)

        for audiobook_set in audiobook_sets:
            output_path = self._build_output_path(audiobook_set, config)

            operation = ConversionOperation(audiobook_set, output_path, config)
            plan.add_operation(operation)

        return plan

    async def execute_plan(self, plan: ConversionPlan) -> bool:
        """Execute a conversion plan."""
        success = True

        for operation in plan.operations:
            try:
                await self._execute_operation(operation)
            except Exception as e:
                print(f"Failed to convert {operation.audiobook_set.source_path}: {e}")
                success = False

        return success

    async def _execute_operation(self, operation: ConversionOperation) -> None:
        """Execute a single conversion operation."""
        audiobook_set = operation.audiobook_set
        output_path = operation.output_path
        config = operation.config

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Create temporary directory for processing
        temp_dir = output_path.parent / f".bookbot_temp_{uuid.uuid4().hex[:8]}"
        temp_dir.mkdir(exist_ok=True)

        try:
            # Step 1: Convert tracks to AAC if needed
            aac_files = []
            chapter_data = []
            current_time = 0.0

            for track in sorted(
                audiobook_set.tracks, key=lambda t: (t.disc, t.track_index)
            ):
                temp_file = (
                    temp_dir / f"track_{track.disc:02d}_{track.track_index:03d}.aac"
                )

                ffmpeg = self._get_ffmpeg()

                if (
                    ffmpeg.can_stream_copy(track.src_path)
                    and not config.normalize_audio
                ):
                    # Stream copy for AAC files
                    success = self._stream_copy(track.src_path, temp_file)
                else:
                    # Convert to AAC
                    success = ffmpeg.convert_to_aac(
                        track.src_path,
                        temp_file,
                        bitrate=config.bitrate if not config.use_vbr else None,
                        vbr_quality=config.vbr_quality if config.use_vbr else None,
                        normalize=config.normalize_audio,
                        target_lufs=config.target_lufs,
                    )

                if not success:
                    raise RuntimeError(f"Failed to convert {track.src_path}")

                aac_files.append(temp_file)
                operation.temp_files.append(temp_file)

                # Calculate chapter information
                if config.create_chapters:
                    duration = ffmpeg.get_duration(temp_file)

                    if (
                        config.chapter_naming == "from_tags"
                        and track.existing_tags.title
                    ):
                        chapter_title = track.existing_tags.title
                    elif config.chapter_naming == "track_number":
                        chapter_title = f"Track {track.track_index}"
                    else:  # auto
                        chapter_title = (
                            track.existing_tags.title
                            or f"Chapter {len(chapter_data) + 1}"
                        )

                    chapter_data.append(
                        {
                            "title": chapter_title,
                            "start": current_time,
                            "end": current_time + duration,
                        }
                    )

                    current_time += duration

            # Step 2: Concatenate files
            if len(aac_files) == 1:
                # Single file - just copy
                aac_files[0].rename(output_path)
            else:
                # Multiple files - concatenate
                success = self._get_ffmpeg().concatenate_files(
                    aac_files,
                    output_path,
                    chapters=chapter_data if config.create_chapters else None,
                )

                if not success:
                    raise RuntimeError("Failed to concatenate audio files")

            # Step 3: Add metadata
            if audiobook_set.chosen_identity:
                await self._apply_metadata(output_path, audiobook_set.chosen_identity)

            # Step 4: Embed cover art
            if config.write_cover_art and audiobook_set.chosen_identity:
                await self._embed_cover_art(output_path, audiobook_set.chosen_identity)

        finally:
            # Clean up temporary files
            for temp_file in operation.temp_files:
                if temp_file.exists():
                    temp_file.unlink()

            # Remove temporary directory
            if temp_dir.exists():
                temp_dir.rmdir()

    def _stream_copy(self, input_path: Path, output_path: Path) -> bool:
        """Stream copy a file without re-encoding."""
        try:
            import shutil

            shutil.copy2(input_path, output_path)
            return True
        except Exception:
            return False

    async def _apply_metadata(
        self, file_path: Path, identity: ProviderIdentity
    ) -> None:
        """Apply metadata tags to the M4B file."""
        metadata = {}

        if identity.title:
            metadata["title"] = identity.title
            metadata["album"] = identity.title

        if identity.authors:
            metadata["artist"] = ", ".join(identity.authors)
            metadata["albumartist"] = identity.authors[0]

        if identity.narrator:
            metadata["composer"] = identity.narrator

        if identity.year:
            metadata["date"] = str(identity.year)

        if identity.series_name:
            metadata["series"] = identity.series_name
            if identity.series_index:
                metadata["series_index"] = identity.series_index

        if identity.publisher:
            metadata["publisher"] = identity.publisher

        if identity.language:
            metadata["language"] = identity.language

        if identity.isbn_13 or identity.isbn_10:
            metadata["isbn"] = identity.isbn_13 or identity.isbn_10

        metadata["genre"] = "Audiobook"
        metadata["comment"] = "Converted by BookBot"

        # Apply metadata using FFmpeg
        self._get_ffmpeg().set_metadata(file_path, metadata)

    async def _embed_cover_art(
        self, file_path: Path, identity: ProviderIdentity
    ) -> None:
        """Download and embed cover art."""
        if not identity.cover_urls:
            return

        # Try to download the largest cover
        for cover_url in identity.cover_urls:
            try:
                cover_path = await self._download_cover(cover_url, file_path.parent)
                if cover_path:
                    success = self._get_ffmpeg().embed_cover_art(file_path, cover_path)
                    cover_path.unlink()  # Clean up downloaded cover
                    if success:
                        break
            except Exception:
                continue

    async def _download_cover(self, url: str, temp_dir: Path) -> Path | None:
        """Download cover art from URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    if response.status == 200:
                        # Determine file extension from content type
                        content_type = response.headers.get("content-type", "")
                        if "jpeg" in content_type or "jpg" in content_type:
                            ext = ".jpg"
                        elif "png" in content_type:
                            ext = ".png"
                        else:
                            ext = ".jpg"  # Default

                        cover_path = temp_dir / f"cover_{uuid.uuid4().hex[:8]}{ext}"

                        async with aiofiles.open(cover_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(1024):
                                await f.write(chunk)

                        return cover_path
        except Exception:
            pass

        return None

    def _build_output_path(
        self, audiobook_set: AudiobookSet, config: ConversionConfig
    ) -> Path:
        """Construct the output path for a converted audiobook."""
        if audiobook_set.chosen_identity:
            identity = audiobook_set.chosen_identity
            if identity.series_name and identity.series_index:
                filename = (
                    f"{identity.series_name} {identity.series_index} - {identity.title}"
                )
            else:
                filename = identity.title

            if identity.authors:
                filename = f"{identity.authors[0]} - {filename}"
        else:
            filename = audiobook_set.raw_title_guess or audiobook_set.source_path.name

        filename = self._sanitize_filename(filename)

        output_dir = config.output_directory or audiobook_set.source_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        return output_dir / f"{filename}.m4b"

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for filesystem compatibility."""
        # Remove or replace problematic characters
        forbidden_chars = '<>:"/\\|?*'
        for char in forbidden_chars:
            filename = filename.replace(char, "_")

        # Limit length
        if len(filename) > 200:
            filename = filename[:200]

        return filename.strip()

    async def convert_audiobook_set(
        self, audiobook_set: AudiobookSet, config: ConversionConfig
    ) -> bool:
        """Convert a single audiobook set to M4B format."""
        operation = ConversionOperation(
            audiobook_set, self._build_output_path(audiobook_set, config), config
        )
        await self._execute_operation(operation)
        return True

    def convert_directory(self, source_path: Path, config: ConversionConfig) -> bool:
        """Convert all audiobooks in a directory to M4B format."""
        plan = self.create_conversion_plan(source_path, config)

        # Save conversion plan
        log_dir = self.config_manager.get_log_dir()
        plan_file = log_dir / f"conversion_plan_{plan.plan_id}.json"

        try:
            with open(plan_file, "w") as f:
                json.dump(plan.to_dict(), f, indent=2)
        except Exception:
            pass

        # Execute plan
        import asyncio

        return asyncio.run(self.execute_plan(plan))
