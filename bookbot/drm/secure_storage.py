import json
from pathlib import Path

try:
    import keyring  # type: ignore
except ImportError:
    keyring = None  # type: ignore[assignment]

import toml

from .models import Token


def _get_fallback_file(service_name: str, key_name: str) -> Path:
    """Get fallback file path for when keyring is unavailable."""
    config_dir = Path.home() / ".config" / "bookbot"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / f".{service_name}_{key_name}.json"


def save_token(token: Token, service_name: str = "bookbot") -> None:
    """
    Saves a token to the system's keyring or fallback file.
    """
    token_data = toml.dumps({"token": token.model_dump()})

    if keyring is not None:
        try:
            keyring.set_password(service_name, "token", token_data)
            return
        except Exception:
            pass  # Fall through to file storage

    # Fallback to file storage
    fallback_file = _get_fallback_file(service_name, "token")
    fallback_file.write_text(token_data)
    fallback_file.chmod(0o600)


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


def save_activation_bytes(activation_bytes: str, service_name: str = "bookbot") -> None:
    """
    Saves activation bytes to the system's keyring or fallback file.
    """
    if keyring is not None:
        try:
            keyring.set_password(service_name, "activation_bytes", activation_bytes)
            return
        except Exception:
            pass

    # Fallback to file storage
    fallback_file = _get_fallback_file(service_name, "activation_bytes")
    fallback_file.write_text(json.dumps({"activation_bytes": activation_bytes}))
    fallback_file.chmod(0o600)


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
