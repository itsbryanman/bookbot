"""Provider manager for handling multiple metadata sources."""

from ..config.manager import ConfigManager
from ..core.logging import get_logger
from ..io.cache import CacheManager
from .audible import AudibleProvider
from .audnexus import AudnexusProvider
from .base import MetadataProvider
from .googlebooks import GoogleBooksProvider
from .hardcover import HardcoverProvider
from .librivox import LibriVoxProvider
from .openlibrary import OpenLibraryProvider

logger = get_logger("provider_manager")


class ProviderManager:
    """Manages multiple metadata providers."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.cache_manager = CacheManager(config_manager)
        self.providers: dict[str, MetadataProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize providers with Open Library FIRST (always enabled)."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        # ALWAYS initialize Open Library first - no API key needed
        self.providers["openlibrary"] = OpenLibraryProvider(
            cache_manager=self.cache_manager
        )
        logger.info("Initialized Open Library provider (default, always enabled)")

        # Audnexus - no API key needed, enabled by default
        if provider_config.audnexus.enabled:
            marketplace = provider_config.audnexus.marketplace
            self.providers["audnexus"] = AudnexusProvider(
                marketplace=marketplace,
                cache_manager=self.cache_manager,
            )
            logger.info(f"Initialized Audnexus provider (marketplace: {marketplace})")

        # Optional providers only if configured
        google_books_config = provider_config.google_books
        if google_books_config.enabled and google_books_config.api_key:
            self.providers["googlebooks"] = GoogleBooksProvider(
                api_key=google_books_config.api_key,
                cache_manager=self.cache_manager,
            )
            logger.info("Initialized Google Books provider")

        if provider_config.librivox.enabled:
            self.providers["librivox"] = LibriVoxProvider(
                cache_manager=self.cache_manager
            )
            logger.info("Initialized LibriVox provider")

        if provider_config.audible.enabled:
            marketplace = provider_config.audible.marketplace
            self.providers["audible"] = AudibleProvider(
                marketplace=marketplace,
                cache_manager=self.cache_manager,
            )
            logger.info(f"Initialized Audible provider (marketplace: {marketplace})")

        # Hardcover - requires API key
        hardcover_config = provider_config.hardcover
        if hardcover_config.enabled and hardcover_config.api_key:
            self.providers["hardcover"] = HardcoverProvider(
                api_key=hardcover_config.api_key,
                cache_manager=self.cache_manager,
            )
            logger.info("Initialized Hardcover provider")

    def get_enabled_providers(self) -> list[MetadataProvider]:
        """Get providers with Open Library ALWAYS first."""
        # Open Library is always first
        enabled = [self.providers["openlibrary"]]

        config = self.config_manager.load_config()

        # Add others based on priority order in config
        for provider_name in config.providers.priority_order:
            if provider_name == "openlibrary":
                continue  # Already added
            if provider_name in self.providers:
                enabled.append(self.providers[provider_name])

        return enabled

    def get_provider(self, name: str) -> MetadataProvider | None:
        """Get a specific provider by name."""
        return self.providers.get(name.lower())

    def get_primary_provider(self) -> MetadataProvider:
        """Get the primary (highest priority) provider."""
        enabled = self.get_enabled_providers()
        return enabled[0] if enabled else self.providers["openlibrary"]

    async def close_all(self) -> None:
        """Close all provider connections."""
        for provider in self.providers.values():
            if hasattr(provider, "close"):
                await provider.close()

    def list_providers(self) -> dict[str, dict[str, object]]:
        """List all available providers with their status."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        providers_info: dict[str, dict[str, object]] = {
            "openlibrary": {
                "name": "Open Library",
                "status": "enabled" if "openlibrary" in self.providers else "disabled",
                "description": "Free, comprehensive book database",
                "requires_api_key": "no",
            },
            "audnexus": {
                "name": "Audnexus",
                "status": "enabled" if "audnexus" in self.providers else "disabled",
                "description": "Audiobook metadata with chapter data (ASIN-based)",
                "requires_api_key": "no",
                "marketplace": provider_config.audnexus.marketplace,
            },
            "googlebooks": {
                "name": "Google Books",
                "status": "enabled" if "googlebooks" in self.providers else "disabled",
                "description": "Google's extensive book catalog",
                "requires_api_key": "yes",
                "api_key_provided": bool(provider_config.google_books.api_key),
            },
            "librivox": {
                "name": "LibriVox",
                "status": "enabled" if "librivox" in self.providers else "disabled",
                "description": "Public domain audiobooks",
                "requires_api_key": "no",
            },
            "audible": {
                "name": "Audible",
                "status": "enabled" if "audible" in self.providers else "disabled",
                "description": "Audible audiobook metadata",
                "requires_api_key": "no",
                "marketplace": provider_config.audible.marketplace,
            },
            "hardcover": {
                "name": "Hardcover",
                "status": "enabled" if "hardcover" in self.providers else "disabled",
                "description": "Hardcover book database (GraphQL API)",
                "requires_api_key": "yes",
                "api_key_provided": bool(provider_config.hardcover.api_key),
            },
        }

        return providers_info
