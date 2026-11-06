"""Audiobookshelf-style metadata.json output, merge and CLI wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.config.models import LibraryOutputConfig
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.operations import TransactionManager
from bookbot.core.planning import PlanBuilder
from bookbot.io.sidecar import SidecarManager

# advplyr/audiobookshelf master,
# server/utils/generators/abmetadataGenerator.js, mediaTypeKeys.book
ABS_BOOK_KEYS = {
    "tags": "stringArray",
    "title": "string",
    "subtitle": "string",
    "authors": "stringArray",
    "narrators": "stringArray",
    "series": "stringArray",
    "genres": "stringArray",
    "publishedYear": "string",
    "publishedDate": "string",
    "publisher": "string",
    "description": "string",
    "isbn": "string",
    "asin": "string",
    "language": "string",
    "explicit": "boolean",
    "abridged": "boolean",
}

IDENTITY = ProviderIdentity(
    provider="audnexus",
    external_id="B0ASR",
    title="All Systems Red",
    subtitle="The Murderbot Diaries, Book 1",
    authors=["Martha Wells"],
    narrator="Kevin R. Free; Jane Doe & John Roe",
    series_name="The Murderbot Diaries",
    series_index="1",
    genres=["Science Fiction", "Novella"],
    year=2017,
    publisher="Tor.com",
    description="A murderous android discovers itself.",
    isbn_13="9780765397539",
    asin="B06XG7DXSL",
    language="English",
)


def _abs_type_ok(value: object, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def test_abs_output_keys_and_types_match_abs_parser(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    SidecarManager().write_metadata_json(path, IDENTITY, style="audiobookshelf")
    data = json.loads(path.read_text())

    for key, value in data.items():
        assert key in ABS_BOOK_KEYS, f"ABS ignores key {key!r}"
        assert _abs_type_ok(value, ABS_BOOK_KEYS[key]), (key, value)

    assert data == {
        "title": "All Systems Red",
        "subtitle": "The Murderbot Diaries, Book 1",
        "authors": ["Martha Wells"],
        "narrators": ["Kevin R. Free", "Jane Doe", "John Roe"],
        "series": ["The Murderbot Diaries #1"],
        "genres": ["Science Fiction", "Novella"],
        "publishedYear": "2017",
        "publisher": "Tor.com",
        "description": "A murderous android discovers itself.",
        "isbn": "9780765397539",
        "asin": "B06XG7DXSL",
        "language": "English",
    }


def test_abs_series_without_index_has_no_sequence(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    identity = IDENTITY.model_copy(update={"series_index": None})
    SidecarManager().write_metadata_json(path, identity, style="audiobookshelf")
    assert json.loads(path.read_text())["series"] == ["The Murderbot Diaries"]


def test_abs_round_trip_keeps_identity(tmp_path: Path) -> None:
    manager = SidecarManager()
    path = tmp_path / "metadata.json"
    manager.write_metadata_json(path, IDENTITY, style="audiobookshelf")

    read = manager.read_metadata_json(path)

    assert read is not None
    for field in (
        "title",
        "subtitle",
        "authors",
        "series_name",
        "series_index",
        "genres",
        "year",
        "publisher",
        "description",
        "isbn_13",
        "asin",
        "language",
    ):
        assert getattr(read, field) == getattr(IDENTITY, field), field
    assert read.narrator == "Kevin R. Free, Jane Doe, John Roe"


def test_booklore_style_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    SidecarManager().write_metadata_json(path, IDENTITY, style="booklore")
    data = json.loads(path.read_text())
    assert data["series"] == "The Murderbot Diaries"
    assert data["seriesIndex"] == "1"
    assert data["year"] == 2017
    assert data["narrator"] == "Kevin R. Free; Jane Doe & John Roe"


def test_merge_preserves_keys_bookbot_does_not_model(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    existing = {
        "title": "Old Title",
        "chapters": [{"id": 0, "start": 0, "end": 60, "title": "Opening"}],
        "tags": ["favorite"],
        "genres": ["Kept Genre"],
        "explicit": False,
        "abridged": True,
        "customKey": {"nested": 1},
    }
    path.write_text(json.dumps(existing))
    identity = IDENTITY.model_copy(update={"genres": []})

    SidecarManager().write_metadata_json(
        path, identity, style="audiobookshelf", existing=existing
    )
    data = json.loads(path.read_text())

    assert data["title"] == "All Systems Red"
    assert data["chapters"] == existing["chapters"]
    assert data["tags"] == ["favorite"]
    assert data["genres"] == ["Kept Genre"]
    assert data["explicit"] is False
    assert data["abridged"] is True
    assert data["customKey"] == {"nested": 1}


def test_audiobookshelf_profile_defaults_to_abs_style(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "cfg")
    profile = manager.load_profile("audiobookshelf")
    assert profile is not None
    assert profile.config.output.metadata_json_style == "audiobookshelf"
    assert LibraryOutputConfig().metadata_json_style == "booklore"


def _book(folder: Path) -> AudiobookSet:
    folder.mkdir(parents=True)
    track_path = folder / "01.mp3"
    track_path.write_bytes(b"\0")
    return AudiobookSet(
        source_path=folder,
        raw_title_guess="All Systems Red",
        total_tracks=1,
        tracks=[
            Track(
                src_path=track_path,
                disc=1,
                track_index=1,
                file_size=1,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
        ],
        chosen_identity=IDENTITY,
    )


def test_apply_merges_existing_metadata_json_and_undo_restores(
    tmp_path: Path,
) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    config = config_manager.load_config()
    config.output.metadata_json_style = "audiobookshelf"
    config.output.write_cover = False
    config_manager.save_config(config)

    library = tmp_path / "library"
    audiobook_set = _book(library / "rough")
    original = {
        "title": "rough",
        "chapters": [{"id": 0, "start": 0, "end": 5, "title": "Intro"}],
        "tags": ["kept"],
    }
    (audiobook_set.source_path / "metadata.json").write_text(json.dumps(original))

    plan = PlanBuilder(config).create_plan(
        library_root=library, audiobook_sets=[audiobook_set], source_roots=[library]
    )
    manager = TransactionManager(config_manager)
    assert manager.execute_plan(plan)

    written = sorted(library.rglob("metadata.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert data["title"] == "All Systems Red"
    assert data["series"] == ["The Murderbot Diaries #1"]
    assert data["chapters"] == original["chapters"]
    assert data["tags"] == ["kept"]

    assert manager.undo_transaction(plan.applied_transaction_id or "")
    restored = json.loads((library / "rough" / "metadata.json").read_text())
    assert restored == original


def test_sidecar_write_uses_configured_style_and_merges(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_manager = ConfigManager(config_dir)
    config = config_manager.load_config()
    config.output.metadata_json_style = "audiobookshelf"
    config_manager.save_config(config)

    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({"chapters": [], "x": 1}))

    def fake_scan(self, path, progress=None):  # type: ignore[no-untyped-def]
        return [_book(tmp_path / "unused")]

    runner = CliRunner()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("bookbot.core.discovery.AudioFileScanner.scan_directory", fake_scan)
        result = runner.invoke(
            cli,
            [
                "--config-dir",
                str(config_dir),
                "sidecar",
                "write",
                str(folder),
                "--format",
                "json",
            ],
        )
    assert result.exit_code == 0, result.output
    data = json.loads((folder / "metadata.json").read_text())
    assert data["series"] == ["The Murderbot Diaries #1"]
    assert data["x"] == 1
    assert data["chapters"] == []


@pytest.mark.parametrize(
    "command",
    [
        ["match"],
        ["review-queue"],
        ["accept", "--set", "1", "--skip"],
        ["scan", "--match"],
    ],
)
def test_metadata_from_files_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    seen: list[bool] = []

    class _NoMatches:
        async def find_matches(self, audiobook_set, limit=10):  # type: ignore[no-untyped-def]
            return []

        async def close(self) -> None:
            return None

    def fake_provider(config_manager, metadata_from_files=False):  # type: ignore[no-untyped-def]
        seen.append(metadata_from_files)
        return _NoMatches()

    monkeypatch.setattr("bookbot.cli._build_matching_provider", fake_provider)
    monkeypatch.setattr(
        "bookbot.core.discovery.AudioFileScanner.scan_directory",
        lambda self, path, progress=None: [_book(library / "book")],
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "cfg"),
            command[0],
            str(library),
            *command[1:],
            "--metadata-from-files",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [True]
