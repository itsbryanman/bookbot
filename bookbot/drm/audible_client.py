"""Audible authentication client using browser-based cookie authentication."""

from typing import Any

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
        self._session: requests.Session | None = None

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

    def get_library(self) -> list[dict[str, Any]]:
        """Get user's Audible library by scraping with Playwright."""
        # Make sure cookies are loaded
        if not self._browser_auth.cookies:
            if not self._browser_auth._load_cookies():
                raise Exception("Not authenticated. Call authenticate() first.")

        try:
            import time

            from playwright.sync_api import sync_playwright

            books = []

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )

                # Load cookies into context
                cookies = self._browser_auth.cookies
                context.add_cookies(cookies)

                page = context.new_page()

                # Navigate to library
                library_url = f"https://www.{self._browser_auth.base_domain}/library/titles"
                page.goto(library_url, wait_until="domcontentloaded")

                # Wait for JavaScript to load books
                page.wait_for_selector('a[href*="/pd/"]', timeout=10000)
                time.sleep(2)

                # Get page HTML
                html = page.content()
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')

                # Find all titles (they have the ASIN in a nearby link)
                seen_asins = set()
                for title_span in soup.find_all('span', class_=lambda x: x and 'bc-size-headline3' in x):
                    title = title_span.get_text(strip=True)
                    if not title:
                        continue

                    # Find the container this title is in
                    container = title_span.find_parent('li')
                    if not container:
                        container = title_span.find_parent('div')
                    if not container:
                        continue

                    # Find ASIN from /pd/ link in same container
                    asin = None
                    for link in container.find_all('a', href=True):
                        href = link['href']
                        if '/pd/' in href:
                            parts = href.split('/')
                            for part in reversed(parts):
                                part = part.split('?')[0]
                                if part and len(part) == 10 and part[0] in 'B0123456789':
                                    asin = part
                                    break
                        if asin:
                            break

                    if not asin or asin in seen_asins:
                        continue

                    seen_asins.add(asin)

                    # Find author - search more broadly if not found in container
                    author = "Unknown"
                    author_link = container.find('a', href=lambda x: x and '/author/' in x)
                    if not author_link:
                        # Try searching in parent container
                        parent = container.find_parent()
                        if parent:
                            author_link = parent.find('a', href=lambda x: x and '/author/' in x)
                    if author_link:
                        author = author_link.get_text(strip=True)

                    books.append({
                        "asin": asin,
                        "title": title,
                        "authors": [{"name": author}],
                    })

                browser.close()

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

    def get_activation_bytes(self) -> str | None:
        """
        Get activation bytes for DRM removal.

        Note: Activation bytes require additional implementation.
        See: https://github.com/inAudible-NG/audible-activator
        """
        # Activation bytes extraction requires additional work
        # and may need to use the audible-activator approach
        return None

    def get_cookies(self) -> dict[str, str]:
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

    def _load_stored_auth(self) -> bool:
        """
        Load stored authentication from cookies file.

        Returns:
            True if authenticated, False otherwise
        """
        # First load cookies from storage
        if not self._browser_auth._load_cookies():
            return False

        # Then verify they're valid
        return self._browser_auth.is_authenticated()
