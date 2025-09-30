"""Audible authentication client using browser-based cookie authentication."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from .audible_browser_auth import AudibleBrowserAuth


class AudibleAuthClient:
    """Client for authenticating with Audible using browser-based authentication (OpenAudible approach)."""

    def __init__(self, country_code: str = "US") -> None:
        self.country_code = country_code
        self._browser_auth = AudibleBrowserAuth(country_code=country_code)
        self._session: Optional[requests.Session] = None

    def authenticate(self, headless: bool = False) -> bool:
        """
        Perform browser-based authentication with Audible.

        Args:
            headless: Run browser in headless mode (not recommended for login)

        Returns:
            True if authentication succeeded, False otherwise
        """
        return self._browser_auth.authenticate(headless=headless)

    def _get_session(self) -> requests.Session:
        """Get or create HTTP session with cookies."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })
            # Load cookies
            cookie_dict = self._browser_auth.get_cookies_dict()
            for name, value in cookie_dict.items():
                self._session.cookies.set(name, value, domain=f".{self._browser_auth.base_domain}")
        return self._session

    def get_library(self) -> List[Dict[str, Any]]:
        """Get user's Audible library by scraping the library page."""
        if not self._browser_auth.is_authenticated():
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            session = self._get_session()

            # Try Audible API endpoint (may require additional auth)
            # For now, we'll scrape the library HTML page
            library_url = f"https://www.{self._browser_auth.base_domain}/library/titles"

            response = session.get(library_url, timeout=30)
            response.raise_for_status()

            # Parse library page
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")

            # Extract book items
            books = []
            for item in soup.select("[data-asin]"):
                asin = item.get("data-asin")
                if not asin:
                    continue

                title_tag = item.select_one("h3, [class*='title']")
                title = title_tag.get_text(strip=True) if title_tag else "Unknown"

                author_tag = item.select_one("[class*='author']")
                author = author_tag.get_text(strip=True) if author_tag else "Unknown"

                books.append({
                    "asin": asin,
                    "title": title,
                    "authors": [{"name": author}],
                })

            return books

        except Exception as e:
            raise Exception(f"Failed to get library: {e}") from e

    def download_book(self, asin: str, output_path: str, quality: str = "Extreme") -> bool:
        """
        Download an Audible book using authenticated session.

        Args:
            asin: Audible ASIN of the book
            output_path: Where to save the downloaded file
            quality: Audio quality (Extreme, High, Normal, Low)

        Returns:
            True if download succeeded, False otherwise
        """
        if not self._browser_auth.is_authenticated():
            raise Exception("Not authenticated. Call authenticate() first.")

        try:
            session = self._get_session()

            # Get download info from library page
            product_url = f"https://www.{self._browser_auth.base_domain}/library/titles/{asin}"
            response = session.get(product_url, timeout=30)
            response.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")

            # Look for download link/button
            download_link = soup.select_one('a[href*="download"], button[class*="download"]')

            if not download_link:
                raise Exception("Could not find download link for this book")

            # Extract actual download URL (this may require additional parsing)
            # Audible's download system may require additional authentication steps
            raise NotImplementedError(
                "Direct book download requires additional implementation. "
                "Consider using audible-cli or similar tools for book downloads."
            )

        except Exception as e:
            raise Exception(f"Failed to download book: {e}") from e

    def get_activation_bytes(self) -> Optional[str]:
        """
        Get activation bytes for DRM removal.

        Note: Activation bytes require additional implementation.
        See: https://github.com/inAudible-NG/audible-activator
        """
        # Activation bytes extraction requires additional work
        # and may need to use the audible-activator approach
        return None

    def get_cookies(self) -> Dict[str, str]:
        """Get authentication cookies as a dictionary."""
        return self._browser_auth.get_cookies_dict()

    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._browser_auth.is_authenticated()

    def logout(self) -> None:
        """Clear stored authentication."""
        self._browser_auth.logout()
        if self._session:
            self._session.close()
            self._session = None
