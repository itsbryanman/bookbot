"""BookBot - A cross-platform TUI audiobook renamer and organizer."""

__version__ = "0.2.0"
__author__ = "BookBot"
__email__ = "bookbot@example.com"


def main() -> None:
    """Main entry point for BookBot."""
    from bookbot.cli import main as cli_main
    cli_main()
