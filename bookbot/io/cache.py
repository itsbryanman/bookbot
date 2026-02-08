"""Cache management for API responses."""

import json
import sqlite3
import time
from typing import Any

from ..config.manager import ConfigManager


class CacheManager:
    """Manages local cache for API responses."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.cache_dir = config_manager.get_cache_dir()
        self.db_path = self.cache_dir / "bookbot_cache.db"
        self._init_database()

    def _init_database(self) -> None:
        """Initialize the SQLite cache database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    response_data TEXT NOT NULL,
                    cached_at REAL NOT NULL,
                    expires_at REAL,
                    etag TEXT,
                    last_modified TEXT
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_provider_query
                ON api_cache (provider, query_hash)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires_at
                ON api_cache (expires_at)
            """)

    def get(self, provider: str, query_hash: str) -> dict[str, Any] | None:
        """Get cached response for a query."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT response_data, cached_at, expires_at, etag, last_modified
                FROM api_cache
                WHERE provider = ? AND query_hash = ?
            """,
                (provider, query_hash),
            )

            row = cursor.fetchone()
            if not row:
                return None

            # Check if cache entry has expired
            current_time = time.time()
            if row["expires_at"] and current_time > row["expires_at"]:
                # Entry has expired, remove it
                self._delete_entry(provider, query_hash)
                return None

            try:
                response_data = json.loads(row["response_data"])
                return {
                    "data": response_data,
                    "cached_at": row["cached_at"],
                    "etag": row["etag"],
                    "last_modified": row["last_modified"],
                }
            except json.JSONDecodeError:
                # Corrupted cache entry, remove it
                self._delete_entry(provider, query_hash)
                return None

    def set(
        self,
        provider: str,
        query_hash: str,
        response_data: Any,
        ttl_seconds: int | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Cache a response."""
        current_time = time.time()
        expires_at = current_time + ttl_seconds if ttl_seconds else None

        try:
            serialized_data = json.dumps(response_data)
        except (TypeError, ValueError):
            # Cannot serialize, skip caching
            return

        cache_key = f"{provider}:{query_hash}"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_cache (
                    key, provider, query_hash, response_data, cached_at,
                    expires_at, etag, last_modified
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    cache_key,
                    provider,
                    query_hash,
                    serialized_data,
                    current_time,
                    expires_at,
                    etag,
                    last_modified,
                ),
            )

    def _delete_entry(self, provider: str, query_hash: str) -> None:
        """Delete a specific cache entry."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM api_cache
                WHERE provider = ? AND query_hash = ?
            """,
                (provider, query_hash),
            )

    def clear_expired(self) -> int:
        """Clear expired cache entries and return count of removed entries."""
        current_time = time.time()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                DELETE FROM api_cache
                WHERE expires_at IS NOT NULL AND expires_at < ?
            """,
                (current_time,),
            )
            return cursor.rowcount

    def clear_provider(self, provider: str) -> int:
        """Clear all cache entries for a specific provider."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                DELETE FROM api_cache WHERE provider = ?
            """,
                (provider,),
            )
            return cursor.rowcount

    def clear_all(self) -> int:
        """Clear all cache entries."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM api_cache")
            return cursor.rowcount

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Total entries
            total_cursor = conn.execute("SELECT COUNT(*) as count FROM api_cache")
            total_count = total_cursor.fetchone()["count"]

            # Entries by provider
            provider_cursor = conn.execute("""
                SELECT provider, COUNT(*) as count
                FROM api_cache
                GROUP BY provider
            """)
            providers = {row["provider"]: row["count"] for row in provider_cursor}

            # Expired entries
            current_time = time.time()
            expired_cursor = conn.execute(
                """
                SELECT COUNT(*) as count FROM api_cache
                WHERE expires_at IS NOT NULL AND expires_at < ?
            """,
                (current_time,),
            )
            expired_count = expired_cursor.fetchone()["count"]

            # Cache size (approximate)
            size_cursor = conn.execute("""
                SELECT SUM(LENGTH(response_data)) as size FROM api_cache
            """)
            cache_size = size_cursor.fetchone()["size"] or 0

            return {
                "total_entries": total_count,
                "expired_entries": expired_count,
                "providers": providers,
                "cache_size_bytes": cache_size,
                "cache_size_mb": round(cache_size / (1024 * 1024), 2),
            }

    def generate_query_hash(self, **kwargs: Any) -> str:
        """Generate a hash for query parameters."""
        import hashlib

        # Sort parameters for consistent hashing
        sorted_params = sorted(kwargs.items())
        query_string = "&".join(f"{k}={v}" for k, v in sorted_params if v is not None)

        return hashlib.sha256(query_string.encode()).hexdigest()[:16]
