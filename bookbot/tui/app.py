"""Main TUI application for BookBot."""

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.widgets import Button, Footer, Header, Label, TabbedContent, TabPane

from ..config.manager import ConfigManager
from ..core.discovery import AudioFileScanner
from ..core.models import AudiobookSet
from ..providers.base import MetadataProvider
from ..providers.openlibrary import OpenLibraryProvider
from .screens import (
    ConversionScreen,
    DRMLoginScreen,
    LoginFailure,
    LoginSuccess,
    MatchReviewScreen,
    PreviewScreen,
    ScanResultsScreen,
    SourceSelectionScreen,
)


class BookBotApp(App):
    """Main BookBot TUI application."""

    CSS = """
    /* DOPE AF Styling - Textual Compatible */
    .title {
        text-align: center;
        text-style: bold;
        color: $text;
        background: $primary;
        padding: 1;
        margin: 1;
        border: heavy $accent;
    }

    .status {
        dock: bottom;
        height: 4;
        background: $primary;
        border: heavy $accent;
    }

    .main-container {
        height: 100%;
        background: $surface;
    }

    .scan-container {
        height: 100%;
    }

    /* Enhanced Buttons */
    Button {
        border: heavy $accent;
        background: $warning;
        color: $text;
        text-style: bold;
        margin: 0 1;
    }

    Button:hover {
        background: $error;
        border: heavy $success;
    }

    Button:focus {
        border: heavy $success;
        background: $success;
        color: $background;
    }

    Button.-primary {
        background: $accent;
        border: heavy $accent;
        color: $background;
    }

    Button.-primary:hover {
        background: $success;
        border: heavy $success;
    }

    Button:disabled {
        background: $surface;
        color: #666;
        border: solid #666;
    }

    /* Enhanced Tables */
    DataTable {
        border: heavy $accent;
        background: $surface;
    }

    DataTable > .datatable--header {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $accent 50%;
        color: $text;
    }

    DataTable:focus > .datatable--cursor {
        background: $accent;
        color: $background;
    }

    /* Enhanced Tabs */
    TabbedContent {
        border: heavy $accent;
        background: $surface;
    }

    Tab {
        background: $primary;
        color: $text;
        text-style: bold;
        border: solid $accent;
    }

    Tab.-active {
        background: $accent;
        border: heavy $success;
        color: $background;
        text-style: bold;
    }

    Tab:hover {
        background: $success;
        color: $background;
    }

    /* Section Styling */
    .section-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        background: $primary;
        padding: 1;
        border: solid $accent;
        margin: 1;
    }

    .subsection-title {
        text-style: bold;
        color: $warning;
        margin: 1 0;
    }

    /* Enhanced Input Fields */
    Input {
        border: solid $accent;
        background: $surface;
        color: $text;
    }

    Input:focus {
        border: heavy $success;
        background: $background;
    }

    /* Progress Bar Enhancement */
    ProgressBar {
        border: solid $accent;
        background: $surface;
    }

    ProgressBar > .bar--bar {
        background: $accent;
    }

    ProgressBar > .bar--complete {
        background: $success;
    }

    /* Status Label */
    #status_label {
        color: $warning;
        text-style: bold;
        margin: 1;
    }

    /* Checkbox Styling */
    Checkbox {
        color: $accent;
    }

    Checkbox:focus {
        background: $accent 20%;
    }

    /* Select Styling */
    Select {
        border: solid $accent;
        background: $surface;
        color: $text;
    }

    Select:focus {
        border: heavy $success;
        background: $background;
    }

    /* Container Enhancements */
    Container {
        border: none;
        background: transparent;
    }

    /* Footer Enhancement */
    Footer {
        background: $background;
        color: $accent;
    }

    /* Header Enhancement */
    Header {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    /* Enhanced Labels */
    Label {
        color: $text;
    }

    Static {
        color: $text;
    }

    /* Special Effect Classes */
    .success-text {
        color: $success;
        text-style: bold;
    }

    .warning-text {
        color: $warning;
        text-style: bold;
    }

    .error-text {
        color: $error;
        text-style: bold;
    }

    .highlight {
        background: $accent;
        color: $background;
        padding: 0 1;
    }

    .glow {
        border: heavy $accent;
        background: $accent 20%;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+h", "help", "Help", show=True),
        Binding("f1", "help", "Help", show=False),
        ("ctrl+s", "save_config", "Save Config"),
        ("ctrl+r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        config_manager: ConfigManager,
        source_folders: list[Path],
        provider: MetadataProvider | None = None,
    ):
        super().__init__()
        self.config_manager = config_manager
        self.source_folders = source_folders
        self.audiobook_sets: list[AudiobookSet] = []
        self.provider = provider or OpenLibraryProvider()

        # Application state
        self.current_step = "source_selection"
        self.scanning_complete = False

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()

        with Container(classes="main-container"):
            yield Label("BookBot - Ultimate Audiobook Organizer", classes="title")

            with TabbedContent(initial="source"):
                with TabPane("Source Selection", id="source"):
                    yield SourceSelectionScreen(
                        self.config_manager, self.source_folders, id="source_screen"
                    )

                with TabPane("Scan Results", id="scan"):
                    yield ScanResultsScreen(self.config_manager, id="scan_screen")

                with TabPane("Match Review", id="match"):
                    yield MatchReviewScreen(
                        self.config_manager, self.provider, id="match_screen"
                    )

                with TabPane("Preview", id="preview"):
                    yield PreviewScreen(self.config_manager, id="preview_screen")

                with TabPane("Convert", id="convert"):
                    yield ConversionScreen(self.config_manager, id="convert_screen")

                with TabPane("DRM Removal", id="drm_tab"):
                    yield DRMLoginScreen(id="drm_login_screen")

        with Container(classes="status"):
            yield Label("Ready", id="status_label")
            with Horizontal():
                yield Button("Start Scan", id="start_scan", variant="primary")
                yield Button("Find Matches", id="find_matches", disabled=True)
                yield Button("Preview Changes", id="preview_changes", disabled=True)
                yield Button("Apply Changes", id="apply_changes", disabled=True)
                yield Button("Convert to M4B", id="convert_m4b", disabled=True)

        yield Footer()

    async def on_mount(self) -> None:
        """Called when the app is mounted."""
        # Initialize default profiles if they don't exist
        self.config_manager.create_default_profiles()

        # Set initial status
        self.update_status("Select folders and start scanning")

        # If folders were provided, start scanning automatically
        if self.source_folders:
            self.post_message(self.StartScan())

    async def on_drm_login_screen_login_success(self, message: LoginSuccess) -> None:
        """Handle successful DRM login."""
        self.update_status("Successfully logged into Audible.")
        # Optionally, switch to a different screen or enable DRM-related features
        # For now, just update the status
        self.query_one(TabbedContent).active = "drm_tab"

    async def on_drm_login_screen_login_failure(self, message: LoginFailure) -> None:
        """Handle failed DRM login."""
        self.update_status(f"Audible login failed: {message.error}")
        self.query_one(TabbedContent).active = "drm_tab"

    def update_status(self, message: str) -> None:
        """Update the status label."""
        status_label = self.query_one("#status_label", Label)
        status_label.update(message)

    class StartScan(Message):
        """Message to start scanning."""

        pass

    class ScanComplete(Message):
        """Message when scanning is complete."""

        def __init__(self, audiobook_sets: list[AudiobookSet]):
            super().__init__()
            self.audiobook_sets = audiobook_sets

    class MatchesFound(Message):
        """Message when matches are found."""

        pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "start_scan":
            self.post_message(self.StartScan())
        elif event.button.id == "find_matches":
            await self.find_matches()
        elif event.button.id == "preview_changes":
            self.show_preview()
        elif event.button.id == "apply_changes":
            await self.apply_changes()
        elif event.button.id == "convert_m4b":
            self.show_conversion()

    async def on_start_scan(self, event: StartScan) -> None:
        """Handle start scan message."""
        if not self.source_folders:
            self.update_status("Error: No folders selected")
            return

        # Disable scan button
        scan_button = self.query_one("#start_scan", Button)
        scan_button.disabled = True
        scan_button.label = "Scanning..."

        self.update_status("Scanning for audiobooks...")

        try:
            scanner_tasks = []
            for folder in self.source_folders:
                self.update_status(f"Scanning {folder.name}...")
                scanner_tasks.append(
                    asyncio.to_thread(
                        AudioFileScanner(recursive=True, max_depth=5).scan_directory,
                        folder,
                    )
                )

            results = await asyncio.gather(*scanner_tasks, return_exceptions=True)

            all_audiobook_sets: list[AudiobookSet] = []
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                all_audiobook_sets.extend(result)

            self.audiobook_sets = all_audiobook_sets
            self.scanning_complete = True

            # Update UI
            scan_screen = self.query_one("#scan_screen", ScanResultsScreen)
            scan_screen.set_audiobook_sets(self.audiobook_sets)

            # Enable next step
            find_button = self.query_one("#find_matches", Button)
            find_button.disabled = False

            # Switch to scan results tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "scan"

            self.update_status(
                f"Scan complete: Found {len(self.audiobook_sets)} audiobook set(s)"
            )

        except Exception as e:
            self.update_status(f"Scan failed: {e}")

        finally:
            # Re-enable scan button
            scan_button.disabled = False
            scan_button.label = "Start Scan"

    async def find_matches(self) -> None:
        """Find metadata matches for audiobooks."""
        if not self.audiobook_sets:
            self.update_status("No audiobooks to match")
            return

        find_button = self.query_one("#find_matches", Button)
        find_button.disabled = True
        find_button.label = "Finding Matches..."

        self.update_status("Finding metadata matches...")

        try:
            match_screen = self.query_one("#match_screen", MatchReviewScreen)
            await match_screen.find_matches(self.audiobook_sets)

            # Enable next step
            preview_button = self.query_one("#preview_changes", Button)
            preview_button.disabled = False

            # Switch to match review tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "match"

            self.update_status("Matches found - review and confirm")

        except Exception as e:
            self.update_status(f"Match finding failed: {e}")

        finally:
            find_button.disabled = False
            find_button.label = "Find Matches"

    def show_preview(self) -> None:
        """Show preview of changes."""
        preview_screen = self.query_one("#preview_screen", PreviewScreen)
        preview_screen.set_audiobook_sets(self.audiobook_sets)

        # Enable apply button
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.disabled = False

        # Switch to preview tab
        tabbed_content = self.query_one(TabbedContent)
        tabbed_content.active = "preview"

        self.update_status("Review changes and apply when ready")

    async def apply_changes(self) -> None:
        """Apply the changes."""
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.disabled = True
        apply_button.label = "Applying..."

        self.update_status("Applying changes...")

        try:
            preview_screen = self.query_one("#preview_screen", PreviewScreen)
            success = await preview_screen.apply_changes()

            if success:
                self.update_status("Changes applied successfully!")
                # Enable conversion button after successful changes
                convert_button = self.query_one("#convert_m4b", Button)
                convert_button.disabled = False
            else:
                self.update_status("Failed to apply changes")

        except Exception as e:
            self.update_status(f"Error applying changes: {e}")

        finally:
            apply_button.disabled = False
            apply_button.label = "Apply Changes"

    def show_conversion(self) -> None:
        """Show M4B conversion screen."""
        convert_screen = self.query_one("#convert_screen", ConversionScreen)
        convert_screen.set_audiobook_sets(self.audiobook_sets)

        # Switch to conversion tab
        tabbed_content = self.query_one(TabbedContent)
        tabbed_content.active = "convert"

        self.update_status("Configure conversion settings and start conversion")

    def action_help(self) -> None:
        """Show help dialog."""
        # TODO: Implement help dialog
        self.update_status("Help: Use tabs to navigate, buttons to process")

    async def action_save_config(self) -> None:
        """Save current configuration without blocking the UI."""
        try:
            await asyncio.to_thread(self.config_manager.save_config)
            self.update_status("Configuration saved")
        except OSError as e:
            self.update_status(f"Failed to save config: {e}")

    def action_refresh(self) -> None:
        """Refresh the current view."""
        self.update_status("Refreshing...")
        # TODO: Implement refresh logic

    async def action_quit(self) -> None:
        """Quit the application."""
        # Close provider connections
        if self.provider:
            await self.provider.close()
        self.exit()
