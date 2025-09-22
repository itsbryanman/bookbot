"""Metadata providers for BookBot."""

from .audible import AudibleProvider
from .base import MetadataProvider
from .googlebooks import GoogleBooksProvider
from .librivox import LibriVoxProvider
from .manager import ProviderManager
from .openlibrary import OpenLibraryProvider

__all__ = [
    "MetadataProvider",
    "OpenLibraryProvider",
    "GoogleBooksProvider",
    "LibriVoxProvider",
    "AudibleProvider",
    "ProviderManager",
]
