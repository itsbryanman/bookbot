"""Exception hierarchy with structured error details."""

from typing import Any


class BookBotError(Exception):
    """Base exception with structured details."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        recoverable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable


class MetadataError(BookBotError):
    """Provider/metadata failures."""

    pass


class AudioProcessingError(BookBotError):
    """Audio analysis/conversion failures."""

    pass


class ConversionError(AudioProcessingError):
    """Format conversion specific."""

    pass


class OrganizationError(BookBotError):
    """File organization failures."""

    pass


class ConfigurationError(BookBotError):
    """Invalid configuration."""

    pass
