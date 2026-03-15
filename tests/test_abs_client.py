"""Tests for the Audiobookshelf client."""

from unittest.mock import AsyncMock, patch

import pytest

from bookbot.abs.client import AudiobookshelfClient


@pytest.fixture
def client():
    return AudiobookshelfClient(
        server_url="https://abs.example.com",
        api_token="test-token-123",
    )


class TestAudiobookshelfClient:
    def test_init(self, client):
        assert client.server_url == "https://abs.example.com"
        assert client.api_token == "test-token-123"

    def test_server_url_trailing_slash_stripped(self):
        c = AudiobookshelfClient("https://abs.example.com/", "tok")
        assert c.server_url == "https://abs.example.com"

    @pytest.mark.asyncio
    async def test_close_no_session(self, client):
        await client.close()  # should not raise

    @pytest.mark.asyncio
    async def test_get_libraries(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {
                "libraries": [
                    {"id": "lib1", "name": "Audiobooks", "mediaType": "book"}
                ]
            }
            libs = await client.get_libraries()
        assert len(libs) == 1
        assert libs[0]["name"] == "Audiobooks"

    @pytest.mark.asyncio
    async def test_get_libraries_list_response(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = [
                {"id": "lib1", "name": "Books", "mediaType": "book"}
            ]
            libs = await client.get_libraries()
        assert len(libs) == 1

    @pytest.mark.asyncio
    async def test_get_libraries_empty(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = None
            libs = await client.get_libraries()
        assert libs == []

    @pytest.mark.asyncio
    async def test_search_library(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {
                "book": [{"libraryItem": {"id": "item1", "title": "Dune"}}]
            }
            results = await client.search_library("lib1", "dune")
        assert "book" in results

    @pytest.mark.asyncio
    async def test_get_item(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {
                "id": "item1",
                "media": {"metadata": {"title": "Dune"}},
            }
            item = await client.get_item("item1")
        assert item is not None
        assert item["id"] == "item1"

    @pytest.mark.asyncio
    async def test_get_item_not_found(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = None
            item = await client.get_item("nonexistent")
        assert item is None

    @pytest.mark.asyncio
    async def test_update_progress(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {"success": True}
            result = await client.update_progress("item1", 0.5, 1800.0)
        assert result is not None
        mock_req.assert_called_once_with(
            "PATCH",
            "/api/me/progress/item1",
            json_data={
                "progress": 0.5,
                "currentTime": 1800.0,
                "isFinished": False,
            },
        )

    @pytest.mark.asyncio
    async def test_match_item(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {"updated": True}
            result = await client.match_item("item1", provider="audnexus")
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_stats(self, client):
        with patch.object(
            client, "_request", new_callable=AsyncMock
        ) as mock_req:
            mock_req.return_value = {"totalTime": 360000}
            stats = await client.get_stats()
        assert stats is not None
        assert stats["totalTime"] == 360000
