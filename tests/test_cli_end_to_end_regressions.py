"""Regressions found by exercising every CLI command on a realistic library."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.chapters.models import Chapter
from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudiobookSet, AudioFormat, AudioTags, Track
from bookbot.core.operations import OperationRecord, TransactionManager
from bookbot.core.organizer import SmartOrganizer
from bookbot.core.planning import PlanBuilder
from bookbot.core.templates import TemplateEngine
from bookbot.io.sidecar import SidecarManager

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None or _FFPROBE is None, reason="ffmpeg/ffprobe not installed"
)


def _tone(path: Path, *metadata: str, codec: str = "libmp3lame") -> None:
    assert _FFMPEG is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    for item in metadata:
        args.extend(["-metadata", item])
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={300 + len(str(path)) % 200}:duration=1",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-c:a",
            codec,
            *args,
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _probe(path: Path) -> dict:
    assert _FFPROBE is not None
    result = subprocess.run(
        [
            _FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _enable_conversion(config_dir: Path) -> None:
    manager = ConfigManager(config_dir)
    config = manager.load_config()
    config.conversion.enabled = True
    manager.save_config(config)


# --- ffmpeg-backed conversion and m4b tooling ---


@requires_ffmpeg
def test_concatenate_files_accepts_relative_paths(tmp_path, monkeypatch) -> None:
    from bookbot.convert.ffmpeg import FFmpegWrapper

    monkeypatch.chdir(tmp_path)
    work = Path("work")
    _tone(work / "a.m4a", codec="aac")
    _tone(work / "b.m4a", codec="aac")

    ok = FFmpegWrapper().concatenate_files(
        [work / "a.m4a", work / "b.m4a"], work / "out.m4b"
    )

    assert ok is True
    assert (tmp_path / "work" / "out.m4b").is_file()


@requires_ffmpeg
def test_convert_relative_output_single_track_is_tagged_mp4(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    book = Path("lib") / "Neil Gaiman - American Gods"
    _tone(book / "01.mp3", "artist=Neil Gaiman", "album=American Gods")
    config_dir = tmp_path / "cfg"
    _enable_conversion(config_dir)

    result = CliRunner().invoke(
        cli, ["--config-dir", str(config_dir), "convert", str(book), "-o", "out"]
    )

    assert result.exit_code == 0, result.output
    outputs = list((tmp_path / "out").glob("*.m4b"))
    assert len(outputs) == 1
    probe = _probe(outputs[0])
    assert "mp4" in probe["format"]["format_name"]
    tags = {k.lower(): v for k, v in probe["format"].get("tags", {}).items()}
    assert tags.get("title") == "American Gods"
    assert tags.get("artist") == "Neil Gaiman"


@requires_ffmpeg
def test_convert_multi_track_relative_output_succeeds(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    book = Path("lib") / "Andy Weir - Project Hail Mary"
    _tone(book / "Part 1.mp3", "title=Part 1", "track=1")
    _tone(book / "Part 2.mp3", "title=Part 2", "track=2")
    config_dir = tmp_path / "cfg"
    _enable_conversion(config_dir)

    result = CliRunner().invoke(
        cli, ["--config-dir", str(config_dir), "convert", str(book), "-o", "out"]
    )

    assert result.exit_code == 0, result.output
    output = tmp_path / "out" / "Project Hail Mary.m4b"
    assert [c["tags"]["title"] for c in _probe(output)["chapters"]] == [
        "Part 1",
        "Part 2",
    ]


def test_convert_dry_run_does_not_create_output_directory(tmp_path) -> None:
    book = tmp_path / "Author - Title"
    book.mkdir()
    (book / "01.mp3").touch()
    output = tmp_path / "out"

    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "cfg"),
            "convert",
            str(book),
            "-o",
            str(output),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run complete" in result.output
    assert not output.exists()


def test_convert_bitrate_option_disables_configured_vbr(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_convert_directory(self, source_path, config):  # type: ignore[no-untyped-def]
        captured["config"] = config
        return True

    monkeypatch.setattr(
        "bookbot.convert.pipeline.ConversionPipeline.convert_directory",
        fake_convert_directory,
    )
    monkeypatch.setattr("bookbot.cli.shutil.which", lambda name: "/usr/bin/ffmpeg")
    config_dir = tmp_path / "cfg"
    _enable_conversion(config_dir)
    book = tmp_path / "book"
    book.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "convert",
            str(book),
            "-o",
            str(tmp_path / "out"),
            "--bitrate",
            "64k",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["config"].use_vbr is False
    assert captured["config"].bitrate == "64k"

    result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "convert", str(book), "-o", "out"],
    )
    assert result.exit_code == 0, result.output
    # Without --bitrate the configured encoding mode is left alone.
    assert captured["config"].use_vbr is True


@requires_ffmpeg
def test_m4b_tag_and_cover_work_on_chaptered_m4b(tmp_path) -> None:
    from bookbot.convert.ffmpeg import FFmpegWrapper

    _tone(tmp_path / "a.mp3")
    _tone(tmp_path / "b.mp3")
    cover = tmp_path / "cover.jpg"
    subprocess.run(
        [
            str(_FFMPEG),
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64",
            "-frames:v",
            "1",
            "-y",
            str(cover),
        ],
        check=True,
        capture_output=True,
    )
    ffmpeg = FFmpegWrapper()
    m4b = ffmpeg.merge_to_m4b(
        [tmp_path / "a.mp3", tmp_path / "b.mp3"],
        tmp_path / "book.m4b",
        chapters=[
            Chapter(title="One", start_ms=0, end_ms=1000),
            Chapter(title="Two", start_ms=1000, end_ms=2000),
        ],
        cover=cover,
    )

    probe = _probe(m4b)
    assert any(
        stream.get("disposition", {}).get("attached_pic") == 1
        for stream in probe["streams"]
    )

    ffmpeg.embed_metadata(m4b, {"title": "New Title", "artist": "Someone"})

    probe = _probe(m4b)
    tags = {k.lower(): v for k, v in probe["format"]["tags"].items()}
    assert tags["title"] == "New Title"
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["One", "Two"]
    assert any(
        stream.get("disposition", {}).get("attached_pic") == 1
        for stream in probe["streams"]
    )

    assert ffmpeg.embed_cover_art(m4b, cover) is True


@requires_ffmpeg
def test_m4b_split_to_mp3(tmp_path) -> None:
    from bookbot.convert.ffmpeg import FFmpegWrapper

    _tone(tmp_path / "a.mp3")
    _tone(tmp_path / "b.mp3")
    ffmpeg = FFmpegWrapper()
    m4b = ffmpeg.merge_to_m4b(
        [tmp_path / "a.mp3", tmp_path / "b.mp3"],
        tmp_path / "book.m4b",
        chapters=[
            Chapter(title="One", start_ms=0, end_ms=1000),
            Chapter(title="Two", start_ms=1000, end_ms=2000),
        ],
    )

    outputs = ffmpeg.split_m4b(m4b, tmp_path / "split", output_format="mp3")

    assert [p.name for p in outputs] == ["001 - One.mp3", "002 - Two.mp3"]
    assert _probe(outputs[0])["streams"][0]["codec_name"] == "mp3"


@requires_ffmpeg
def test_m4b_merge_handles_multi_disc_folder_in_disc_order(tmp_path) -> None:
    book = tmp_path / "Tolkien - The Hobbit"
    _tone(book / "CD1" / "Track 1.mp3", "title=D1T1", "track=1", "disc=1")
    _tone(book / "CD1" / "Track 2.mp3", "title=D1T2", "track=2", "disc=1")
    _tone(book / "Disc 2" / "Track 1.mp3", "title=D2T1", "track=1", "disc=2")
    output = tmp_path / "hobbit.m4b"

    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "cfg"),
            "m4b",
            "merge",
            str(book),
            "-o",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    chapters = [c["tags"]["title"] for c in _probe(output)["chapters"]]
    assert chapters == ["D1T1", "D1T2", "D2T1"]


# --- sidecars, config, history ---


def test_read_opf_keeps_narrator_out_of_authors(tmp_path) -> None:
    opf = tmp_path / "metadata.opf"
    opf.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:title>A Wizard of Earthsea</dc:title>
  <dc:creator opf:role="aut">Ursula K. Le Guin</dc:creator>
  <dc:creator opf:role="nrt">Kobna Holdbrook-Smith</dc:creator>
 </metadata>
</package>
""",
        encoding="utf-8",
    )

    identity = SidecarManager().read_opf(opf)

    assert identity is not None
    assert identity.authors == ["Ursula K. Le Guin"]
    assert identity.narrator == "Kobna Holdbrook-Smith"


def test_config_set_rejects_invalid_boolean(tmp_path) -> None:
    config_dir = tmp_path / "cfg"
    _enable_conversion(config_dir)

    result = CliRunner().invoke(
        cli,
        ["--config-dir", str(config_dir), "config", "set", "conversion.enabled", "x"],
    )

    assert result.exit_code == 1
    assert "not a valid boolean" in result.output
    assert ConfigManager(config_dir).load_config().conversion.enabled is True


def test_status_messages_have_separated_prefixes(tmp_path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--config-dir", str(tmp_path), "config", "set", "conversion.bitrate", "96k"],
    )

    assert result.exit_code == 0
    assert "Done: Set conversion.bitrate = 96k" in result.output


def _record_rename(manager: TransactionManager, tmp_path: Path, tx_id: str) -> Path:
    original = tmp_path / f"{tx_id}-before.mp3"
    moved = tmp_path / f"{tx_id}-after.mp3"
    moved.write_text("audio")
    manager.record_transaction(
        tx_id,
        [
            OperationRecord(
                operation_id=tx_id,
                timestamp=datetime(2026, 7, 20, 12, 0, 0),
                operation_type="rename",
                old_path=original,
                new_path=moved,
            )
        ],
        transaction_type="rename",
    )
    return original


def test_undo_accepts_prefix_shown_by_history(tmp_path) -> None:
    config_dir = tmp_path / "cfg"
    manager = TransactionManager(ConfigManager(config_dir))
    tx_id = "1a2b3c4d-0000-4000-8000-000000000001"
    original = _record_rename(manager, tmp_path, tx_id)
    runner = CliRunner()

    history = runner.invoke(cli, ["--config-dir", str(config_dir), "history"])
    shown = history.output.split()[history.output.split().index("-") - 1]

    result = runner.invoke(cli, ["--config-dir", str(config_dir), "undo", shown])

    assert result.exit_code == 0, result.output
    assert original.exists()
    assert tx_id in result.output


def test_undo_rejects_ambiguous_prefix(tmp_path) -> None:
    config_dir = tmp_path / "cfg"
    manager = TransactionManager(ConfigManager(config_dir))
    first = _record_rename(manager, tmp_path, "abcd0001-aaaa")
    second = _record_rename(manager, tmp_path, "abcd0002-bbbb")

    result = CliRunner().invoke(cli, ["--config-dir", str(config_dir), "undo", "abcd"])

    assert result.exit_code == 1
    assert "ambiguous" in result.output.lower()
    assert "abcd0001-aaaa" in result.output
    assert "abcd0002-bbbb" in result.output
    assert not first.exists()
    assert not second.exists()


def test_recover_accepts_unique_prefix(tmp_path) -> None:
    config_dir = tmp_path / "cfg"
    manager = TransactionManager(ConfigManager(config_dir))
    tx_id = "feedbeef-1111"
    original = _record_rename(manager, tmp_path, tx_id)
    log_file = next(manager.log_dir.glob(f"transaction_{tx_id}.json"))
    data = json.loads(log_file.read_text())
    data["status"] = "in_progress"
    log_file.write_text(json.dumps(data))

    result = CliRunner().invoke(
        cli, ["--config-dir", str(config_dir), "recover", "feedbeef"]
    )

    assert result.exit_code == 0, result.output
    assert original.exists()


def test_author_aliases_honor_runtime_config_dir(tmp_path, monkeypatch) -> None:
    from bookbot.core.matching import AdvancedMatcher

    monkeypatch.setenv("BOOKBOT_CONFIG_DIR", str(tmp_path))
    aliases = tmp_path / "author_aliases.json"
    aliases.write_text('{"x": ["y"]}', encoding="utf-8")

    assert AdvancedMatcher._find_aliases_json() == aliases


def test_get_activation_bytes_without_selenium_is_friendly(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "selenium" or name.startswith("selenium."):
            raise ImportError("No module named 'selenium'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = CliRunner().invoke(cli, ["audible", "get-activation-bytes"])

    assert result.exit_code == 1
    assert "pip install selenium" in result.output
    assert "chromedriver" in result.output
    # Fail before asking for credentials that cannot be used.
    assert "Password" not in result.output


# --- discovery, naming, planning, organize ---


def _scanner_set(folder: Path, files: list[Track]) -> AudiobookSet:
    scanner = AudioFileScanner()
    title, author, series, volume = scanner._extract_metadata_guesses(folder, files)
    return AudiobookSet(
        source_path=folder,
        raw_title_guess=title,
        author_guess=author,
        series_guess=series,
        volume_guess=volume,
        tracks=files,
        total_tracks=len(files),
    )


def _track(path: Path, index: int, **tags: str) -> Track:
    return Track(
        src_path=path,
        track_index=index,
        audio_format=AudioFormat.MP3,
        existing_tags=AudioTags(**tags),
    )


def test_book_number_prefix_is_not_an_author(tmp_path) -> None:
    folder = tmp_path / "Brandon Sanderson" / "Mistborn" / "Book 1 - The Final Empire"
    tracks = [
        _track(
            folder / f"0{i}.mp3",
            i,
            artist="Brandon Sanderson",
            album="The Final Empire",
        )
        for i in (1, 2)
    ]

    audiobook_set = _scanner_set(folder, tracks)

    assert audiobook_set.author_guess == "Brandon Sanderson"
    assert audiobook_set.raw_title_guess == "The Final Empire"
    assert audiobook_set.volume_guess == "1"


def test_clean_metadata_name_keeps_word_boundaries() -> None:
    scanner = AudioFileScanner()

    assert (
        scanner._clean_metadata_name("frank herbert - dune (unabridged) [2007] 128kbps")
        == "frank herbert - dune"
    )
    assert scanner._clean_metadata_name("The (Real) Story") == "The Story"


def test_title_case_preserves_initials_and_mixed_case_names() -> None:
    engine = TemplateEngine()

    assert engine._smart_title_case("Tolkien, J.R.R.") == "Tolkien, J.R.R."
    assert engine._smart_title_case("cormac McCarthy") == "Cormac McCarthy"
    assert engine._smart_title_case("dune part 01 FINAL") == "Dune Part 01 Final"


def test_author_last_first_keeps_surname_particles() -> None:
    engine = TemplateEngine()

    assert engine._format_author_last_first("Ursula K. Le Guin") == (
        "Le Guin, Ursula K."
    )
    assert engine._format_author_last_first("Ludwig van Beethoven") == (
        "van Beethoven, Ludwig"
    )
    assert engine._format_author_last_first("Brandon Sanderson") == (
        "Sanderson, Brandon"
    )


def test_prefer_m4b_fallback_survives_unrelated_duplicate(tmp_path) -> None:
    library = tmp_path / "lib"
    books = []
    for folder_name in ("Andy Weir - Project Hail Mary", "Andy Weir - PHM copy"):
        folder = library / folder_name
        folder.mkdir(parents=True)
        tracks = []
        for i in (1, 2):
            path = folder / f"Part {i}.mp3"
            path.touch()
            tracks.append(_track(path, i, title=f"Part {i}"))
        books.append(
            AudiobookSet(
                source_path=folder,
                raw_title_guess="Project Hail Mary",
                author_guess="Andy Weir",
                tracks=tracks,
                total_tracks=2,
            )
        )
    hobbit = library / "Tolkien - The Hobbit"
    hobbit.mkdir()
    hobbit_tracks = []
    for i in (1, 2):
        path = hobbit / f"{i}.mp3"
        path.touch()
        hobbit_tracks.append(_track(path, i, title=f"Chapter {i}"))
    books.append(
        AudiobookSet(
            source_path=hobbit,
            raw_title_guess="The Hobbit",
            author_guess="J.R.R. Tolkien",
            tracks=hobbit_tracks,
            total_tracks=2,
        )
    )

    config = ConfigManager(tmp_path / "cfg").load_profile("plex").config
    plan = PlanBuilder(config).create_plan(library, books, source_roots=[library])

    # Only the genuinely duplicated book is affected: the copy is skipped
    # (not a plan-wide conflict), and the others get unique per-track names
    # instead of all colliding on "{title}".
    assert plan.conflicts == []
    assert [item["source_path"] for item in plan.skipped] == [
        str(library / "Andy Weir - PHM copy")
    ]
    assert "Project Hail Mary" in plan.skipped[0]["reason"]
    original = library / "Andy Weir - Project Hail Mary"
    assert any(op.old_path.parent == original for op in plan.operations)
    hobbit_targets = {
        op.new_path.name for op in plan.operations if op.old_path.parent == hobbit
    }
    assert len(hobbit_targets) == 2


def test_organize_confirm_moves_sidecars_and_prunes_empty_folders(tmp_path) -> None:
    library = tmp_path / "lib"
    folder = library / "Neil Gaiman - American Gods"
    folder.mkdir(parents=True)
    tracks = []
    for i in (1, 2):
        path = folder / f"0{i}.mp3"
        path.write_bytes(b"audio")
        tracks.append(_track(path, i))
    (folder / "cover.jpg").write_bytes(b"jpg")
    book = AudiobookSet(
        source_path=folder,
        raw_title_guess="American Gods",
        author_guess="Neil Gaiman",
        tracks=tracks,
        total_tracks=2,
    )

    organizer = SmartOrganizer()
    plan = organizer.propose_reorganization(library, None, "default", [book])
    assert organizer.execute_plan(plan, dry_run=False) is True

    destination = library / "Gaiman, Neil" / "American Gods"
    assert (destination / "cover.jpg").is_file()
    assert not folder.exists()


def test_duplicate_target_conflicts_are_listed_in_stable_order(tmp_path) -> None:
    from bookbot.core.models import RenameOperation, RenamePlan

    operations = []
    for index in range(12):
        source = tmp_path / f"{index}.mp3"
        source.touch()
        target = tmp_path / "z" / f"{index // 2}.mp3"
        operations.append(RenameOperation(old_path=source, new_path=target))
    plan = RenamePlan(
        plan_id="p",
        created_at=datetime(2026, 1, 1),
        source_path=tmp_path,
        operations=operations,
    )

    plan.validate_plan()

    assert plan.conflicts == sorted(plan.conflicts)
