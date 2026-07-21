"""Tests for Phase 2: identifier extraction and deterministic lookup."""

import asyncio
import re
from pathlib import Path

import pytest

from bookbot.core.discovery import _ASIN_RE, _ISBN_RE, AudioFileScanner
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    AudioTags,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    Track,
)
from bookbot.providers.base import MetadataProvider

# ── ISBN / ASIN validation regex ──


class TestISBNValidation:
    def test_valid_isbn_10(self) -> None:
        assert _ISBN_RE.match("0451524934")
        assert _ISBN_RE.match("080442957X")  # trailing X

    def test_valid_isbn_13(self) -> None:
        assert _ISBN_RE.match("9780451524935")

    def test_invalid_isbn(self) -> None:
        assert not _ISBN_RE.match("12345")
        assert not _ISBN_RE.match("abcdefghij")
        assert not _ISBN_RE.match("123456789012")  # 12 digits
        assert not _ISBN_RE.match("")

    def test_isbn_stripping(self) -> None:
        raw = "978-0-451-52493-5"
        cleaned = re.sub(r"[-\s]", "", raw)
        assert _ISBN_RE.match(cleaned)


class TestASINValidation:
    def test_valid_asin(self) -> None:
        assert _ASIN_RE.match("B0ABCDEFGH")
        assert _ASIN_RE.match("B012345678")

    def test_valid_asin_case_insensitive(self) -> None:
        assert _ASIN_RE.match("b0abcdefgh")

    def test_invalid_asin(self) -> None:
        assert not _ASIN_RE.match("1234567890")
        assert not _ASIN_RE.match("B1ABCDEFGH")  # B1 not B0
        assert not _ASIN_RE.match("B0ABCDEF")  # too short
        assert not _ASIN_RE.match("")


# ── Majority-vote identifier extraction ──


class TestMajorityIdentifier:
    def _make_tracks(
        self,
        isbn_values: list[str | None],
    ) -> list[Track]:
        tracks = []
        for i, isbn in enumerate(isbn_values):
            tracks.append(
                Track(
                    src_path=Path(f"/tmp/track{i}.mp3"),
                    track_index=i + 1,
                    audio_format=AudioFormat.MP3,
                    existing_tags=AudioTags(isbn=isbn),
                )
            )
        return tracks

    def test_majority_agrees(self) -> None:
        tracks = self._make_tracks(
            ["9780451524935", "9780451524935", "9780451524935"]
        )
        result = AudioFileScanner._majority_identifier(tracks, "isbn")
        assert result == "9780451524935"

    def test_majority_disagrees(self) -> None:
        tracks = self._make_tracks(
            ["9780451524935", "9780000000000", None]
        )
        # 2 tracks carry the tag, but they disagree (1 vs 1)
        result = AudioFileScanner._majority_identifier(tracks, "isbn")
        assert result is None

    def test_majority_with_nones(self) -> None:
        tracks = self._make_tracks(
            ["9780451524935", None, None, "9780451524935"]
        )
        # 2 out of 2 agree
        result = AudioFileScanner._majority_identifier(tracks, "isbn")
        assert result == "9780451524935"

    def test_no_values(self) -> None:
        tracks = self._make_tracks([None, None])
        result = AudioFileScanner._majority_identifier(tracks, "isbn")
        assert result is None


# ── Tag extraction (ID3 TXXX, MP4 freeform) ──


class TestTagExtraction:
    def test_id3_isbn_extraction(self, tmp_path: Path) -> None:
        """Build a minimal MP3 with TXXX:ISBN and extract it."""
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3

        mp3_path = tmp_path / "test.mp3"
        # Create minimal valid MP3 (MPEG frame header + silence)
        mp3_path.write_bytes(
            b"\xff\xfb\x90\x00" + b"\x00" * 417  # MPEG1 Layer3 128kbps
        )

        try:
            audio = MP3(mp3_path)
        except Exception:
            # If mutagen can't parse the minimal frame, skip
            pytest.skip("Cannot create minimal MP3 for testing")

        audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="ISBN", text=["9780451524935"]))
        audio.save()

        scanner = AudioFileScanner()
        tags = scanner._extract_audio_tags(mp3_path)
        assert tags.isbn == "9780451524935"

    def test_id3_asin_extraction(self, tmp_path: Path) -> None:
        """Build a minimal MP3 with TXXX:ASIN and extract it."""
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3

        mp3_path = tmp_path / "test.mp3"
        mp3_path.write_bytes(
            b"\xff\xfb\x90\x00" + b"\x00" * 417
        )

        try:
            audio = MP3(mp3_path)
        except Exception:
            pytest.skip("Cannot create minimal MP3 for testing")

        audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="ASIN", text=["B0ABCDEFGH"]))
        audio.save()

        scanner = AudioFileScanner()
        tags = scanner._extract_audio_tags(mp3_path)
        assert tags.asin == "B0ABCDEFGH"

    def test_invalid_isbn_discarded(self, tmp_path: Path) -> None:
        """Invalid ISBN values should be silently discarded."""
        from mutagen.id3 import TXXX
        from mutagen.mp3 import MP3

        mp3_path = tmp_path / "test.mp3"
        mp3_path.write_bytes(
            b"\xff\xfb\x90\x00" + b"\x00" * 417
        )

        try:
            audio = MP3(mp3_path)
        except Exception:
            pytest.skip("Cannot create minimal MP3 for testing")

        audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="ISBN", text=["not-an-isbn"]))
        audio.save()

        scanner = AudioFileScanner()
        tags = scanner._extract_audio_tags(mp3_path)
        assert tags.isbn is None


# ── ASIN fast path short-circuits ──


class _MockAudibleProvider(MetadataProvider):
    """Mock provider that supports ASIN lookup."""

    def __init__(self) -> None:
        super().__init__("MockAudible")
        self.search_called = False

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
    ) -> list[ProviderIdentity]:
        self.search_called = True
        return [
            ProviderIdentity(
                provider="MockAudible",
                external_id="fallback",
                title="Fallback Result",
            )
        ]

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        if external_id == "B0ABCDEFGH":
            return ProviderIdentity(
                provider="MockAudible",
                external_id="B0ABCDEFGH",
                title="The Stand",
                authors=["Stephen King"],
                asin="B0ABCDEFGH",
            )
        return None

    async def _try_asin_lookup(self, asin: str) -> MatchCandidate | None:
        identity = await self.get_by_id(asin)
        if identity is not None:
            return MatchCandidate(
                identity=identity,
                confidence=1.0,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=["ASIN exact match"],
            )
        return None


class TestASINFastPath:
    def test_asin_fast_path_skips_search(self) -> None:
        provider = _MockAudibleProvider()
        audiobook = AudiobookSet(
            source_path=Path("/tmp/the_stand"),
            raw_title_guess="The Stand",
            author_guess="Stephen King",
            asin_guess="B0ABCDEFGH",
        )

        candidates = asyncio.get_event_loop().run_until_complete(
            provider.find_matches(audiobook)
        )

        assert len(candidates) == 1
        assert candidates[0].confidence == 1.0
        assert candidates[0].match_reasons == ["ASIN exact match"]
        assert candidates[0].identity.asin == "B0ABCDEFGH"
        # Search should NOT have been called
        assert not provider.search_called

    def test_asin_not_found_falls_through(self) -> None:
        provider = _MockAudibleProvider()
        audiobook = AudiobookSet(
            source_path=Path("/tmp/test"),
            raw_title_guess="Unknown Book",
            asin_guess="B0XXXXXXXXX",  # invalid — too long, won't match
        )

        candidates = asyncio.get_event_loop().run_until_complete(
            provider.find_matches(audiobook)
        )

        # Should fall through to fuzzy search
        assert provider.search_called
        assert len(candidates) >= 1
