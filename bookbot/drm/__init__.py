"""
DRM removal utilities for BookBot.
"""

from . import secure_storage
from .audible_client import AudibleAuthClient

__all__ = ["AudibleAuthClient", "secure_storage"]