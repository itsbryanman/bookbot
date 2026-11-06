"""output.cover_filename and organize templates from config/CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.config.models import Config, LibraryOutputConfig
from bookbot.core.doctor import LibraryDoctor
from bookbot.core.health import LibraryHealthChecker
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.core.operations import TransactionManager
from bookbot.core.organizer import SmartOrganizer
from bookbot.core.planning import PlanBuilder
from bookbot.io.sidecar import sidecar_output_paths

IDENTITY = ProviderIdentity(
    provider="audnexus",
    external_id="B0ASR",
    title="All Systems Red",
    authors=["Martha Wells"],
    year=2017,
    cover_urls=["https://example.invalid/art/large.png"],
)


def _book(folder: Path, identity: ProviderIdentity | None = IDENTITY) -> AudiobookSet:
    folder.mkdir(parents=True, exist_ok=True)
    tracks = []
    for index in (1, 2):
        path = folder / f"{index:02d}.mp3"
        path.write_bytes(b"\0" * index)
        tracks.append(
            Track(
                src_path=path,
                disc=1,
                track_index=index,
                file_size=index,
                audio_format=AudioFormat.MP3,
                status=TrackStatus.VALID,
            )
        )
    return AudiobookSet(
        source_path=folder,
        raw_title_guess="All Systems Red",
        author_guess="Martha Wells",
        total_tracks=2,
        tracks=tracks,
        chosen_identity=identity,
    )


def test_default_cover_name_is_unchanged(tmp_path: Path) -> None:
    targets = sidecar_output_paths(tmp_path, IDENTITY, LibraryOutputConfig())
    assert targets["cover"].name == "cover.png"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("folder", "folder.png"),
        ("{title} - cover", "All Systems Red - cover.png"),
        ("{Author}: {Title}", "Martha Wells - All Systems Red.png"),
    ],
)
def test_cover_filename_template(tmp_path: Path, template: str, expected: str) -> None:
    output = LibraryOutputConfig(cover_filename=template)
    assert sidecar_output_paths(tmp_path, IDENTITY, output)["cover"].name == expected


def test_custom_cover_lands_on_disk_and_undo_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_manager = ConfigManager(tmp_path / "cfg")
    config = config_manager.load_config()
    config.output.cover_filename = "folder"
    config.output.write_metadata_json = False
    config_manager.save_config(config)
    monkeypatch.setattr(
        TransactionManager,
        "_download_cover_bytes",
        lambda self, url, timeout: b"\x89PNG fake",
    )

    library = tmp_path / "library"
    audiobook_set = _book(library / "rough")
    plan = PlanBuilder(config).create_plan(
        library_root=library, audiobook_sets=[audiobook_set], source_roots=[library]
    )
    manager = TransactionManager(config_manager)
    assert manager.execute_plan(plan)

    covers = list(library.rglob("folder.png"))
    assert len(covers) == 1
    assert covers[0].read_bytes() == b"\x89PNG fake"
    assert not list(library.rglob("cover.*"))

    assert manager.undo_transaction(plan.applied_transaction_id or "")
    assert not list(library.rglob("folder.png"))
    assert sorted(p.name for p in (library / "rough").iterdir()) == [
        "01.mp3",
        "02.mp3",
    ]


def test_doctor_counts_configured_cover_name(tmp_path: Path) -> None:
    book = _book(tmp_path / "lib" / "book")
    (book.source_path / "folder.jpg").write_bytes(b"x")

    default_doctor = LibraryDoctor(Config(), tmp_path / "cfg")
    assert default_doctor._count_missing_covers([book]) == 1

    config = Config()
    config.output.cover_filename = "folder"
    assert LibraryDoctor(config, tmp_path / "cfg")._count_missing_covers([book]) == 0


def test_health_recognizes_configured_cover_name(tmp_path: Path) -> None:
    book = _book(tmp_path / "lib" / "book")
    (book.source_path / "All Systems Red - art.jpg").write_bytes(b"x")

    default_checker = LibraryHealthChecker()
    assert len(default_checker.check_missing_covers([book])) == 1
    assert any(
        "All Systems Red - art.jpg" in str(p)
        for p in default_checker.check_orphaned_files(tmp_path / "lib", [book])
    )

    checker = LibraryHealthChecker(cover_filename="{title} - art")
    assert checker.check_missing_covers([book]) == []
    assert checker.check_orphaned_files(tmp_path / "lib", [book]) == []


def test_organize_config_preset_uses_output_templates(tmp_path: Path) -> None:
    book = _book(tmp_path / "lib" / "rough")
    plan = SmartOrganizer().propose_reorganization(
        tmp_path / "lib",
        None,
        "config",
        [book],
        folder_template="{author}/{title}",
        file_template="Part {track_pad}",
    )
    destinations = sorted(str(op.destination) for op in plan.operations)
    assert destinations == [
        str(tmp_path / "lib" / "Martha Wells" / "All Systems Red" / "Part 1.mp3"),
        str(tmp_path / "lib" / "Martha Wells" / "All Systems Red" / "Part 2.mp3"),
    ]


def test_organize_cli_honors_config_and_template_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    config_manager = ConfigManager(config_dir)
    config = config_manager.load_config()
    config.output.folder_template = "{author}/{year} - {title}"
    config.output.file_template = "Track {track_pad}"
    config_manager.save_config(config)
    library = tmp_path / "lib"
    monkeypatch.setattr(
        "bookbot.core.discovery.AudioFileScanner.scan_directory",
        lambda self, path, progress=None: [_book(library / "rough")],
    )
    runner = CliRunner()

    from_config = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "organize",
            str(library),
            "--template",
            "config",
        ],
    )
    assert from_config.exit_code == 0, from_config.output
    assert "Martha Wells/2017 - All Systems Red/Track 1.mp3" in from_config.output

    from_flags = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "organize",
            str(library),
            "--folder-template",
            "{title}",
            "--file-template",
            "{title} {track_pad}",
        ],
    )
    assert from_flags.exit_code == 0, from_flags.output
    assert "All Systems Red/All Systems Red 1.mp3" in from_flags.output
