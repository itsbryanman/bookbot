"""Chapter data models."""

from pydantic import BaseModel


class Chapter(BaseModel):
    """Represents a single chapter marker."""

    title: str
    start_ms: int
    end_ms: int | None = None
    source: str = "unknown"  # detection method: audnexus, embedded, cue, tracks, silence
