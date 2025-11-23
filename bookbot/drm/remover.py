"""DRM removal functionality."""

import subprocess
from pathlib import Path

from .detector import DRMDetector
from .models import DRMType, RemovalResult


class DRMRemover:
    """Removes DRM protection from audio files."""

    def __init__(
        self, ffmpeg_path: str | None = None, activation_bytes: str | None = None
    ) -> None:
        """
        Initialize the DRM remover.

        Args:
            ffmpeg_path: Path to ffmpeg binary
            activation_bytes: Audible activation bytes for AAX files
        """
        self.ffmpeg_path = ffmpeg_path or "ffmpeg"
        self.activation_bytes = activation_bytes
        self.detector = DRMDetector()

    def remove_drm(
        self,
        file_path: Path,
        output_path: Path | None = None,
        activation_bytes: str | None = None,
    ) -> RemovalResult:
        """
        Remove DRM protection from an audio file.

        Args:
            file_path: Input file with DRM
            output_path: Output file path (optional)
            activation_bytes: Audible activation bytes (optional)

        Returns:
            RemovalResult with operation details
        """
        # First detect DRM type
        drm_info = self.detector.detect_drm(file_path)

        if not drm_info.is_protected:
            return RemovalResult(
                success=True,
                original_file=file_path,
                output_file=file_path,  # No conversion needed
                drm_info=drm_info,
                method_used="no_drm",
            )

        # Generate output path if not provided
        if output_path is None:
            output_path = self._generate_output_path(file_path, drm_info.drm_type)

        # Use provided activation bytes or instance default or stored bytes
        activation_bytes = activation_bytes or self.activation_bytes
        if not activation_bytes:
            try:
                from .secure_storage import load_activation_bytes

                activation_bytes = load_activation_bytes()
            except ImportError:
                pass

        try:
            if drm_info.drm_type == DRMType.AUDIBLE_AAX:
                return self._remove_aax_drm(file_path, output_path, activation_bytes)
            elif drm_info.drm_type == DRMType.AUDIBLE_AAXC:
                return self._remove_aaxc_drm(file_path, output_path)
            elif drm_info.drm_type == DRMType.FAIRPLAY:
                return self._remove_fairplay_drm(file_path, output_path)
            else:
                return RemovalResult(
                    success=False,
                    original_file=file_path,
                    drm_info=drm_info,
                    error_message=f"Unsupported DRM type: {drm_info.drm_type}",
                )

        except Exception as e:
            return RemovalResult(
                success=False,
                original_file=file_path,
                drm_info=drm_info,
                error_message=str(e),
            )

    def _remove_aax_drm(
        self, input_path: Path, output_path: Path, activation_bytes: str | None
    ) -> RemovalResult:
        """Remove DRM from Audible AAX files using ffmpeg."""
        if not activation_bytes:
            return RemovalResult(
                success=False,
                original_file=input_path,
                drm_info=self.detector.detect_drm(input_path),
                error_message="Activation bytes required for AAX DRM removal",
            )

        try:
            # Use ffmpeg to convert AAX to M4A/MP3
            cmd = [
                self.ffmpeg_path,
                "-activation_bytes",
                activation_bytes,
                "-i",
                str(input_path),
                "-vn",  # No video
                "-c:a",
                "copy",  # Copy audio codec if possible
                "-y",  # Overwrite output
                str(output_path),
            ]

            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)

            return RemovalResult(
                success=True,
                original_file=input_path,
                output_file=output_path,
                drm_info=self.detector.detect_drm(input_path),
                method_used="ffmpeg_activation_bytes",
            )

        except subprocess.TimeoutExpired:
            error_msg = "FFmpeg timed out during AAX DRM removal"
        except subprocess.CalledProcessError as e:
            error_msg = f"FFmpeg failed: {e.stderr}" if e.stderr else str(e)

        if output_path.exists():
            output_path.unlink(missing_ok=True)

        return RemovalResult(
            success=False,
            original_file=input_path,
            drm_info=self.detector.detect_drm(input_path),
            error_message=error_msg,
        )

    def _remove_aaxc_drm(self, input_path: Path, output_path: Path) -> RemovalResult:
        """Remove DRM from Audible AAXC files."""
        try:
            cmd = [
                self.ffmpeg_path,
                "-i",
                str(input_path),
                "-vn",  # No video
                "-c:a",
                "copy",  # Copy audio codec
                "-y",  # Overwrite output
                str(output_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode == 0:
                return RemovalResult(
                    success=True,
                    original_file=input_path,
                    output_file=output_path,
                    drm_info=self.detector.detect_drm(input_path),
                    method_used="ffmpeg_copy",
                )

            # If copy fails, try re-encoding
            cmd = [
                self.ffmpeg_path,
                "-i",
                str(input_path),
                "-vn",
                "-c:a",
                "aac",  # Re-encode to AAC
                "-b:a",
                "128k",
                "-y",
                str(output_path),
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=600
            )

            return RemovalResult(
                success=True,
                original_file=input_path,
                output_file=output_path,
                drm_info=self.detector.detect_drm(input_path),
                method_used="ffmpeg_reencode",
            )

        except subprocess.TimeoutExpired:
            error_msg = "FFmpeg timed out during AAXC DRM removal"
        except subprocess.CalledProcessError as e:
            error_msg = f"FFmpeg failed: {e.stderr}" if e.stderr else str(e)

        if output_path.exists():
            output_path.unlink(missing_ok=True)

        return RemovalResult(
            success=False,
            original_file=input_path,
            drm_info=self.detector.detect_drm(input_path),
            error_message=error_msg,
        )

    def _remove_fairplay_drm(
        self, input_path: Path, output_path: Path
    ) -> RemovalResult:
        """Remove FairPlay DRM from iTunes files."""
        return RemovalResult(
            success=False,
            original_file=input_path,
            drm_info=self.detector.detect_drm(input_path),
            error_message="FairPlay DRM removal requires iTunes authorization",
        )

    def _generate_output_path(self, input_path: Path, drm_type: DRMType) -> Path:
        """Generate appropriate output path based on DRM type."""
        parent = input_path.parent
        stem = input_path.stem

        # Choose appropriate extension
        if drm_type == DRMType.AUDIBLE_AAX:
            extension = ".m4a"  # AAX typically converts to M4A
        elif drm_type == DRMType.AUDIBLE_AAXC:
            extension = ".m4a"
        elif drm_type == DRMType.FAIRPLAY:
            extension = ".m4a"
        else:
            extension = ".m4a"

        return parent / f"{stem}_no_drm{extension}"

    def batch_remove_drm(
        self,
        file_paths: list[Path],
        output_dir: Path | None = None,
        activation_bytes: str | None = None,
    ) -> list[RemovalResult]:
        """
        Remove DRM from multiple files.

        Args:
            file_paths: List of input files
            output_dir: Output directory (optional)
            activation_bytes: Audible activation bytes (optional)

        Returns:
            List of RemovalResult objects
        """
        results = []

        for file_path in file_paths:
            if output_dir:
                output_path = (
                    output_dir
                    / self._generate_output_path(
                        file_path, self.detector.detect_drm(file_path).drm_type
                    ).name
                )
            else:
                output_path = None

            result = self.remove_drm(file_path, output_path, activation_bytes)
            results.append(result)

        return results

    def check_ffmpeg_availability(self) -> bool:
        """Check if ffmpeg is available and supports activation_bytes."""
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-h", "full"],
                capture_output=True,
                text=True,
                check=True,
            )
            return "activation_bytes" in result.stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def get_supported_formats(self) -> list[DRMType]:
        """Get list of DRM formats this remover can handle."""
        supported = [DRMType.NONE]  # Always support non-DRM files

        if self.check_ffmpeg_availability():
            supported.append(DRMType.AUDIBLE_AAX)

        return supported
