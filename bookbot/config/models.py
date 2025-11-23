"""Configuration data models."""

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CasePolicy(str, Enum):
    """Case normalization policies."""

    TITLE_CASE = "title"
    LOWER_CASE = "lower"
    UPPER_CASE = "upper"
    AS_IS = "as_is"


class OverwritePolicy(str, Enum):
    """Tag overwrite policies."""

    OVERWRITE = "overwrite"
    FILL_MISSING = "fill_missing"
    PRESERVE = "preserve"


class NamingTemplate(BaseModel):
    """Filename template configuration."""

    name: str
    description: str
    folder_template: str = "{AuthorLastFirst}/{SeriesName}/{SeriesIndex} - {Title}"
    file_template: str = "{DiscPad}{TrackPad} - {Title}"

    # Template variables documentation
    available_tokens: list[str] = Field(
        default_factory=lambda: [
            "{Author}",
            "{AuthorLastFirst}",
            "{Title}",
            "{ShortTitle}",
            "{SeriesName}",
            "{SeriesIndex}",
            "{Year}",
            "{Narrator}",
            "{DiscPad}",
            "{TrackPad}",
            "{Language}",
            "{ISBN}",
        ]
    )


class TaggingConfig(BaseModel):
    """Audio tagging configuration."""

    enabled: bool = True
    overwrite_policy: OverwritePolicy = OverwritePolicy.FILL_MISSING
    write_cover_art: bool = True
    max_cover_size: int = 1200  # pixels
    preserve_existing_tags: bool = True

    # Standard tags to write
    write_album: bool = True
    write_albumartist: bool = True
    write_artist: bool = True
    write_title: bool = True
    write_track: bool = True
    write_disc: bool = True
    write_date: bool = True
    write_genre: bool = True
    write_language: bool = True
    write_series: bool = True
    write_identifiers: bool = True

    @field_validator("overwrite_policy", mode="before")
    def validate_overwrite_policy(cls, v: str | OverwritePolicy) -> OverwritePolicy:
        if isinstance(v, str):
            return OverwritePolicy(v)
        return v


class ConversionConfig(BaseModel):
    """M4B conversion configuration."""

    enabled: bool = False
    output_directory: Path | None = None
    bitrate: str = "128k"  # AAC bitrate
    use_vbr: bool = True
    vbr_quality: int = 5  # aacvbr quality (1-6)
    normalize_audio: bool = False
    target_lufs: float = -16.0  # EBU R128 target
    create_chapters: bool = True
    chapter_naming: str = "auto"  # "auto", "from_tags", "track_number"
    temp_directory: Path | None = None
    write_cover_art: bool = True  # Embed cover art in M4B files

    def validate_paths(cls, v: str | Path | None) -> Path | None:
        return Path(v) if isinstance(v, str) else v


class GoogleBooksConfig(BaseModel):
    """Google Books API configuration."""

    enabled: bool = False
    api_key: str | None = None


class LibriVoxConfig(BaseModel):
    """LibriVox configuration."""

    enabled: bool = True


class AudibleConfig(BaseModel):
    """Audible configuration."""

    enabled: bool = True
    marketplace: str = "US"  # US, UK, CA, AU, FR, DE, IT, ES, JP, IN


class ProviderConfig(BaseModel):
    """Metadata provider configuration."""

    priority_order: list[str] = Field(
        default_factory=lambda: ["openlibrary", "googlebooks", "librivox", "audible"]
    )
    cache_enabled: bool = True
    cache_size_mb: int = 100
    rate_limit_delay: float = 0.1
    request_timeout: int = 30
    language_preference: str = "en"

    # Provider-specific configurations
    google_books: GoogleBooksConfig = Field(default_factory=GoogleBooksConfig)
    librivox: LibriVoxConfig = Field(default_factory=LibriVoxConfig)
    audible: AudibleConfig = Field(default_factory=AudibleConfig)


class Config(BaseModel):
    """Main configuration model."""

    # File operations
    safe_mode: bool = True  # Rename only, no tagging
    dry_run_default: bool = True
    zero_padding_width: int = 0  # 0 = auto-detect
    case_policy: CasePolicy = CasePolicy.TITLE_CASE
    unicode_normalization: bool = True
    max_path_length: int = 255

    # Naming templates
    active_template: str = "default"
    templates: dict[str, NamingTemplate] = Field(default_factory=dict)

    # Tagging
    tagging: TaggingConfig = Field(default_factory=TaggingConfig)

    # Conversion
    conversion: ConversionConfig = Field(default_factory=ConversionConfig)

    # Providers
    providers: ProviderConfig = Field(default_factory=ProviderConfig)

    # Cache and logging
    cache_directory: Path = Path.home() / ".cache" / "bookbot"
    log_directory: Path = Path.home() / ".local" / "share" / "bookbot" / "logs"
    transaction_history_days: int = 30

    # Performance
    max_concurrent_operations: int = 5
    scan_timeout: int = 300  # seconds

    @field_validator("cache_directory", "log_directory", mode="before")
    def validate_paths(cls, v: str | Path | None) -> Path | None:
        return Path(v) if isinstance(v, str) else v

    @field_validator("case_policy", mode="before")
    def validate_case_policy(cls, v: str | CasePolicy) -> CasePolicy:
        if isinstance(v, str):
            return CasePolicy(v)
        return v

    def model_post_init(self, __context: Any) -> None:
        """Initialize default templates if none exist."""
        if not self.templates:
            self.templates = self._get_default_templates()

    def _get_default_templates(self) -> dict[str, NamingTemplate]:
        """Get default naming templates."""
        return {
            "default": NamingTemplate(
                name="Default",
                description="Standard audiobook naming",
                folder_template="{AuthorLastFirst}/{Title} ({Year})",
                file_template="{DiscPad}{TrackPad} - {Title}",
            ),
            "plex": NamingTemplate(
                name="Plex Media Server",
                description="Plex-friendly naming convention",
                folder_template=(
                    "{AuthorLastFirst}/{SeriesName}/{SeriesIndex} - {Title}"
                ),
                file_template="{DiscPad}{TrackPad} - {Title}",
            ),
            "audible": NamingTemplate(
                name="Audible Style",
                description="Audible-like naming with narrator",
                folder_template="{AuthorLastFirst}/{Title} ({Narrator})",
                file_template="Chapter {TrackPad} - {Title}",
            ),
            "series": NamingTemplate(
                name="Series Focused",
                description="Organize by series first",
                folder_template=(
                    "{SeriesName}/{SeriesIndex} - {Title} - {AuthorLastFirst}"
                ),
                file_template="{DiscPad}{TrackPad} - {Title}",
            ),
        }


class Profile(BaseModel):
    """A complete configuration profile."""

    name: str
    description: str
    config: Config

    # Profile metadata
    created_at: str | None = None
    last_used: str | None = None
    use_count: int = 0
