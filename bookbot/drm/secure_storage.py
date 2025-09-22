import keyring  # type: ignore
import toml

from bookbot.drm.models import Token


def save_token(token: Token, service_name: str = "bookbot") -> None:
    """
    Saves a token to the system's keyring.
    """
    keyring.set_password(
        service_name, "token", toml.dumps({"token": token.model_dump()})
    )


def load_token(service_name: str = "bookbot") -> Token | None:
    """
    Loads a token from the system's keyring.
    """
    stored_token = keyring.get_password(service_name, "token")
    if stored_token:
        return Token.model_validate(toml.loads(stored_token)["token"])
    return None


def delete_token(service_name: str = "bookbot") -> None:
    """
    Deletes a token from the system's keyring.
    """
    try:
        keyring.delete_password(service_name, "token")
    except keyring.errors.PasswordDeleteError:
        pass


def save_activation_bytes(activation_bytes: str, service_name: str = "bookbot") -> None:
    """
    Saves activation bytes to the system's keyring.
    """
    keyring.set_password(service_name, "activation_bytes", activation_bytes)


def load_activation_bytes(service_name: str = "bookbot") -> str | None:
    """
    Loads activation bytes from the system's keyring.
    """
    return keyring.get_password(service_name, "activation_bytes")


def delete_activation_bytes(service_name: str = "bookbot") -> None:
    """
    Deletes activation bytes from the system's keyring.
    """
    try:
        keyring.delete_password(service_name, "activation_bytes")
    except keyring.errors.PasswordDeleteError:
        pass
