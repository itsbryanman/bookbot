"""Tests for template engine functionality."""

from pathlib import Path

import pytest

from bookbot.config.models import CasePolicy
from bookbot.core.models import AudiobookSet, AudioFormat, ProviderIdentity, Track
from bookbot.core.templates import TemplateEngine


class TestTemplateEngine:
    """Test cases for TemplateEngine."""

    @pytest.fixture
    def sample_audiobook_set(self) -> AudiobookSet:
        """Create a sample audiobook set for testing."""
        return AudiobookSet(
            source_path=Path("/path/to/audiobook"),
            raw_title_guess="The Way of Kings",
            author_guess="Brandon Sanderson",
            series_guess="The Stormlight Archive",
            volume_guess="1",
            disc_count=2,
            total_tracks=6,
        )

    @pytest.fixture
    def sample_identity(self) -> ProviderIdentity:
        """Create a sample provider identity for testing."""
        return ProviderIdentity(
            provider="Open Library",
            external_id="/works/12345",
            title="The Way of Kings",
            authors=["Brandon Sanderson"],
            series_name="The Stormlight Archive",
            series_index="1",
            year=2010,
            language="en",
        )

    @pytest.fixture
    def sample_track(self) -> Track:
        """Create a sample track for testing."""
        return Track(
            src_path=Path("/path/to/track01.mp3"),
            disc=1,
            track_index=1,
            audio_format=AudioFormat.MP3,
        )

    def test_build_tokens_with_identity(
        self, sample_audiobook_set, sample_identity, sample_track
    ):
        """Test building template tokens with provider identity."""
        engine = TemplateEngine(case_policy=CasePolicy.AS_IS)

        tokens = engine._build_tokens(
            sample_audiobook_set, sample_identity, sample_track, zero_padding_width=2
        )

        assert tokens["Title"] == "The Way of Kings"
        assert tokens["Author"] == "Brandon Sanderson"
        assert tokens["AuthorLastFirst"] == "Sanderson, Brandon"
        assert tokens["SeriesName"] == "The Stormlight Archive"
        assert tokens["SeriesIndex"] == "1"
        assert tokens["Year"] == "2010"
        assert tokens["TrackPad"] == "01"
        assert tokens["Track"] == "1"
        assert tokens["DiscPad"] == "1"

    def test_build_tokens_without_identity(self, sample_audiobook_set, sample_track):
        """Test building template tokens without provider identity."""
        engine = TemplateEngine(case_policy=CasePolicy.AS_IS)

        tokens = engine._build_tokens(
            sample_audiobook_set, None, sample_track, zero_padding_width=3
        )

        assert tokens["Title"] == "The Way of Kings"
        assert tokens["Author"] == "Brandon Sanderson"
        assert tokens["AuthorLastFirst"] == "Sanderson, Brandon"
        assert tokens["SeriesName"] == "The Stormlight Archive"
        assert tokens["SeriesIndex"] == "1"
        assert tokens["Year"] == ""
        assert tokens["TrackPad"] == "001"

    def test_generate_filename(
        self, sample_audiobook_set, sample_identity, sample_track
    ):
        """Test filename generation from template."""
        engine = TemplateEngine()

        filename = engine.generate_filename(
            sample_track,
            sample_audiobook_set,
            sample_identity,
            template="{DiscPad}{TrackPad} - {Title}",
            zero_padding_width=2,
        )

        assert filename == "101 - The Way Of Kings.mp3"

    def test_generate_folder_name(self, sample_audiobook_set, sample_identity):
        """Test folder name generation from template."""
        engine = TemplateEngine()

        folder_name = engine.generate_folder_name(
            sample_audiobook_set,
            sample_identity,
            template="{AuthorLastFirst}/{SeriesName}/{SeriesIndex} - {Title} ({Year})",
        )

        assert (
            folder_name
            == "Sanderson, Brandon/The Stormlight Archive/1 - The Way Of Kings (2010)"
        )

    def test_case_policies(self, sample_audiobook_set, sample_identity):
        """Test different case policies."""
        test_cases = [
            (CasePolicy.TITLE_CASE, "The Way Of Kings"),
            (CasePolicy.LOWER_CASE, "the way of kings"),
            (CasePolicy.UPPER_CASE, "THE WAY OF KINGS"),
            (CasePolicy.AS_IS, "The Way of Kings"),
        ]

        for case_policy, expected in test_cases:
            engine = TemplateEngine(case_policy=case_policy)
            tokens = engine._build_tokens(sample_audiobook_set, sample_identity)
            assert tokens["Title"] == expected

    def test_smart_title_case(self):
        """Test smart title case functionality."""
        engine = TemplateEngine()

        test_cases = [
            ("the way of kings", "The Way Of Kings"),
            ("a song of ice and fire", "A Song Of Ice And Fire"),
            ("the lord of the rings", "The Lord Of The Rings"),
            ("ready player one", "Ready Player One"),
        ]

        for input_text, expected in test_cases:
            result = engine._smart_title_case(input_text)
            assert result == expected

    def test_author_last_first_formatting(self):
        """Test author name formatting."""
        engine = TemplateEngine()

        test_cases = [
            ("Brandon Sanderson", "Sanderson, Brandon"),
            ("J.K. Rowling", "Rowling, J.K."),
            ("Stephen King", "King, Stephen"),
            ("Madonna", "Madonna"),  # Single name
            ("Jean-Claude Van Damme", "Damme, Jean-Claude Van"),
        ]

        for input_name, expected in test_cases:
            result = engine._format_author_last_first(input_name)
            assert result == expected

    def test_shorten_title(self):
        """Test title shortening functionality."""
        engine = TemplateEngine()

        long_title = "This Is A Very Long Title That Should Be Shortened"
        short_result = engine._shorten_title(long_title, max_length=20)

        assert len(short_result) <= 20
        assert short_result.startswith("This Is A Very Long")

    def test_normalize_filename(self):
        """Test filename normalization."""
        engine = TemplateEngine()

        test_cases = [
            ("File with spaces.mp3", "File with spaces.mp3"),
            ("File/with\\forbidden:chars.mp3", "File_with_forbidden_chars.mp3"),
            (
                "File<with>more|forbidden?chars*.mp3",
                "File_with_more_forbidden_chars_.mp3",
            ),
            ("File\"with'quotes.mp3", "File_with'quotes.mp3"),
        ]

        for input_filename, expected in test_cases:
            result = engine._normalize_filename(input_filename)
            assert result == expected

    def test_validate_template(self):
        """Test template validation."""
        engine = TemplateEngine()

        # Valid templates
        valid_templates = [
            "{Author} - {Title}",
            "{DiscPad}{TrackPad} - {Title}",
            "{AuthorLastFirst}/{SeriesName}/{Title}",
        ]

        for template in valid_templates:
            is_valid, errors = engine.validate_template(template)
            assert is_valid, f"Template should be valid: {template}, errors: {errors}"

        # Invalid templates
        invalid_templates = [
            "{Author - {Title}",  # Unmatched braces
            "{Author} - {InvalidToken}",  # Unknown token
            "{Author} - {Title<>}",  # Forbidden characters
        ]

        for template in invalid_templates:
            is_valid, errors = engine.validate_template(template)
            assert not is_valid, f"Template should be invalid: {template}"
            assert len(errors) > 0

    def test_zero_padding_detection(self, sample_audiobook_set, sample_track):
        """Test automatic zero padding width detection."""
        engine = TemplateEngine()

        # Create audiobook set with many tracks to test padding
        sample_audiobook_set.total_tracks = 150
        sample_audiobook_set.tracks = [
            Track(
                src_path=Path(f"track{i:03d}.mp3"),
                disc=1,
                track_index=i,
                audio_format=AudioFormat.MP3,
            )
            for i in range(1, 151)
        ]

        tokens = engine._build_tokens(
            sample_audiobook_set,
            None,
            sample_track,
            zero_padding_width=0,  # Auto-detect
        )

        # Should detect 3-digit padding needed for 150 tracks
        assert tokens["TrackPad"] == "001"

    def test_multi_disc_handling(self, sample_audiobook_set, sample_identity):
        """Test handling of multi-disc audiobooks."""
        engine = TemplateEngine()

        # Create a track on disc 2
        track_disc2 = Track(
            src_path=Path("/path/to/track01_disc2.mp3"),
            disc=2,
            track_index=1,
            audio_format=AudioFormat.MP3,
        )

        tokens = engine._build_tokens(
            sample_audiobook_set, sample_identity, track_disc2, zero_padding_width=2
        )

        assert tokens["Disc"] == "2"
        assert tokens["DiscPad"] == "2"  # Should be padded for 2-disc set

    def test_single_disc_no_disc_padding(self):
        """Test that single-disc audiobooks don't include disc padding."""
        engine = TemplateEngine()

        single_disc_set = AudiobookSet(
            source_path=Path("/path/to/audiobook"), disc_count=1, total_tracks=10
        )

        track = Track(
            src_path=Path("/path/to/track01.mp3"),
            disc=1,
            track_index=1,
            audio_format=AudioFormat.MP3,
        )

        tokens = engine._build_tokens(single_disc_set, None, track)

        assert tokens["Disc"] == ""
        assert tokens["DiscPad"] == ""
