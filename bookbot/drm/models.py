"""DRM-related data models."""

from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

from pydantic import BaseModel


class DRMType(str, Enum):
    """Types of DRM protection detected."""
    AUDIBLE_AAX = "audible_aax"
    AUDIBLE_AAXC = "audible_aaxc"
    FAIRPLAY = "fairplay"
    WMDRM = "wmdrm"
    UNKNOWN = "unknown"
    NONE = "none"


class DRMInfo(BaseModel):
    """Information about DRM protection on a file."""
    drm_type: DRMType
    file_path: Path
    is_protected: bool
    metadata: Dict[str, Any] = {}
    activation_bytes: Optional[str] = None
    checksum: Optional[str] = None

    class Config:
        """Pydantic configuration."""
        arbitrary_types_allowed = True


class RemovalResult(BaseModel):
    """Result of DRM removal operation."""
    success: bool
    original_file: Path
    output_file: Optional[Path] = None
    drm_info: DRMInfo
    error_message: Optional[str] = None
    method_used: Optional[str] = None

    class Config:
        """Pydantic configuration."""
        arbitrary_types_allowed = True