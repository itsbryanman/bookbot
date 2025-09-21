"""Metadata providers for BookBot."""

from .base import MetadataProvider
from .openlibrary import OpenLibraryProvider
from .googlebooks import GoogleBooksProvider
from .librivox import LibriVoxProvider
from .audible import AudibleProvider
from .manager import ProviderManager

__all__ = [
    "MetadataProvider",
    "OpenLibraryProvider",
    "GoogleBooksProvider",
    "LibriVoxProvider",
    "AudibleProvider",
    "ProviderManager"
]