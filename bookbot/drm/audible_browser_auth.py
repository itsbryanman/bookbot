"""Browser-based Audible authentication using Playwright (OpenAudible approach)."""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

    HAS_PLAYWRIGHT = True
except ModuleNotFoundError:
    sync_playwright = None  # type: ignore[assignment]
    Browser = None  # type: ignore[assignment]
    Page = None  # type: ignore[assignment]
    BrowserContext = None  # type: ignore[assignment]
    HAS_PLAYWRIGHT = False

try:
    import keyring
except ModuleNotFoundError:
    keyring = None  # type: ignore[assignment]


def _ensure_playwright_browsers() -> bool:
    """
    Ensure Playwright browsers are installed.

    Returns:
        True if browsers are available, False otherwise
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        if result.returncode == 0:
            return True

    except subprocess.TimeoutExpired:
        return False

    # Browsers not found - install them
    print("\n📦 Installing Chromium browser for authentication...")
    print("⏳ This is a one-time setup and may take a minute...")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=False,
            timeout=300,
            check=False,
        )

        if result.returncode == 0:
            print("✅ Browser installation complete!")
            return True

        print(f"❌ Browser installation failed with code {result.returncode}")
        return False

    except subprocess.TimeoutExpired:
        print("❌ Browser installation timed out")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        print(f"❌ Browser installation error: {e}")
        return False


class AudibleBrowserAuth:
    """
    Audible authentication using embedded browser approach (like OpenAudible).

    Flow:
    1. Open real browser window for user to login manually
    2. Monitor for successful login (detect sign-out link)
    3. Extract cookies after login
    4. Save cookies for future use
    5. Use cookies with HTTP client for API calls
    """

    def __init__(self, country_code: str = "US") -> None:
        """Initialize browser authentication."""
        if not HAS_PLAYWRIGHT:
            raise ImportError(
                "playwright is required for browser authentication. "
                "Install with: pip install playwright && playwright install chromium"
            )

        self.country_code = country_code.upper()
        self.marketplace_domains = {
            "US": "audible.com",
            "UK": "audible.co.uk",
            "CA": "audible.ca",
            "AU": "audible.com.au",
            "FR": "audible.fr",
            "DE": "audible.de",
            "IT": "audible.it",
            "ES": "audible.es",
            "JP": "audible.co.jp",
            "IN": "audible.in",
        }
        self.base_domain = self.marketplace_domains.get(country_code, "audible.com")
        self.cookies: list[dict[str, Any]] = []

    def authenticate(self, headless: bool = False) -> bool:
        """
        Perform browser-based authentication.

        Args:
            headless: If True, run browser in headless mode (not recommended for login)

        Returns:
            True if authentication succeeded, False otherwise
        """
        # Check for existing valid cookies first
        if self._load_cookies():
            print("Found existing authentication, checking if valid...")
            if self._verify_cookies():
                print("✅ Existing authentication is valid!")
                return True
            else:
                print("⚠️ Existing authentication expired, re-authenticating...")

        # Ensure browsers are installed before attempting authentication
        if not _ensure_playwright_browsers():
            print("❌ Failed to install browser dependencies")
            return False

        print("\n🌐 Opening browser for Audible authentication...")
        print("📝 Please log in to your Audible account in the browser window")
        print("⏳ Waiting for you to complete login...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )

                try:
                    page = context.new_page()

                    audible_url = f"https://www.{self.base_domain}"
                    page.goto(audible_url, wait_until="domcontentloaded")

                    login_successful = self._wait_for_login(page)

                    if not login_successful:
                        print("❌ Login failed or timed out")
                        return False

                    self.cookies = context.cookies()
                    self._save_cookies()

                    print("✅ Authentication successful!")
                    print(f"✅ Saved {len(self.cookies)} cookies")

                    return True
                finally:
                    context.close()
                    browser.close()

        except (RuntimeError, ValueError, OSError) as e:
            print(f"❌ Authentication error: {e}")
            return False

    def _wait_for_login(self, page: "Page", timeout: int = 300) -> bool:
        """
        Wait for user to complete login by checking for logged-in indicators.

        Args:
            page: Playwright page object
            timeout: Maximum wait time in seconds

        Returns:
            True if login detected, False if timeout
        """
        start_time = time.time()
        check_interval = 2  # Check every 2 seconds

        while time.time() - start_time < timeout:
            try:
                # Check for logged-in indicators (sign-out link or account link)
                # OpenAudible checks for: sign-out link, account-details link

                # Try multiple selectors for sign-out/account
                selectors = [
                    'a[href*="signout"]',
                    'a[href*="/signOut"]',
                    'a[href*="account-details"]',
                    'a[href*="/account"]',
                ]

                for selector in selectors:
                    elements = page.locator(selector).all()
                    if len(elements) > 0:
                        print(f"✅ Login detected (found {selector})!")
                        time.sleep(2)  # Wait for cookies to settle
                        return True

                # Also check we're NOT on sign-in page
                sign_in_elements = page.locator('input[name="signIn"], a[href*="/sign-in"]').all()
                if len(sign_in_elements) == 0:
                    # No sign-in form, might be logged in
                    # Double-check with account indicator
                    account_indicators = page.locator(
                        'a[href*="account"], span:has-text("Hi,"), div:has-text("Hello,")'
                    ).all()
                    if len(account_indicators) > 0:
                        print("✅ Login detected (no sign-in form, account found)!")
                        time.sleep(2)
                        return True

                # Wait before next check
                time.sleep(check_interval)

            except Exception as e:
                print(f"⚠️ Error checking login status: {e}")
                time.sleep(check_interval)

        print("⏰ Login timeout - no login detected within timeout period")
        return False

    def _verify_cookies(self) -> bool:
        """
        Verify that stored cookies are still valid.

        Returns:
            True if cookies are valid, False otherwise
        """
        if not self.cookies:
            return False

        try:
            import requests

            # Try to access library page with cookies
            cookie_dict = {c["name"]: c["value"] for c in self.cookies}
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }

            # Check if we can access a logged-in-only page
            response = requests.get(
                f"https://www.{self.base_domain}/library/titles",
                cookies=cookie_dict,
                headers=headers,
                timeout=10,
                allow_redirects=False
            )

            # If we get redirected to login, cookies are invalid
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if "sign-in" in location.lower() or "signin" in location.lower():
                    return False

            # Check response for login indicators
            if response.status_code == 200:
                # Look for logged-out indicators
                if b"signIn" in response.content or b"sign-in" in response.content:
                    # Might be login form
                    return False
                return True

            return False

        except Exception as e:
            print(f"⚠️ Cookie verification failed: {e}")
            return False

    def _save_cookies(self) -> None:
        """Save cookies to persistent storage."""
        if not self.cookies:
            return

        cookie_data = {
            "marketplace": self.country_code,
            "domain": self.base_domain,
            "cookies": self.cookies,
            "timestamp": time.time(),
        }

        # Try keyring first
        if keyring is not None:
            try:
                keyring.set_password(
                    "bookbot",
                    f"audible_cookies_{self.country_code}",
                    json.dumps(cookie_data)
                )
                print("✅ Cookies saved securely to system keyring")
                return
            except Exception as e:
                print(f"⚠️ Keyring not available ({e}), using file storage")

        # Fallback: save to config directory
        try:
            config_dir = Path.home() / ".config" / "bookbot"
            config_dir.mkdir(parents=True, exist_ok=True)
            cookie_file = config_dir / f"audible_cookies_{self.country_code}.json"
            cookie_file.write_text(json.dumps(cookie_data, indent=2))
            cookie_file.chmod(0o600)  # Read/write for owner only
            print(f"✅ Cookies saved to {cookie_file}")
        except Exception as e:
            print(f"⚠️ Failed to save cookies: {e}")

    def _load_cookies(self) -> bool:
        """
        Load cookies from persistent storage.

        Returns:
            True if cookies loaded successfully, False otherwise
        """
        cookie_data_str: str | None = None

        # Try keyring first
        if keyring is not None:
            try:
                cookie_data_str = keyring.get_password(
                    "bookbot",
                    f"audible_cookies_{self.country_code}"
                )
            except Exception:
                pass

        # Try file fallback
        if not cookie_data_str:
            try:
                config_dir = Path.home() / ".config" / "bookbot"
                cookie_file = config_dir / f"audible_cookies_{self.country_code}.json"
                if cookie_file.exists():
                    cookie_data_str = cookie_file.read_text()
            except Exception:
                pass

        if not cookie_data_str:
            return False

        try:
            cookie_data = json.loads(cookie_data_str)
            self.cookies = cookie_data.get("cookies", [])

            # Check if cookies are too old (>30 days)
            saved_timestamp = cookie_data.get("timestamp", 0)
            age_days = (time.time() - saved_timestamp) / 86400
            if age_days > 30:
                print(f"⚠️ Cookies are {age_days:.0f} days old, may need refresh")

            return len(self.cookies) > 0

        except Exception as e:
            print(f"⚠️ Failed to load cookies: {e}")
            return False

    def get_cookies_dict(self) -> dict[str, str]:
        """
        Get cookies as a simple dict for use with requests.

        Returns:
            Dictionary mapping cookie names to values
        """
        return {cookie["name"]: cookie["value"] for cookie in self.cookies}

    def get_cookies_for_domain(self, domain: str | None = None) -> list[dict[str, Any]]:
        """
        Get cookies filtered by domain.

        Args:
            domain: Domain to filter by (defaults to marketplace domain)

        Returns:
            List of cookie dictionaries
        """
        if domain is None:
            domain = self.base_domain

        return [
            c for c in self.cookies
            if domain in c.get("domain", "")
        ]

    def logout(self) -> None:
        """Clear stored cookies and authentication."""
        # Clear keyring
        if keyring is not None:
            try:
                keyring.delete_password("bookbot", f"audible_cookies_{self.country_code}")
            except Exception:
                pass

        # Clear file
        try:
            config_dir = Path.home() / ".config" / "bookbot"
            cookie_file = config_dir / f"audible_cookies_{self.country_code}.json"
            if cookie_file.exists():
                cookie_file.unlink()
        except Exception:
            pass

        self.cookies = []
        print("✅ Logged out and cleared cookies")

    def is_authenticated(self) -> bool:
        """
        Check if we have valid authentication.

        Returns:
            True if authenticated, False otherwise
        """
        if not self.cookies:
            return False
        return self._verify_cookies()
