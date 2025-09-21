"""DRM detection functionality."""

import hashlib
import struct
from pathlib import Path
from typing import Optional, Dict, Any

from ..core.models import AudioFile
from .models import DRMInfo, DRMType


class DRMDetector:
    """Detects DRM protection on audio files."""

    def __init__(self) -> None:
        """Initialize the DRM detector."""
        self._signature_map = {
            b'\x57\x90\x75\x36': DRMType.AUDIBLE_AAX,  # Audible AAX signature
            b'AAXC': DRMType.AUDIBLE_AAXC,  # Audible AAXC signature
        }

    def detect_drm(self, file_path: Path) -> DRMInfo:
        """
        Detect DRM protection on an audio file.

        Args:
            file_path: Path to the audio file

        Returns:
            DRMInfo object with detection results
        """
        try:
            drm_type = self._detect_drm_type(file_path)
            metadata = self._extract_drm_metadata(file_path, drm_type)

            return DRMInfo(
                drm_type=drm_type,
                file_path=file_path,
                is_protected=drm_type != DRMType.NONE,
                metadata=metadata,
                checksum=self._calculate_checksum(file_path)
            )
        except Exception as e:
            return DRMInfo(
                drm_type=DRMType.UNKNOWN,
                file_path=file_path,
                is_protected=True,
                metadata={"error": str(e)}
            )

    def detect_from_audiofile(self, audio_file: AudioFile) -> DRMInfo:
        """
        Detect DRM from an AudioFile instance.

        Args:
            audio_file: AudioFile instance

        Returns:
            DRMInfo object with detection results
        """
        return self.detect_drm(audio_file.path)

    def _detect_drm_type(self, file_path: Path) -> DRMType:
        """Detect the type of DRM protection."""
        if not file_path.exists():
            return DRMType.UNKNOWN

        # Check file extension first
        suffix = file_path.suffix.lower()
        if suffix == '.aax':
            return DRMType.AUDIBLE_AAX
        elif suffix == '.aaxc':
            return DRMType.AUDIBLE_AAXC

        # Check file signatures
        try:
            with open(file_path, 'rb') as f:
                header = f.read(64)

                for signature, drm_type in self._signature_map.items():
                    if signature in header:
                        return drm_type

                # Check for MP4-based DRM (iTunes/FairPlay)
                if b'ftyp' in header and b'M4P' in header:
                    return DRMType.FAIRPLAY

                # Check for Windows Media DRM
                if b'ASF' in header[:16] and b'DRM' in header:
                    return DRMType.WMDRM

        except (IOError, PermissionError):
            return DRMType.UNKNOWN

        return DRMType.NONE

    def _extract_drm_metadata(self, file_path: Path, drm_type: DRMType) -> Dict[str, Any]:
        """Extract DRM-specific metadata."""
        metadata: Dict[str, Any] = {}

        if drm_type == DRMType.AUDIBLE_AAX:
            metadata.update(self._extract_aax_metadata(file_path))
        elif drm_type == DRMType.AUDIBLE_AAXC:
            metadata.update(self._extract_aaxc_metadata(file_path))

        return metadata

    def _extract_aax_metadata(self, file_path: Path) -> Dict[str, Any]:
        """Extract AAX-specific metadata."""
        metadata = {}

        try:
            with open(file_path, 'rb') as f:
                # Skip to where Audible metadata typically starts
                f.seek(32)
                chunk = f.read(1024)

                # Look for Audible activation bytes hint
                if b'activation_bytes' in chunk:
                    # This would need proper AAX parsing
                    metadata['has_activation_bytes_hint'] = True

                # Extract file size for activation bytes calculation
                metadata['file_size'] = file_path.stat().st_size

        except (IOError, PermissionError):
            pass

        return metadata

    def _extract_aaxc_metadata(self, file_path: Path) -> Dict[str, Any]:
        """Extract AAXC-specific metadata."""
        metadata = {}

        try:
            with open(file_path, 'rb') as f:
                header = f.read(256)

                # AAXC files have different structure than AAX
                if b'voucher' in header.lower():
                    metadata['has_voucher'] = True

                metadata['file_size'] = file_path.stat().st_size

        except (IOError, PermissionError):
            pass

        return metadata

    def _calculate_checksum(self, file_path: Path) -> Optional[str]:
        """Calculate SHA-256 checksum of first 1MB of file."""
        try:
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                # Only checksum first 1MB for performance
                chunk = f.read(1024 * 1024)
                sha256.update(chunk)
            return sha256.hexdigest()
        except (IOError, PermissionError):
            return None

    def is_protected(self, file_path: Path) -> bool:
        """Quick check if file is DRM protected."""
        drm_info = self.detect_drm(file_path)
        return drm_info.is_protected