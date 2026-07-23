"""Regressions from the 2026-07-23 user-flow round.

Two defects: loosely-typed sidecar JSON aborted `sidecar read`/`sidecar sync`,
and applying a multi-disc plan stranded per-disc companions plus empty disc
folders, leaving the old layout coexisting with the new one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.operations import TransactionManager
from bookbot.core.planning import PlanBuilder
from bookbot.io.sidecar import SidecarManager

FIXTURE_MP3 = (
    Path(__file__).parent
    / "fixtures"
    / "messy_library"
    / "Brandon Sanderson - The Way of Kings"
    / "CD1"
    / "01 - Prelude.mp3"
)


@pytest.fixture
def sidecars() -> SidecarManager:
    return SidecarManager()


class TestLooselyTypedSidecarJson:
    """Every one of these shapes used to raise past the reader's except."""

    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_empty_series_list(self, sidecars: SidecarManager, tmp_path: Path) -> None:
        """The Tampa fixture: "series": []."""
        path = self._write(tmp_path, {"title": "Tampa", "series": []})
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.title == "Tampa"
        assert result.series_name is None

    def test_series_object_list(
        self, sidecars: SidecarManager, tmp_path: Path
    ) -> None:
        path = self._write(
            tmp_path,
            {"title": "T", "series": [{"name": "Kingkiller", "sequence": "1"}]},
        )
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.series_name == "Kingkiller"

    def test_numeric_isbn(self, sidecars: SidecarManager, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"title": "T", "isbn": 9780062280541})
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.isbn_13 == "9780062280541"

    def test_author_objects(self, sidecars: SidecarManager, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, {"title": "T", "authors": [{"name": "Alissa Nutting"}]}
        )
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.authors == ["Alissa Nutting"]

    def test_list_narrator_and_string_year(
        self, sidecars: SidecarManager, tmp_path: Path
    ) -> None:
        path = self._write(
            tmp_path,
            {"title": "T", "narrator": ["Wendy Tremont King"], "year": "2013"},
        )
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.narrator == "Wendy Tremont King"
        assert result.year == 2013

    def test_plain_string_series_still_works(
        self, sidecars: SidecarManager, tmp_path: Path
    ) -> None:
        path = self._write(tmp_path, {"title": "T", "series": "Real Series"})
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.series_name == "Real Series"

    def test_non_object_json_degrades(
        self, sidecars: SidecarManager, tmp_path: Path
    ) -> None:
        path = self._write(tmp_path, ["not", "an", "object"])
        assert sidecars.read_metadata_json(path) is None

    def test_unparseable_year_degrades(
        self, sidecars: SidecarManager, tmp_path: Path
    ) -> None:
        path = self._write(tmp_path, {"title": "T", "year": "n/a"})
        result = sidecars.read_metadata_json(path)
        assert result is not None
        assert result.year is None


def _make_track(path: Path, title: str, album: str, track: int) -> None:
    """Copy the fixture MP3 and retag it so filenames don't collide."""
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK

    shutil.copy(FIXTURE_MP3, path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text="Dave Grossman"))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=str(track)))
    tags.save(path)


def _build_multidisc_library(root: Path) -> Path:
    """Multi-disc book with a distinct cover in every disc folder."""
    library = root / "lib"
    book = library / "On Combat - Dave Grossman"
    for disc in range(1, 4):
        disc_folder = book / f"On Combat Disc {disc}"
        disc_folder.mkdir(parents=True)
        (disc_folder / "cover.jpg").write_bytes(b"COVER" + bytes([disc]))
        for track in range(1, 3):
            _make_track(
                disc_folder / f"{track:02d} - Track.mp3",
                f"Track {track}",
                f"On Combat Disc {disc}",
                track,
            )
    return library


class TestNoSourceResidueAfterApply:
    def _apply(self, library: Path, config_manager) -> None:
        scanner = AudioFileScanner()
        sets = scanner.scan_directory(library)
        config = config_manager.load_config()
        plan = PlanBuilder(config).create_plan(
            library, sets, source_roots=[library]
        )
        TransactionManager(config_manager).execute_plan(plan)

    def test_every_disc_cover_moves(self, tmp_path: Path, config_manager) -> None:
        library = _build_multidisc_library(tmp_path)
        covers_before = len(list(library.rglob("cover.jpg")))
        assert covers_before == 3

        self._apply(library, config_manager)

        # All three distinct covers survive somewhere in the library.
        images = list(library.rglob("*.jpg"))
        assert len(images) == 3
        assert {image.read_bytes() for image in images} == {
            b"COVER" + bytes([disc]) for disc in range(1, 4)
        }

    def test_old_disc_folders_are_pruned(
        self, tmp_path: Path, config_manager
    ) -> None:
        library = _build_multidisc_library(tmp_path)
        self._apply(library, config_manager)

        assert not (library / "On Combat - Dave Grossman").exists()

    def test_library_root_survives(self, tmp_path: Path, config_manager) -> None:
        library = _build_multidisc_library(tmp_path)
        self._apply(library, config_manager)

        assert library.is_dir()

    def test_folder_with_other_content_is_kept(
        self, tmp_path: Path, config_manager
    ) -> None:
        """Pruning only removes genuinely empty directories."""
        library = tmp_path / "lib"
        book = library / "Author Name - Some Book"
        book.mkdir(parents=True)
        (book / "companion.epub").write_text("EPUB")
        for track in range(1, 3):
            _make_track(
                book / f"{track:02d}.mp3", f"Track {track}", "Some Book", track
            )

        self._apply(library, config_manager)

        assert book.is_dir()
        assert (book / "companion.epub").exists()


class TestBoundedEmptyDirCleanup:
    def test_cleanup_stops_at_boundary(
        self, tmp_path: Path, config_manager
    ) -> None:
        root = tmp_path / "root"
        nested = root / "a" / "b"
        nested.mkdir(parents=True)

        manager = TransactionManager(config_manager)
        manager._cleanup_empty_directories(nested, stop_at=root)

        assert root.is_dir()
        assert not (root / "a").exists()

    def test_cleanup_never_removes_boundary_itself(
        self, tmp_path: Path, config_manager
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()

        manager = TransactionManager(config_manager)
        manager._cleanup_empty_directories(root, stop_at=root)

        assert root.is_dir()
