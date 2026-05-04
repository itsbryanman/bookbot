"""Compatibility shim for importing BookBot from the repository parent.

The source tree nests the real package under ``bookbot/bookbot``. When Python's
import path starts at the repository parent, this outer directory is discovered
first and would otherwise become an empty namespace package. Exposing the inner
package directory here keeps imports like ``bookbot.cli`` working in both
contexts.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.3.0"
__author__ = "itsbryanman"
__email__ = "itsbryanman@users.noreply.github.com"

_PACKAGE_ROOT = Path(__file__).resolve().parent
_INNER_PACKAGE = _PACKAGE_ROOT / "bookbot"

# Search both locations so repository-root imports resolve to the inner package
# without breaking explicit ``bookbot.bookbot`` references.
__path__ = [str(_PACKAGE_ROOT), str(_INNER_PACKAGE)]


def main() -> None:
    """Main entry point for BookBot."""
    from .cli import main as cli_main

    cli_main()
