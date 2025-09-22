"""GUI entry point for BookBot."""

import sys


def main() -> None:
    """Launch the GUI version of BookBot."""
    try:
        from .tui.app import BookBotApp
        from .config.manager import ConfigManager

        # For now, launch TUI as GUI - can be enhanced later
        config_manager = ConfigManager()
        app = BookBotApp(config_manager, [])
        app.run()

    except ImportError:
        print("Error: GUI dependencies not installed. Using TUI mode.")
        from .cli import main as cli_main
        cli_main()
    except Exception as e:
        print(f"Error launching GUI: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()