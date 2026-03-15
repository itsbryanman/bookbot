"""Tests for sidecar metadata reading and writing."""

import json
import tempfile
from pathlib import Path

import pytest

from bookbot.core.models import ProviderIdentity
from bookbot.io.sidecar import SidecarManager


@pytest.fixture
def manager():
    return SidecarManager()


@pytest.fixture
def sample_identity():
    return ProviderIdentity(
        provider="test",
        external_id="test-id",
        title="The Name of the Wind",
        authors=["Patrick Rothfuss"],
        series_name="The Kingkiller Chronicle",
        series_index="1",
        year=2007,
        language="en",
        narrator="Nick Podehl",
        publisher="DAW Books",
        isbn_13="9780756404741",
        asin="B002UZMLXM",
        description="A great fantasy novel.",
    )


class TestOPFRoundTrip:
    def test_write_and_read(self, manager, sample_identity):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.opf"
            manager.write_opf(path, sample_identity)

            assert path.exists()

            result = manager.read_opf(path)
            assert result is not None
            assert result.title == "The Name of the Wind"
            assert "Patrick Rothfuss" in result.authors
            assert result.year == 2007
            assert result.series_name == "The Kingkiller Chronicle"
            assert result.series_index == "1"
            assert result.narrator == "Nick Podehl"

    def test_read_nonexistent(self, manager):
        result = manager.read_opf(Path("/nonexistent/file.opf"))
        assert result is None


class TestJSONRoundTrip:
    def test_write_and_read(self, manager, sample_identity):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.json"
            manager.write_metadata_json(path, sample_identity)

            assert path.exists()

            result = manager.read_metadata_json(path)
            assert result is not None
            assert result.title == "The Name of the Wind"
            assert result.authors == ["Patrick Rothfuss"]
            assert result.year == 2007
            assert result.isbn_13 == "9780756404741"
            assert result.asin == "B002UZMLXM"
            assert result.series_name == "The Kingkiller Chronicle"

    def test_read_invalid_json(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("not valid json{{{")
            result = manager.read_metadata_json(path)
            assert result is None

    def test_read_empty_title(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text(json.dumps({"title": "", "authors": []}))
            result = manager.read_metadata_json(path)
            assert result is None


class TestNFO:
    def test_read_key_value(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audiobook.nfo"
            path.write_text(
                "Title: My Audiobook\nAuthor: John Doe\nYear: 2020\nNarrator: Jane Smith"
            )
            result = manager.read_nfo(path)
            assert result is not None
            assert result.title == "My Audiobook"
            assert result.authors == ["John Doe"]
            assert result.year == 2020
            assert result.narrator == "Jane Smith"

    def test_read_xml_nfo(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audiobook.nfo"
            path.write_text(
                "<audiobook>"
                "<title>XML Book</title>"
                "<author>Author X</author>"
                "<year>2019</year>"
                "</audiobook>"
            )
            result = manager.read_nfo(path)
            assert result is not None
            assert result.title == "XML Book"
            assert result.year == 2019

    def test_read_empty_nfo(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.nfo"
            path.write_text("")
            result = manager.read_nfo(path)
            assert result is None


class TestAutoDetect:
    def test_detects_opf(self, manager, sample_identity):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            manager.write_opf(path / "metadata.opf", sample_identity)
            result = manager.auto_detect_sidecar(path)
            assert result is not None
            assert result.title == "The Name of the Wind"

    def test_detects_json(self, manager, sample_identity):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            manager.write_metadata_json(path / "metadata.json", sample_identity)
            result = manager.auto_detect_sidecar(path)
            assert result is not None

    def test_detects_nfo(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "info.nfo").write_text("Title: Test Book\nAuthor: Author")
            result = manager.auto_detect_sidecar(path)
            assert result is not None
            assert result.title == "Test Book"

    def test_no_sidecar(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            result = manager.auto_detect_sidecar(Path(tmp))
            assert result is None
