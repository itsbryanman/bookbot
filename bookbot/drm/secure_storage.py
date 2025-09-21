import keyring
import toml

from bookbot.core.models import Token


def save_token(token: Token, service_name: str = "bookbot") -> None:
    """
    Saves a token to the system's keyring.
    """
    keyring.set_password(service_name, "token", toml.dumps({"token": token.dict()}))


def load_token(service_name: str = "bookbot") -> Token | None:
    """
    Loads a token from the system's keyring.
    """
    stored_token = keyring.get_password(service_name, "token")
    if stored_token:
        return Token.parse_obj(toml.loads(stored_token)["token"])
    return None


def delete_token(service_name: str = "bookbot") -> None:
    """
    Deletes a token from the system's keyring.
    """
    try:
        keyring.delete_password(service_name, "token")
    except keyring.errors.PasswordNotFoundError:
        pass