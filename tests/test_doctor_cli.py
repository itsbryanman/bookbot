"""CLI and doctor regression tests for duplicate reporting."""

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.dedupe import DedupeEngine
from bookbot.core.doctor import LibraryDoctor

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_cli_subprocess(
    args: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    pythonpath = full_env.get("PYTHONPATH")
    full_env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not pythonpath
        else f"{REPO_ROOT}{os.pathsep}{pythonpath}"
    )

    return subprocess.run(
        [sys.executable, "-m", "bookbot.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


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


def test_doctor_reports_quarantine_local_transaction_ids_and_undo_guidance(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    original_config = ConfigManager(tmp_path / "original-config")
    current_config = ConfigManager(tmp_path / "current-config")

    a_dir = library / "a"
    b_dir = library / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir()
    (a_dir / "track.mp3").write_bytes(b"same-audio")
    (b_dir / "track.mp3").write_bytes(b"same-audio")

    engine = DedupeEngine(library)
    plan = engine.build_plan(file_groups=engine.analyze_files())
    engine.execute_plan(plan, original_config)

    doctor = LibraryDoctor(current_config.load_config(), current_config.config_dir)
    report = doctor.run(library_path=library)
    quarantine_check = next(
        check for check in report.checks if "Quarantine contains" in check.message
    )

    assert plan.plan_id in quarantine_check.message
    assert plan.created_at in quarantine_check.message
    assert "bookbot history" not in quarantine_check.message
    assert f"bookbot undo {plan.plan_id}" in quarantine_check.message
    assert "original --config-dir" in quarantine_check.message

    monkeypatch.chdir(library)
    result = runner.invoke(
        cli,
        ["--config-dir", str(current_config.config_dir), "undo", plan.plan_id],
    )

    assert result.exit_code == 0
    assert f"Transaction {plan.plan_id} undone successfully" in result.output
    assert (a_dir / "track.mp3").exists()
    assert (b_dir / "track.mp3").exists()


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


def test_cli_help_doctor_and_scan_avoid_default_home_log_warning(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    config_dir = tmp_path / "config-root"
    library.mkdir()
    home_blocker = tmp_path / "home-blocker"
    home_blocker.write_text("not a directory")

    env = {"HOME": str(home_blocker)}

    help_result = _run_cli_subprocess(
        ["--config-dir", str(config_dir), "--help"],
        env=env,
    )
    assert help_result.returncode == 0
    assert "Warning: log directory" not in (help_result.stdout + help_result.stderr)

    doctor_result = _run_cli_subprocess(
        ["--config-dir", str(config_dir), "doctor", str(library)],
        env=env,
    )
    assert doctor_result.returncode == 0
    assert "Warning: log directory" not in (
        doctor_result.stdout + doctor_result.stderr
    )
    assert (config_dir / "logs" / "doctor.jsonl").exists()

    scan_result = _run_cli_subprocess(
        ["--config-dir", str(config_dir), "scan", str(library)],
        env=env,
    )
    assert scan_result.returncode == 0
    assert "Warning: log directory" not in (scan_result.stdout + scan_result.stderr)


def test_doctor_warns_once_when_resolved_log_dir_is_unwritable(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    config_dir = tmp_path / "config-root"
    library.mkdir()
    config_dir.mkdir()
    (config_dir / "logs").write_text("not a directory")

    result = _run_cli_subprocess(
        ["--config-dir", str(config_dir), "doctor", str(library)],
    )

    combined_output = result.stdout + result.stderr
    warning = (
        f"Warning: log directory {config_dir / 'logs'} is not writable; "
        "falling back to stderr-only logging."
    )

    assert result.returncode == 0
    assert combined_output.count(warning) == 1
    assert (
        f"Warning: {config_dir / 'logs'} is not writable; using "
        f"{config_dir / 'logs'} instead."
    ) not in combined_output
