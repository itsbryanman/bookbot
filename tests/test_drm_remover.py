"""Safety and integrity tests for protected audiobook conversion."""

import subprocess
from pathlib import Path

from bookbot.drm.remover import DRMRemover


def _protected_file(tmp_path: Path, suffix: str = ".aax") -> Path:
    path = tmp_path / f"owned-book{suffix}"
    path.write_bytes(b"synthetic protected fixture")
    return path


def test_aax_rejects_malformed_activation_bytes(monkeypatch, tmp_path) -> None:
    source = _protected_file(tmp_path)
    called = False

    def unexpected_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("FFmpeg must not run with malformed credentials")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    result = DRMRemover().remove_drm(source, activation_bytes="not-a-key")

    assert result.success is False
    assert "8 hexadecimal" in (result.error_message or "")
    assert called is False


def test_aax_publishes_only_after_output_validation(monkeypatch, tmp_path) -> None:
    source = _protected_file(tmp_path)
    output = tmp_path / "clean" / "book.m4a"
    commands: list[list[str]] = []

    def successful_run(command, **kwargs):
        commands.append(command)
        if "-activation_bytes" in command:
            Path(command[-1]).write_bytes(b"valid audio container")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", successful_run)

    result = DRMRemover().remove_drm(
        source,
        output_path=output,
        activation_bytes="DEADBEEF",
    )

    assert result.success is True
    assert result.output_file == output
    assert output.read_bytes() == b"valid audio container"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-activation_bytes") + 1] == "deadbeef"
    assert not list(output.parent.glob("*.partial*"))


def test_failed_conversion_preserves_existing_output(monkeypatch, tmp_path) -> None:
    source = _protected_file(tmp_path)
    output = tmp_path / "book.m4a"
    output.write_bytes(b"existing clean copy")

    def failed_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command, stderr="bad activation bytes")

    monkeypatch.setattr(subprocess, "run", failed_run)

    result = DRMRemover().remove_drm(
        source,
        output_path=output,
        activation_bytes="deadbeef",
    )

    assert result.success is False
    assert "bad activation bytes" in (result.error_message or "")
    assert output.read_bytes() == b"existing clean copy"
    assert not list(tmp_path.glob("*.partial*"))


def test_aaxc_explains_voucher_requirement(monkeypatch, tmp_path) -> None:
    source = _protected_file(tmp_path, ".aaxc")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("AAXC must not be passed to FFmpeg without a voucher")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    result = DRMRemover().remove_drm(source)

    assert result.success is False
    assert "voucher and content key" in (result.error_message or "")


def test_output_cannot_replace_protected_input(tmp_path) -> None:
    source = _protected_file(tmp_path)

    result = DRMRemover().remove_drm(
        source,
        output_path=source,
        activation_bytes="deadbeef",
    )

    assert result.success is False
    assert "must differ" in (result.error_message or "")
    assert source.read_bytes() == b"synthetic protected fixture"
