import json
import os
from pathlib import Path
from typing import Literal

try:
    import keyring  # type: ignore
except ImportError:
    keyring = None  # type: ignore[assignment]

import toml

from ..config.manager import get_runtime_config_dir
from .models import Token


def _get_fallback_file(service_name: str, key_name: str) -> Path:
    """Get fallback file path for when keyring is unavailable."""
    config_dir = get_runtime_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / f".{service_name}_{key_name}.json"


def _write_private_text(path: Path, content: str) -> None:
    """Create or replace a credential file with private permissions from byte one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temp_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _require_file_choice(storage: Literal["keyring", "file"], error: Exception) -> None:
    if storage == "file":
        return
    raise RuntimeError(
        "System keyring storage is unavailable. Re-run with explicit file "
        "credential storage if you accept credentials being saved in a local "
        "owner-readable file."
    ) from error


def save_token(
    token: Token,
    service_name: str = "bookbot",
    *,
    storage: Literal["keyring", "file"] = "keyring",
) -> str:
    """
    Saves a token to the system's keyring or fallback file.
    """
    token_data = toml.dumps({"token": token.model_dump()})

    if storage == "keyring" and keyring is not None:
        try:
            keyring.set_password(service_name, "token", token_data)
            return "system keyring"
        except Exception as exc:
            _require_file_choice(storage, exc)

    if storage == "keyring":
        _require_file_choice(storage, RuntimeError("keyring package unavailable"))

    fallback_file = _get_fallback_file(service_name, "token")
    _write_private_text(fallback_file, token_data)
    return f"local file ({fallback_file})"


def load_token(service_name: str = "bookbot") -> Token | None:
    """
    Loads a token from the system's keyring or fallback file.
    """
    stored_token = None

    if keyring is not None:
        try:
            stored_token = keyring.get_password(service_name, "token")
        except Exception:
            pass

    if not stored_token:
        fallback_file = _get_fallback_file(service_name, "token")
        if fallback_file.exists():
            stored_token = fallback_file.read_text()

    if stored_token:
        return Token.model_validate(toml.loads(stored_token)["token"])
    return None


def delete_token(service_name: str = "bookbot") -> None:
    """
    Deletes a token from the system's keyring and fallback file.
    """
    if keyring is not None:
        try:
            keyring.delete_password(service_name, "token")
        except Exception:
            pass

    fallback_file = _get_fallback_file(service_name, "token")
    if fallback_file.exists():
        fallback_file.unlink()


def save_activation_bytes(
    activation_bytes: str,
    service_name: str = "bookbot",
    *,
    storage: Literal["keyring", "file"] = "keyring",
) -> str:
    """
    Saves activation bytes to the system's keyring or fallback file.
    """
    if storage == "keyring" and keyring is not None:
        try:
            keyring.set_password(service_name, "activation_bytes", activation_bytes)
            return "system keyring"
        except Exception as exc:
            _require_file_choice(storage, exc)

    if storage == "keyring":
        _require_file_choice(storage, RuntimeError("keyring package unavailable"))

    fallback_file = _get_fallback_file(service_name, "activation_bytes")
    _write_private_text(
        fallback_file,
        json.dumps({"activation_bytes": activation_bytes}),
    )
    return f"local file ({fallback_file})"


def load_activation_bytes(service_name: str = "bookbot") -> str | None:
    """
    Loads activation bytes from the system's keyring or fallback file.
    """
    activation_bytes = None

    if keyring is not None:
        try:
            activation_bytes = keyring.get_password(service_name, "activation_bytes")
        except Exception:
            pass

    if not activation_bytes:
        fallback_file = _get_fallback_file(service_name, "activation_bytes")
        if fallback_file.exists():
            data = json.loads(fallback_file.read_text())
            activation_bytes = data.get("activation_bytes")

    return activation_bytes


def delete_activation_bytes(service_name: str = "bookbot") -> None:
    """
    Deletes activation bytes from the system's keyring and fallback file.
    """
    if keyring is not None:
        try:
            keyring.delete_password(service_name, "activation_bytes")
        except Exception:
            pass

    fallback_file = _get_fallback_file(service_name, "activation_bytes")
    if fallback_file.exists():
        fallback_file.unlink()
