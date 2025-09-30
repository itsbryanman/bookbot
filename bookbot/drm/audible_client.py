"""Audible authentication client using the official audible package."""

import json
import webbrowser
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

try:
    import audible
except ImportError:
    audible = None


class AudibleAuthClient:
    """Client for authenticating with Audible using browser-based OAuth flow."""

    def __init__(self, country_code: str = "US") -> None:
        if audible is None:
            raise ImportError(
                "The 'audible' package is required for Audible authentication. "
                "Install it with: pip install audible"
            )

        self.country_code = country_code
        self._auth: audible.Authenticator | None = None
        self._client: audible.Client | None = None

    def authenticate(self) -> bool:
        """
        Perform browser-based authentication with Audible.
        Returns True if authentication succeeded, False otherwise.
        """
        try:
            print("Starting Audible authentication...")
            print("This will open your web browser for login.")

            # Check if we already have stored authentication
            stored_auth = self._load_stored_auth()
            if stored_auth:
                print("Found existing authentication, checking if it's still valid...")
                try:
                    with audible.Client(stored_auth) as client:
                        client.get("library", num_results=1)
                    self._auth = stored_auth
                    print("✅ Existing authentication is valid!")
                    return True
                except Exception:
                    print("⚠️ Existing authentication is expired, re-authenticating...")

            # Perform new authentication
            def login_url_callback(login_url: str) -> str:
                print("\n🌐 Opening browser for authentication...")
                print(f"If the browser doesn't open automatically, visit: {login_url}")
                webbrowser.open(login_url)

                print(
                    "\n📝 After logging in, you'll see an error page - this is normal!"
                )
                print("📋 Copy the FULL URL from the address bar and paste it below.")
                print(
                    "📋 It should look like: https://www.amazon.com/ap/maplanding?..."
                )

                return input("\n🔗 Paste the redirect URL here: ").strip()

            # Create authenticator with browser-based login
            self._auth = audible.Authenticator.from_login_external(
                locale=self.country_code, login_url_callback=login_url_callback
            )

            # Save authentication for future use
            self._save_auth()

            print("✅ Authentication successful!")
            return True

        except Exception as e:
            print(f"❌ Authentication failed: {e}")
            return False

    def get_library(self) -> list[dict[str, Any]]:
        """Get user's Audible library."""
        if not self._auth:
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            with audible.Client(self._auth) as client:
                library = client.get(
                    "library",
                    num_results=999,
                    response_groups="product_desc,product_attrs,series,media,price",
                )
                return list(library.get("items", []))
        except Exception as e:
            raise Exception(f"Failed to get library: {e}") from e

    def download_book(self, asin: str, output_path: str) -> bool:
        """Download an Audible book."""
        if not self._auth:
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            with audible.Client(self._auth) as client:
                # Get download link
                content_url = client.get_download_link(asin)

                # Download the file
                import requests

                response = requests.get(content_url, stream=True)
                response.raise_for_status()

                output_file = Path(output_path)
                output_file.parent.mkdir(parents=True, exist_ok=True)

                with open(output_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                return True

        except Exception as e:
            raise Exception(f"Failed to download book: {e}") from e

    def get_activation_bytes(self) -> str | None:
        """Get activation bytes for DRM removal."""
        if not self._auth:
            return None

        try:
            # Extract activation bytes from the authentication
            if hasattr(self._auth, "activation_bytes"):
                return str(self._auth.activation_bytes)
            return None
        except Exception:
            return None

    def _save_auth(self) -> None:
        """Save authentication data securely."""
        if self._auth:
            auth_data = {
                "access_token": self._auth.access_token,
                "refresh_token": self._auth.refresh_token,
                "adp_token": self._auth.adp_token,
                "device_private_key": self._auth.device_private_key,
                "store_authentication_cookie": self._auth.store_authentication_cookie,
                "device_info": self._auth.device_info,
                "customer_info": self._auth.customer_info,
                "website_cookies": self._auth.website_cookies,
            }

            # Try to save with keyring first
            if keyring is not None:
                try:
                    keyring.set_password("bookbot", "audible_auth", json.dumps(auth_data))
                    print("✅ Authentication saved securely to system keyring")
                    return
                except Exception as e:
                    print(f"⚠️ Keyring not available ({e}), using fallback file storage")

            # Fallback: save to config directory
            try:
                config_dir = Path.home() / ".config" / "bookbot"
                config_dir.mkdir(parents=True, exist_ok=True)
                auth_file = config_dir / ".audible_auth.json"
                auth_file.write_text(json.dumps(auth_data))
                auth_file.chmod(0o600)  # Read/write for owner only
                print(f"✅ Authentication saved to {auth_file}")
            except Exception as e:
                print(f"⚠️ Failed to save authentication: {e}")
                print("Authentication will work for this session only")

    def _load_stored_auth(self) -> audible.Authenticator | None:
        """Load stored authentication data."""
        auth_data_str = None

        # Try keyring first
        if keyring is not None:
            try:
                auth_data_str = keyring.get_password("bookbot", "audible_auth")
            except Exception:
                pass  # Fallback to file

        # Try fallback file if keyring didn't work
        if not auth_data_str:
            try:
                config_dir = Path.home() / ".config" / "bookbot"
                auth_file = config_dir / ".audible_auth.json"
                if auth_file.exists():
                    auth_data_str = auth_file.read_text()
            except Exception:
                pass

        if not auth_data_str:
            return None

        try:
            auth_data = json.loads(auth_data_str)

            # Reconstruct the authenticator
            auth = audible.Authenticator(
                access_token=auth_data.get("access_token"),
                refresh_token=auth_data.get("refresh_token"),
                adp_token=auth_data.get("adp_token"),
                device_private_key=auth_data.get("device_private_key"),
                store_authentication_cookie=auth_data.get(
                    "store_authentication_cookie"
                ),
                device_info=auth_data.get("device_info"),
                customer_info=auth_data.get("customer_info"),
                website_cookies=auth_data.get("website_cookies"),
            )

            return auth

        except Exception:
            return None

    def logout(self) -> None:
        """Clear stored authentication."""
        # Try to delete from keyring
        if keyring is not None:
            try:
                keyring.delete_password("bookbot", "audible_auth")
            except Exception:
                pass

        # Try to delete fallback file
        try:
            config_dir = Path.home() / ".config" / "bookbot"
            auth_file = config_dir / ".audible_auth.json"
            if auth_file.exists():
                auth_file.unlink()
        except Exception:
            pass

        self._auth = None
        self._client = None

    @staticmethod
    def open_browser(url: str) -> None:
        """Open URL in browser."""
        webbrowser.open(url)
