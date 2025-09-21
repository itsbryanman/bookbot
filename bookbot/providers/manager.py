"""Provider manager for handling multiple metadata sources."""

from typing import Dict, List, Optional, Type

from ..config.manager import ConfigManager
from .base import MetadataProvider
from .openlibrary import OpenLibraryProvider
from .googlebooks import GoogleBooksProvider
from .librivox import LibriVoxProvider
from .audible import AudibleProvider


class ProviderManager:
    """Manages multiple metadata providers."""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.providers: Dict[str, MetadataProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize available providers based on configuration."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        # Always include OpenLibrary as it's free and reliable
        self.providers["openlibrary"] = OpenLibraryProvider()

        # Add Google Books if API key is provided
        if provider_config.google_books.enabled and provider_config.google_books.api_key:
            self.providers["googlebooks"] = GoogleBooksProvider(
                api_key=provider_config.google_books.api_key
            )

        # Add LibriVox if enabled (no API key required)
        if provider_config.librivox.enabled:
            self.providers["librivox"] = LibriVoxProvider()

        # Add Audible if enabled (no API key required, web scraping)
        if provider_config.audible.enabled:
            marketplace = provider_config.audible.marketplace
            self.providers["audible"] = AudibleProvider(marketplace=marketplace)

    def get_enabled_providers(self) -> List[MetadataProvider]:
        """Get list of enabled providers in priority order."""
        config = self.config_manager.load_config()
        provider_order = config.providers.priority_order

        enabled_providers = []
        for provider_name in provider_order:
            if provider_name in self.providers:
                enabled_providers.append(self.providers[provider_name])

        # Add any remaining providers not in the priority list
        for provider_name, provider in self.providers.items():
            if provider not in enabled_providers:
                enabled_providers.append(provider)

        return enabled_providers

    def get_provider(self, name: str) -> Optional[MetadataProvider]:
        """Get a specific provider by name."""
        return self.providers.get(name.lower())

    def get_primary_provider(self) -> MetadataProvider:
        """Get the primary (highest priority) provider."""
        enabled = self.get_enabled_providers()
        return enabled[0] if enabled else self.providers["openlibrary"]

    async def close_all(self) -> None:
        """Close all provider connections."""
        for provider in self.providers.values():
            if hasattr(provider, 'close'):
                await provider.close()

    def list_providers(self) -> Dict[str, Dict[str, str]]:
        """List all available providers with their status."""
        config = self.config_manager.load_config()
        provider_config = config.providers

        providers_info = {
            "openlibrary": {
                "name": "Open Library",
                "status": "enabled" if "openlibrary" in self.providers else "disabled",
                "description": "Free, comprehensive book database",
                "requires_api_key": "no"
            },
            "googlebooks": {
                "name": "Google Books",
                "status": "enabled" if "googlebooks" in self.providers else "disabled",
                "description": "Google's extensive book catalog",
                "requires_api_key": "yes",
                "api_key_provided": bool(provider_config.google_books.api_key)
            },
            "librivox": {
                "name": "LibriVox",
                "status": "enabled" if "librivox" in self.providers else "disabled",
                "description": "Public domain audiobooks",
                "requires_api_key": "no"
            },
            "audible": {
                "name": "Audible",
                "status": "enabled" if "audible" in self.providers else "disabled",
                "description": "Audible audiobook metadata",
                "requires_api_key": "no",
                "marketplace": provider_config.audible.marketplace
            }
        }

        return providers_info