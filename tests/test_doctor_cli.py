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
    assert not any(
        "Quarantine contains" in check.message for check in report.checks
    )


def test_doctor_excludes_quarantine_duplicates_and_reports_state(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()

    visible_a = library / "visible-a"
    visible_b = library / "visible-b"
    visible_a.mkdir()
    visible_b.mkdir()
    (visible_a / "track.mp3").write_bytes(b"visible-dupe")
    (visible_b / "track.mp3").write_bytes(b"visible-dupe")

    quarantine_root = library / ".bookbot-quarantine" / "tx-1"
    hidden_a = quarantine_root / "hidden-a" / "track.mp3"
    hidden_b = quarantine_root / "hidden-b" / "track.mp3"
    hidden_a.parent.mkdir(parents=True)
    hidden_b.parent.mkdir(parents=True)
    hidden_a.write_bytes(b"hidden-dupe")
    hidden_b.write_bytes(b"hidden-dupe")

    config_manager = ConfigManager(tmp_path / "config")
    doctor = LibraryDoctor(config_manager.load_config(), config_manager.config_dir)
    report = doctor.run(library_path=library)

    duplicate_file_check = next(
        check
        for check in report.checks
        if "potential duplicate file group(s) detected" in check.message
    )
    quarantine_check = next(
        check for check in report.checks if "Quarantine contains" in check.message
    )

    assert duplicate_file_check.message.startswith(
        "1 potential duplicate file group(s) detected"
    )
    assert str(visible_a / "track.mp3") in duplicate_file_check.message
    assert str(visible_b / "track.mp3") in duplicate_file_check.message
    assert str(hidden_a) not in duplicate_file_check.message
    assert str(hidden_b) not in duplicate_file_check.message

    hidden_total = hidden_a.stat().st_size + hidden_b.stat().st_size
    assert "1 transaction(s)" in quarantine_check.message
    assert f"{hidden_total} bytes" in quarantine_check.message


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
