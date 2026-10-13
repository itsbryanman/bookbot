"""Credential storage must never silently downgrade to plaintext files."""

from pathlib import Path

import pytest

from bookbot.drm import secure_storage


class _BrokenKeyring:
    @staticmethod
    def set_password(*args) -> None:
        raise RuntimeError("no keyring backend")


def test_keyring_failure_requires_explicit_file_choice(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(secure_storage, "keyring", _BrokenKeyring())
    monkeypatch.setattr(secure_storage, "get_runtime_config_dir", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="explicit file credential storage"):
        secure_storage.save_activation_bytes("deadbeef")

    assert list(tmp_path.iterdir()) == []


def test_explicit_file_storage_is_private_from_creation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(secure_storage, "get_runtime_config_dir", lambda: tmp_path)

    destination = secure_storage.save_activation_bytes(
        "deadbeef",
        storage="file",
    )

    credential_file = Path(destination.removeprefix("local file (").removesuffix(")"))
    assert credential_file.read_text() == '{"activation_bytes": "deadbeef"}'
    assert credential_file.stat().st_mode & 0o777 == 0o600
