"""DRM-related data models."""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Token(BaseModel):
    """OAuth token for Audible authentication."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int | None = None
    expires_at: datetime | None = None
    scope: str | None = None


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
    metadata: dict[str, Any] = Field(default_factory=dict)
    activation_bytes: str | None = None
    checksum: str | None = None

    class Config:
        """Pydantic configuration."""

        arbitrary_types_allowed = True


class RemovalResult(BaseModel):
    """Result of DRM removal operation."""

    success: bool
    original_file: Path
    output_file: Path | None = None
    drm_info: DRMInfo
    error_message: str | None = None
    method_used: str | None = None

    class Config:
        """Pydantic configuration."""

        arbitrary_types_allowed = True
