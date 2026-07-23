"""BookBot - A cross-platform TUI audiobook renamer and organizer."""

__version__ = "1.0.0"
__author__ = "itsbryanman"
__email__ = "itsbryanman@users.noreply.github.com"


def main() -> None:
    """Main entry point for BookBot."""
    from .cli import main as cli_main

    cli_main()
