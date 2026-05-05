"""Main TUI application for BookBot."""

import asyncio
import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from ..config.manager import ConfigManager
from ..core.discovery import AudioFileScanner
from ..core.models import AudiobookSet
from ..core.operations import TransactionManager
from ..core.planning import PlanBuilder, format_plan_diff
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

    #mission_control {
        height: 100%;
    }

    .mission-top {
        height: 1fr;
    }

    #scan_screen, #match_screen, #preview_screen {
        width: 1fr;
        height: 100%;
    }

    #warnings_panel {
        height: 8;
        border: heavy $accent;
        margin: 1;
        padding: 1;
        background: $background;
        color: $text;
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
        Binding("ctrl+h", "help", "Help", show=False),
        Binding("f1", "help", "Help", show=False),
        Binding("s", "scan", "Scan", show=True),
        Binding("m", "match", "Match", show=True),
        Binding("p", "preview_plan", "Preview", show=True),
        Binding("a", "apply_selected", "Apply", show=True),
        Binding("u", "undo", "Undo", show=True),
        Binding("d", "diff", "Diff", show=True),
        Binding("f", "filter_warnings", "Warnings", show=True),
        Binding("?", "help", "Help", show=True),
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
        self.last_transaction_id: str | None = None
        self.warning_filter_enabled = False

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()

        with Container(classes="main-container"):
            yield Label("BookBot - Ultimate Audiobook Organizer", classes="title")

            with TabbedContent(initial="mission" if self.source_folders else "source"):
                with TabPane("Mission Control", id="mission"):
                    with Container(id="mission_control"):
                        with Horizontal(classes="mission-top"):
                            yield ScanResultsScreen(
                                self.config_manager, id="scan_screen"
                            )
                            yield MatchReviewScreen(
                                self.config_manager, self.provider, id="match_screen"
                            )
                            yield PreviewScreen(
                                self.config_manager, id="preview_screen"
                            )
                        yield Static(
                            (
                                "Warnings, conflicts, and actions will appear here.\n"
                                "Shortcuts: s scan, m match, p preview, a apply, "
                                "u undo, d diff, f warnings, ? help"
                            ),
                            id="warnings_panel",
                        )

                with TabPane("Source Selection", id="source"):
                    yield SourceSelectionScreen(
                        self.config_manager, self.source_folders, id="source_screen"
                    )

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

    def update_warning_panel(self, message: str) -> None:
        """Update the bottom warning and action panel."""
        self.query_one("#warnings_panel", Static).update(message)

    def _library_root(self) -> Path | None:
        """Find the common root for scanned audiobook sets."""
        if not self.audiobook_sets:
            return None
        return Path(
            os.path.commonpath([str(book.source_path) for book in self.audiobook_sets])
        )

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

            # Switch to mission control tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "mission"

            self.update_status(
                f"Scan complete: Found {len(self.audiobook_sets)} audiobook set(s)"
            )
            warning_count = sum(len(book.warnings) for book in self.audiobook_sets)
            self.update_warning_panel(
                f"Queue loaded with {len(self.audiobook_sets)} books. "
                f"Warnings detected: {warning_count}."
            )

        except Exception as e:
            self.update_status(f"Scan failed: {e}")
            self.update_warning_panel(f"Scan failed: {e}")

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

            # Switch to mission control tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "mission"

            self.update_status("Matches found - review and confirm")
            self.update_warning_panel(
                "Metadata candidates loaded. Press p to preview the rename plan."
            )

        except Exception as e:
            self.update_status(f"Match finding failed: {e}")
            self.update_warning_panel(f"Match finding failed: {e}")

        finally:
            find_button.disabled = False
            find_button.label = "Find Matches"

    def show_preview(self) -> None:
        """Show preview of changes."""
        preview_screen = self.query_one("#preview_screen", PreviewScreen)
        preview_screen.source_roots = self.source_folders
        preview_screen.set_audiobook_sets(self.audiobook_sets)

        # Enable apply button
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.disabled = False

        # Switch to mission control tab
        tabbed_content = self.query_one(TabbedContent)
        tabbed_content.active = "mission"

        self.update_status("Review changes and apply when ready")
        self.update_warning_panel(
            "Preview updated. Press d to inspect the diff or a to apply the plan."
        )

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
                self.last_transaction_id = preview_screen.last_transaction_id
                # Enable conversion button after successful changes
                convert_button = self.query_one("#convert_m4b", Button)
                convert_button.disabled = False
                self.update_warning_panel(
                    "Plan applied. Press u to undo the latest transaction or switch "
                    "to Convert for M4B packaging."
                )
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
        self.update_status("Mission control shortcuts loaded")
        self.update_warning_panel(
            "Shortcuts: s scan, m match, p preview plan, a apply selected, "
            "u undo latest, d diff, f toggle warning summary, ? help"
        )

    def action_scan(self) -> None:
        """Start or restart scanning."""
        self.post_message(self.StartScan())

    async def action_match(self) -> None:
        """Trigger metadata matching."""
        await self.find_matches()

    def action_preview_plan(self) -> None:
        """Show the rename preview."""
        self.show_preview()

    async def action_apply_selected(self) -> None:
        """Apply the current plan."""
        await self.apply_changes()

    async def action_undo(self) -> None:
        """Undo the most recent transaction."""
        manager = TransactionManager(self.config_manager)
        transaction_id = self.last_transaction_id
        if transaction_id is None:
            recent = manager.list_transactions(days=365)
            transaction_id = next(
                (item["id"] for item in recent if item.get("can_undo")), None
            )

        if not transaction_id:
            self.update_status("No transaction available to undo")
            return

        success = await asyncio.to_thread(manager.undo_transaction, transaction_id)
        if success:
            self.update_status(f"Undid transaction {transaction_id[:8]}...")
            self.update_warning_panel("Latest transaction rolled back successfully.")
        else:
            self.update_status("Undo failed")

    def action_diff(self) -> None:
        """Show the current plan diff in the warning panel."""
        root = self._library_root()
        if root is None:
            self.update_status("No scanned library to diff")
            return
        plan = PlanBuilder(self.config_manager.load_config()).create_plan(
            root,
            self.audiobook_sets,
            source_roots=self.source_folders or [root],
        )
        diff = format_plan_diff(plan)
        self.update_warning_panel("\n".join(diff.splitlines()[:12]))
        self.update_status("Showing plan diff")

    def action_filter_warnings(self) -> None:
        """Toggle warning-focused summary in the bottom pane."""
        self.warning_filter_enabled = not self.warning_filter_enabled
        if not self.audiobook_sets:
            self.update_warning_panel("No scan results available yet.")
            return

        if self.warning_filter_enabled:
            lines = []
            for audiobook_set in self.audiobook_sets:
                if audiobook_set.warnings:
                    lines.append(f"{audiobook_set.source_path.name}:")
                    lines.extend(f"  - {warning}" for warning in audiobook_set.warnings)
            self.update_warning_panel(
                "\n".join(lines) if lines else "No warnings in the current queue."
            )
            self.update_status("Warning filter enabled")
        else:
            self.update_warning_panel(
                "Warnings filter cleared. Press d for diff or ? for shortcuts."
            )
            self.update_status("Warning filter disabled")

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
