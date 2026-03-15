"""Audiobookshelf API client."""

from typing import Any

import aiohttp

from ..core.logging import get_logger

logger = get_logger("abs_client")


class AudiobookshelfClient:
    """HTTP client for Audiobookshelf server API."""

    def __init__(self, server_url: str, api_token: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_token = api_token
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        """Make an API request."""
        session = await self._get_session()
        url = f"{self.server_url}{path}"
        logger.debug(f"ABS API request: {method} {url}")

        try:
            async with session.request(
                method, url, params=params, json=json_data
            ) as response:
                if response.status == 404:
                    return None
                if response.status >= 400:
                    logger.warning(f"ABS returned {response.status} for {path}")
                    return None
                if response.content_type and "json" in response.content_type:
                    return await response.json()
                return None
        except (aiohttp.ClientError, Exception) as e:
            logger.warning(f"ABS request failed: {e}")
            return None

    @classmethod
    async def login(
        cls, server_url: str, username: str, password: str
    ) -> str | None:
        """Authenticate and return a token."""
        url = f"{server_url.rstrip('/')}/login"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json={"username": username, "password": password}
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
                    user = data.get("user", {})
                    return user.get("token")
        except (aiohttp.ClientError, Exception):
            return None

    async def get_libraries(self) -> list[dict[str, Any]]:
        """Get all libraries."""
        data = await self._request("GET", "/api/libraries")
        if isinstance(data, dict):
            return data.get("libraries", [])
        if isinstance(data, list):
            return data
        return []

    async def get_library_items(
        self,
        library_id: str,
        limit: int = 20,
        page: int = 0,
        sort: str | None = None,
        filter_str: str | None = None,
    ) -> dict[str, Any]:
        """Get items from a library."""
        params: dict[str, str] = {
            "limit": str(limit),
            "page": str(page),
        }
        if sort:
            params["sort"] = sort
        if filter_str:
            params["filter"] = filter_str

        data = await self._request(
            "GET", f"/api/libraries/{library_id}/items", params=params
        )
        return data if isinstance(data, dict) else {}

    async def search_library(
        self, library_id: str, query: str, limit: int = 10
    ) -> dict[str, Any]:
        """Search a library."""
        params = {"q": query, "limit": str(limit)}
        data = await self._request(
            "GET", f"/api/libraries/{library_id}/search", params=params
        )
        return data if isinstance(data, dict) else {}

    async def get_item(
        self, item_id: str, expanded: bool = True
    ) -> dict[str, Any] | None:
        """Get full item details."""
        params = {"expanded": "1"} if expanded else {}
        data = await self._request("GET", f"/api/items/{item_id}", params=params)
        return data if isinstance(data, dict) else None

    async def update_item_metadata(
        self, item_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update item metadata."""
        data = await self._request(
            "PATCH", f"/api/items/{item_id}/media", json_data=metadata
        )
        return data if isinstance(data, dict) else None

    async def match_item(
        self,
        item_id: str,
        provider: str = "audnexus",
        title: str | None = None,
        author: str | None = None,
    ) -> dict[str, Any] | None:
        """Trigger metadata match for an item."""
        body: dict[str, str] = {"provider": provider}
        if title:
            body["title"] = title
        if author:
            body["author"] = author
        data = await self._request(
            "POST", f"/api/items/{item_id}/match", json_data=body
        )
        return data if isinstance(data, dict) else None

    async def batch_match(self, library_id: str) -> dict[str, Any] | None:
        """Batch match all items in a library."""
        data = await self._request(
            "POST", f"/api/libraries/{library_id}/match-all"
        )
        return data if isinstance(data, dict) else None

    async def get_progress(self, item_id: str) -> dict[str, Any] | None:
        """Get playback progress for an item."""
        data = await self._request("GET", f"/api/me/progress/{item_id}")
        return data if isinstance(data, dict) else None

    async def update_progress(
        self,
        item_id: str,
        progress: float,
        current_time: float,
        is_finished: bool = False,
    ) -> dict[str, Any] | None:
        """Update playback progress for an item."""
        body = {
            "progress": progress,
            "currentTime": current_time,
            "isFinished": is_finished,
        }
        data = await self._request(
            "PATCH", f"/api/me/progress/{item_id}", json_data=body
        )
        return data if isinstance(data, dict) else None

    async def get_collections(self, library_id: str) -> list[dict[str, Any]]:
        """Get collections for a library."""
        data = await self._request("GET", "/api/collections")
        if isinstance(data, dict):
            return data.get("collections", [])
        if isinstance(data, list):
            return data
        return []

    async def create_collection(
        self, library_id: str, name: str, book_ids: list[str]
    ) -> dict[str, Any] | None:
        """Create a new collection."""
        body = {
            "libraryId": library_id,
            "name": name,
            "books": book_ids,
        }
        data = await self._request("POST", "/api/collections", json_data=body)
        return data if isinstance(data, dict) else None

    async def get_stats(self) -> dict[str, Any] | None:
        """Get listening statistics."""
        data = await self._request("GET", "/api/me/listening-stats")
        return data if isinstance(data, dict) else None
