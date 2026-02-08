"""Local metadata provider using existing sidecar files."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..core.models import AudiobookSet, MatchCandidate, ProviderIdentity
from .base import MetadataProvider

logger = get_logger("local_metadata_provider")


class LocalMetadataProvider(MetadataProvider):
    """Metadata provider that reads local sidecar metadata files."""

    # Specific filenames we check first, in order of preference
    CANDIDATE_FILENAMES = (
        "metadata.json",
        "book.json",
        "info.json",
        "audiobook.json",
        "metadata.nfo",
        "book.nfo",
        "info.nfo",
    )

    # Glob patterns considered if preferred filenames are not found
    CANDIDATE_PATTERNS = ("*.nfo", "*.json", "*.info")

    def __init__(self) -> None:
        super().__init__(name="Local Metadata")

    async def find_matches(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        """Return a single high-confidence match sourced from local metadata files."""
        metadata = await asyncio.to_thread(
            self._load_metadata_for_folder, audiobook_set.source_path
        )

        if not metadata:
            logger.debug(
                "No local metadata files found",
                source_path=str(audiobook_set.source_path),
            )
            return []

        identity = self._build_identity(metadata, audiobook_set)
        confidence, reasons = self._score_from_metadata(metadata, audiobook_set)

        candidate = MatchCandidate(
            identity=identity,
            confidence=confidence,
            confidence_level=self._get_confidence_level(confidence),
            match_reasons=reasons,
        )

        return [candidate]

    async def search(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        series: str | None = None,
        isbn: str | None = None,
        year: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[ProviderIdentity]:  # pragma: no cover - not used for local provider
        """Local search is not supported because we operate on folders directly."""
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        """Fetching by external id is not supported for local metadata."""
        return None

    def calculate_match_score(
        self, audiobook_set: AudiobookSet, identity: ProviderIdentity
    ) -> float:  # pragma: no cover - unused in local workflow
        return 1.0

    # Internal helpers -----------------------------------------------------

    def _load_metadata_for_folder(self, folder: Path) -> dict[str, Any] | None:
        """Search for and parse known metadata sidecar files within a folder."""
        if not folder.exists() or not folder.is_dir():
            return None

        # Check preferred filenames first
        for name in self.CANDIDATE_FILENAMES:
            path = folder / name
            if path.exists() and path.is_file():
                metadata = self._parse_metadata_file(path)
                if metadata:
                    metadata["_source_file"] = path.name
                    return metadata

        # Fall back to general patterns
        for pattern in self.CANDIDATE_PATTERNS:
            for path in folder.glob(pattern):
                if not path.is_file():
                    continue
                metadata = self._parse_metadata_file(path)
                if metadata:
                    metadata["_source_file"] = path.name
                    return metadata

        return None

    def _parse_metadata_file(self, path: Path) -> dict[str, Any] | None:
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                with path.open(encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            elif suffix in {".nfo", ".info"}:
                return self._parse_nfo(path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to parse metadata file", path=str(path), exc=str(exc)
            )
            return None

        return None

    def _parse_nfo(self, path: Path) -> dict[str, Any] | None:
        """Parse a simple key:value based NFO or INFO file."""
        metadata: dict[str, Any] = {}
        key_map = {
            "title": "title",
            "book": "title",
            "name": "title",
            "author": "author",
            "authors": "authors",
            "writer": "author",
            "series": "series",
            "series name": "series",
            "series index": "series_index",
            "volume": "series_index",
            "narrator": "narrator",
            "reader": "narrator",
            "year": "year",
            "published": "year",
            "language": "language",
            "isbn": "isbn",
            "asin": "asin",
            "description": "description",
            "summary": "description",
        }

        try:
            with path.open(encoding="utf-8", errors="ignore") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or ":" not in line:
                        continue
                    key, value = [part.strip() for part in line.split(":", 1)]
                    if not key:
                        continue
                    lowered = key.lower()
                    target_key = key_map.get(lowered)
                    if not target_key:
                        continue
                    if target_key in {"author", "authors"}:
                        metadata.setdefault("authors", [])
                        metadata["authors"].extend(self._split_authors(value))
                    elif target_key == "series_index":
                        metadata[target_key] = value.strip()
                    else:
                        metadata[target_key] = value.strip()

            return metadata or None
        except OSError as exc:
            logger.warning("Unable to read NFO metadata", path=str(path), exc=str(exc))
            return None

    def _build_identity(
        self, metadata: dict[str, Any], audiobook_set: AudiobookSet
    ) -> ProviderIdentity:
        authors = self._coerce_authors(metadata, audiobook_set)
        series_index = metadata.get("series_index")
        year_raw = metadata.get("year")
        try:
            year = int(year_raw) if year_raw is not None else None
        except (TypeError, ValueError):
            year = None

        identity = ProviderIdentity(
            provider="local",
            external_id=metadata.get("id")
            or metadata.get("asin")
            or metadata.get("isbn")
            or audiobook_set.source_path.name,
            title=(
                metadata.get("title")
                or audiobook_set.raw_title_guess
                or audiobook_set.source_path.name
            ),
            authors=authors,
            series_name=metadata.get("series") or audiobook_set.series_guess,
            series_index=str(series_index) if series_index is not None else None,
            year=year,
            language=(metadata.get("language") or audiobook_set.language_guess or None),
            narrator=metadata.get("narrator") or audiobook_set.narrator_guess,
            publisher=metadata.get("publisher"),
            isbn_10=metadata.get("isbn_10") or metadata.get("isbn"),
            isbn_13=metadata.get("isbn_13"),
            asin=metadata.get("asin"),
            description=metadata.get("description"),
            cover_urls=self._coerce_cover_urls(metadata),
            raw_data=metadata,
        )
        return identity

    def _coerce_authors(
        self, metadata: dict[str, Any], audiobook_set: AudiobookSet
    ) -> list[str]:
        authors: list[str] = []

        if "authors" in metadata and isinstance(metadata["authors"], list):
            authors.extend(
                [str(author).strip() for author in metadata["authors"] if author]
            )
        elif "authors" in metadata and isinstance(metadata["authors"], str):
            authors.extend(self._split_authors(metadata["authors"]))
        elif "author" in metadata and metadata["author"]:
            authors.extend(self._split_authors(str(metadata["author"])))

        if not authors and audiobook_set.author_guess:
            authors.append(audiobook_set.author_guess)

        return [author for author in authors if author]

    def _split_authors(self, value: str) -> list[str]:
        if not value:
            return []
        # Split on common separators, respecting potential "Last, First" names
        parts = re.split(r"\s*(?:,|/|&|and)\s*", value)
        return [part.strip() for part in parts if part.strip()]

    def _coerce_cover_urls(self, metadata: dict[str, Any]) -> list[str]:
        cover = metadata.get("cover") or metadata.get("cover_url")
        if not cover:
            return []
        if isinstance(cover, str):
            return [cover]
        if isinstance(cover, list):
            return [str(item) for item in cover if item]
        return []

    def _score_from_metadata(
        self, metadata: dict[str, Any], audiobook_set: AudiobookSet
    ) -> tuple[float, list[str]]:
        """Derive a confidence score and associated reasons."""
        score = 0.2  # baseline for having any metadata
        reasons = []

        if metadata.get("title"):
            score += 0.3
            reasons.append("Title from local metadata")
        if metadata.get("author") or metadata.get("authors"):
            score += 0.3
            reasons.append("Author from local metadata")
        if metadata.get("series"):
            score += 0.1
            reasons.append("Series info found")
        if metadata.get("year"):
            score += 0.05
            reasons.append("Publication year found")
        if metadata.get("narrator"):
            score += 0.05
            reasons.append("Narrator found")

        source_file = metadata.get("_source_file")
        if source_file:
            reasons.append(f"Loaded from {source_file}")

        # Clamp score into [0,1]
        score = max(0.0, min(score, 1.0))

        # Ensure high confidence when the essentials are present
        if metadata.get("title") and (
            metadata.get("authors") or metadata.get("author")
        ):
            score = max(score, 0.9)

        return score, reasons
