"""CLI and doctor regression tests for duplicate reporting."""

from pathlib import Path

from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.doctor import LibraryDoctor


def test_dedupe_audio_hash_flag_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    library.mkdir()

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "config"),
            "dedupe",
            str(library),
            "--audio-hash",
        ],
    )

    assert result.exit_code == 1
    assert "--audio-hash is not implemented yet" in result.output


def test_doctor_reports_duplicate_examples(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()

    edition_a = library / "Stephen King - The Stand"
    edition_b = library / "Stephen King - The Stand (Unabridged)"
    edition_a.mkdir()
    edition_b.mkdir()
    (edition_a / "book.m4b").write_bytes(b"edition-a")
    (edition_b / "book.m4b").write_bytes(b"edition-b")

    duplicate_a = library / "byte-a"
    duplicate_b = library / "byte-b"
    duplicate_a.mkdir()
    duplicate_b.mkdir()
    (duplicate_a / "track.mp3").write_bytes(b"same-bytes")
    (duplicate_b / "track.mp3").write_bytes(b"same-bytes")

    config_manager = ConfigManager(tmp_path / "config")
    doctor = LibraryDoctor(config_manager.load_config(), config_manager.config_dir)
    report = doctor.run(library_path=library)

    duplicate_title_check = next(
        check
        for check in report.checks
        if "duplicate edition group(s) detected" in check.message
    )
    duplicate_file_check = next(
        check
        for check in report.checks
        if "potential duplicate file group(s) detected" in check.message
    )

    assert str(edition_a) in duplicate_title_check.message
    assert str(edition_b) in duplicate_title_check.message
    assert str(duplicate_a / "track.mp3") in duplicate_file_check.message
    assert str(duplicate_b / "track.mp3") in duplicate_file_check.message


def test_doctor_uses_config_dir_with_unusable_home(tmp_path: Path) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    config_dir = tmp_path / "config-root"
    library.mkdir()
    home_blocker = tmp_path / "home-blocker"
    home_blocker.write_text("not a directory")

    result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "doctor", str(library)],
        env={"HOME": str(home_blocker)},
    )

    assert result.exit_code == 0
    assert (config_dir / "logs" / "doctor.jsonl").exists()
