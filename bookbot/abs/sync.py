"""Progress synchronization daemon for Audiobookshelf."""

import asyncio
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config.manager import get_runtime_config_dir
from ..core.logging import get_logger
from .client import AudiobookshelfClient

logger = get_logger("abs_sync")

DEFAULT_DB_PATH = get_runtime_config_dir() / "progress.db"


class SyncReport(BaseModel):
    """Report from a sync operation."""

    pulled: int = 0
    pushed: int = 0
    conflicts: int = 0
    errors: list[str] = Field(default_factory=list)


class ProgressSyncDaemon:
    """Synchronizes playback progress between local state and ABS server."""

    def __init__(
        self,
        client: AudiobookshelfClient,
        state_path: Path | None = None,
    ) -> None:
        self.client = client
        self.state_path = state_path or DEFAULT_DB_PATH
        self._init_database()

    def _init_database(self) -> None:
        """Initialize the local SQLite progress database."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    item_id TEXT PRIMARY KEY,
                    progress REAL NOT NULL DEFAULT 0.0,
                    current_time REAL NOT NULL DEFAULT 0.0,
                    is_finished INTEGER NOT NULL DEFAULT 0,
                    last_update TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                )
            """)

    async def sync_from_server(self) -> list[dict[str, Any]]:
        """Pull all progress from ABS and update local DB."""
        libraries = await self.client.get_libraries()
        updated = []

        for lib in libraries:
            lib_id = lib.get("id", "")
            if not lib_id:
                continue

            items_data = await self.client.get_library_items(lib_id, limit=500)
            results = items_data.get("results", [])

            for item in results:
                item_id = item.get("id", "")
                if not item_id:
                    continue

                server_progress = await self.client.get_progress(item_id)
                if not server_progress:
                    continue

                progress_val = server_progress.get("progress", 0.0)
                current_time_val = server_progress.get("currentTime", 0.0)
                is_finished = server_progress.get("isFinished", False)
                last_update = server_progress.get(
                    "lastUpdate",
                    datetime.now().isoformat(),
                )

                local = self.get_local_progress(item_id)
                should_update = True

                if local:
                    local_update = local.get("last_update", "")
                    if isinstance(last_update, (int, float)):
                        server_ts = last_update
                    else:
                        server_ts = datetime.fromisoformat(
                            str(last_update)
                        ).timestamp()

                    try:
                        local_ts = datetime.fromisoformat(local_update).timestamp()
                    except (ValueError, TypeError):
                        local_ts = 0.0

                    should_update = server_ts > local_ts

                if should_update:
                    self._upsert_progress(
                        item_id,
                        progress_val,
                        current_time_val,
                        1 if is_finished else 0,
                        str(last_update) if not isinstance(last_update, str)
                        else last_update,
                        synced=1,
                    )
                    updated.append({
                        "item_id": item_id,
                        "progress": progress_val,
                        "current_time": current_time_val,
                    })

        return updated

    async def sync_to_server(
        self, item_id: str, progress: float, current_time: float
    ) -> bool:
        """Push local progress to ABS server."""
        result = await self.client.update_progress(
            item_id, progress, current_time
        )
        if result is not None:
            self._mark_synced(item_id)
            return True
        return False

    async def sync_all(self) -> SyncReport:
        """Bidirectional sync: pull from server, then push unsynced local changes."""
        report = SyncReport()

        # Pull first
        try:
            pulled = await self.sync_from_server()
            report.pulled = len(pulled)
        except Exception as e:
            report.errors.append(f"Pull failed: {e}")

        # Push unsynced
        unsynced = self._get_unsynced()
        for entry in unsynced:
            try:
                success = await self.sync_to_server(
                    entry["item_id"],
                    entry["progress"],
                    entry["current_time"],
                )
                if success:
                    report.pushed += 1
                else:
                    report.errors.append(
                        f"Failed to push progress for {entry['item_id']}"
                    )
            except Exception as e:
                report.errors.append(f"Push failed for {entry['item_id']}: {e}")

        return report

    def get_local_progress(self, item_id: str) -> dict[str, Any] | None:
        """Read progress from local DB."""
        with sqlite3.connect(self.state_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM progress WHERE item_id = ?", (item_id,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def update_local_progress(
        self, item_id: str, progress: float, current_time: float
    ) -> None:
        """Update local progress (marks as unsynced)."""
        self._upsert_progress(
            item_id,
            progress,
            current_time,
            1 if progress >= 1.0 else 0,
            datetime.now().isoformat(),
            synced=0,
        )

    def _upsert_progress(
        self,
        item_id: str,
        progress: float,
        current_time: float,
        is_finished: int,
        last_update: str,
        synced: int = 0,
    ) -> None:
        """Insert or update a progress entry."""
        with sqlite3.connect(self.state_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO progress
                    (item_id, progress, current_time, is_finished, last_update, synced)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, progress, current_time, is_finished, last_update, synced),
            )

    def _mark_synced(self, item_id: str) -> None:
        """Mark an entry as synced."""
        with sqlite3.connect(self.state_path) as conn:
            conn.execute(
                "UPDATE progress SET synced = 1 WHERE item_id = ?", (item_id,)
            )

    def _get_unsynced(self) -> list[dict[str, Any]]:
        """Get all unsynced progress entries."""
        with sqlite3.connect(self.state_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM progress WHERE synced = 0")
            return [dict(row) for row in cursor.fetchall()]
