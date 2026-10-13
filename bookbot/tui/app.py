"""Main TUI application for BookBot."""

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
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
from ..core.logging import get_logger
from ..core.models import AudiobookSet
from ..core.operations import TransactionManager
from ..core.planning import PlanBuilder, format_plan_diff
from ..providers.base import MetadataProvider
from ..providers.manager import ProviderManager
from .screens import (
    ConversionScreen,
    DRMLoginScreen,
    LoginFailure,
    LoginSuccess,
    MatchReviewScreen,
    PreviewScreen,
    ScanResultsScreen,
    SourceSelectionScreen,
    TextDialog,
)

logger = get_logger("tui_app")


class BookBotApp(App):
    """Main BookBot TUI application."""

    TITLE = "BookBot"
    SUB_TITLE = "Library Control"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $panel;
        color: $text;
    }

    #pipeline {
        height: 1;
        padding: 0 2;
        background: $boost;
        color: $text-muted;
    }

    TabbedContent {
        height: 1fr;
        background: $background;
    }

    TabPane {
        padding: 0 1;
        overflow-y: auto;
    }

    /* Narrow terminals: stack the library panes instead of squeezing them. */
    Screen.-narrow #mission_body {
        layout: vertical;
    }

    Screen.-narrow #scan_screen, Screen.-narrow #mission_right {
        width: 1fr;
    }

    Screen.-narrow #scan_screen {
        height: 1fr;
    }

    Screen.-narrow #mission_right {
        height: 2fr;
    }

    Screen.-narrow #source_screen > Horizontal,
    Screen.-narrow #convert_screen > Horizontal {
        layout: vertical;
    }

    Screen.-narrow #source_selected, Screen.-narrow #source_browser,
    Screen.-narrow #convert_settings, Screen.-narrow #convert_queue {
        width: 1fr;
    }

    Screen.-short #warnings_panel {
        display: none;
    }

    Screen.-narrow #source_selected {
        height: auto;
        max-height: 9;
    }

    Screen.-narrow #source_list {
        height: auto;
        max-height: 4;
    }

    Tab {
        color: $text-muted;
        text-style: bold;
        padding: 0 2;
    }

    Tab.-active {
        color: $accent;
    }

    Tab:hover {
        color: $text;
    }

    Underline > .underline--bar {
        color: $accent;
        background: $surface-lighten-1;
    }

    .pane {
        background: $surface;
        border: round $surface-lighten-3;
        border-title-color: $text-muted;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 0;
    }

    .pane:focus-within {
        border: round $accent;
        border-title-color: $accent;
    }

    #mission_control, #mission_body {
        height: 1fr;
    }

    #scan_screen {
        width: 2fr;
        height: 1fr;
    }

    #mission_right {
        width: 3fr;
        height: 1fr;
    }

    #match_screen, #preview_screen {
        height: 1fr;
    }

    #warnings_panel {
        height: auto;
        min-height: 3;
        max-height: 6;
        padding: 0 1;
        color: $text;
    }

    VerticalScroll {
        scrollbar-size-vertical: 1;
    }

    DataTable {
        height: 1fr;
        background: $surface;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }

    DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $surface-lighten-2;
        color: $text;
    }

    DataTable:focus > .datatable--cursor {
        background: $accent 35%;
        color: $text;
    }

    #command_bar {
        height: 1;
        padding: 0 1;
        background: $panel;
    }

    #status_label {
        width: 1fr;
        height: 1;
        color: $text;
        padding: 0 1;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }

    Button {
        min-width: 10;
        margin: 0 1 0 0;
        padding: 0 2;
        text-style: bold;
        background: $surface-lighten-2;
        color: $text;
    }

    #command_bar Button {
        margin: 0 0 0 1;
        min-width: 9;
        padding: 0 1;
    }

    Button.-primary {
        background: $accent 30%;
        color: $accent;
    }

    Button.-warning {
        background: $warning 30%;
        color: $warning;
    }

    Button:hover, Button:focus, Button.-primary:hover, Button.-primary:focus {
        background: $accent;
        color: $background;
    }

    Button:disabled, Button.-primary:disabled {
        background: $surface-lighten-1;
        color: $text-disabled;
    }

    #command_bar Button:disabled {
        background: $panel;
    }

    .form-row {
        height: 3;
        margin: 0 0 1 0;
    }

    .form-row > Label {
        width: 18;
        height: 3;
        content-align: left middle;
        color: $text-muted;
    }

    .form-row > Input, .form-row > Select {
        width: 1fr;
    }

    .hint {
        color: $text-muted;
        padding: 0 1;
    }

    .button-row {
        height: 1;
        margin: 1 0;
    }

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

    /* Sources tab */
    #source_screen {
        height: 1fr;
    }

    #source_selected {
        width: 2fr;
        height: 1fr;
        padding: 0 1;
    }

    #source_browser {
        width: 3fr;
        height: 1fr;
        padding: 0 1;
    }

    #source_list {
        height: 1fr;
        background: $surface;
    }

    #source_tree {
        height: 1fr;
        background: $surface;
    }

    #source_path {
        margin: 1 0 0 0;
    }

    /* Convert tab */
    #convert_screen {
        height: 1fr;
    }

    #convert_settings {
        width: 2fr;
        height: 1fr;
        padding: 1 2;
    }

    #convert_queue {
        width: 3fr;
        height: 1fr;
        padding: 0 1;
    }

    #conversion_progress {
        margin: 1 0 0 0;
    }

    #conversion_progress Bar {
        width: 1fr;
    }

    #conversion_status {
        color: $text-muted;
        height: 1;
    }

    .checkbox-row {
        height: auto;
        layout: vertical;
    }

    Checkbox {
        background: $surface;
        border: none;
        margin-right: 2;
    }

    /* Audible tab */
    #drm_login_screen {
        height: 1fr;
        align: center top;
    }

    #drm_login_container {
        width: 100%;
        max-width: 96;
        height: auto;
        margin: 1 0;
        padding: 1 2;
    }

    #drm_login_container .description {
        margin: 0 0 1 0;
        color: $text-muted;
    }

    #user_code_display {
        height: auto;
        min-height: 1;
        padding: 0 1;
        margin-bottom: 1;
        border-left: wide $success;
    }

    /* Modals */
    CandidateDetailScreen, ManualSearchScreen, TextDialog {
        align: center middle;
        background: $background 60%;
    }

    .candidate-modal {
        width: 90%;
        max-width: 140;
        height: 85%;
        padding: 0 1;
        border: round $accent;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
    }

    ManualSearchScreen .candidate-modal {
        height: auto;
        max-width: 90;
        padding: 1 2;
    }

    .modal-path {
        color: $text-muted;
        margin-bottom: 1;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }

    #candidate_table {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }

    .text-dialog {
        height: 80%;
        max-width: 100;
        padding: 0 2;
    }

    #candidate_status {
        color: $warning;
        height: 1;
        margin-bottom: 1;
    }

    .candidate-columns {
        height: 1fr;
    }

    #candidate_local, #candidate_detail {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (28, "-tall")]

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+h", "help", "Help", show=False),
        Binding("f1", "help", "Help", show=False),
        Binding("s", "scan", "Scan", show=True),
        Binding("m", "match", "Match", show=True),
        Binding("c", "choose_candidate", "Choose", show=False),
        Binding("A", "accept_safe", "Accept safe", show=False),
        Binding("p", "preview_plan", "Preview", show=True),
        Binding("a", "apply_selected", "Apply", show=True),
        Binding("u", "undo", "Undo", show=True),
        Binding("d", "diff", "Diff", show=True),
        Binding("f", "filter_warnings", "Warnings", show=False),
        Binding("?", "help", "Help", show=True),
        Binding("ctrl+s", "save_config", "Save Config", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    def __init__(
        self,
        config_manager: ConfigManager,
        source_folders: list[Path],
        provider: MetadataProvider | ProviderManager | None = None,
    ):
        super().__init__()
        self.theme = "catppuccin-mocha"
        self.config_manager = config_manager
        self.source_folders = source_folders
        self.audiobook_sets: list[AudiobookSet] = []
        self.provider: MetadataProvider | ProviderManager = provider or ProviderManager(
            config_manager
        )

        # Application state
        self.current_step = "source_selection"
        self.scanning_complete = False
        self.last_transaction_id: str | None = None
        self.warning_filter_enabled = False
        self._provider_closed = False
        self._matched_count: int | None = None
        self._applied_this_scan: str | None = None
        # Two-press apply confirmation: first `a` arms, second `a` applies,
        # any other action disarms. Prevents single-keystroke destructive
        # applies in the TUI.
        self._apply_armed = False
        self._apply_timer: Timer | None = None
        self._matching = False

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header(icon="◆")
        yield Static("", id="pipeline")

        with TabbedContent(initial="mission" if self.source_folders else "source"):
            with TabPane("Library", id="mission"):
                with Vertical(id="mission_control"):
                    with Horizontal(id="mission_body"):
                        yield ScanResultsScreen(
                            self.config_manager, id="scan_screen", classes="pane"
                        )
                        with Vertical(id="mission_right"):
                            yield MatchReviewScreen(
                                self.config_manager,
                                self.provider,
                                id="match_screen",
                                classes="pane",
                            )
                            yield PreviewScreen(
                                self.config_manager,
                                id="preview_screen",
                                classes="pane",
                            )
                    yield Static(
                        "Press s to scan, m to match, p to preview, "
                        "a to apply, ? for every shortcut.",
                        id="warnings_panel",
                        classes="pane",
                        markup=False,
                    )

            with TabPane("Sources", id="source"):
                yield SourceSelectionScreen(
                    self.config_manager, self.source_folders, id="source_screen"
                )

            with TabPane("Convert", id="convert"):
                yield ConversionScreen(self.config_manager, id="convert_screen")

            with TabPane("Audible", id="drm_tab"):
                yield DRMLoginScreen(id="drm_login_screen")

        with Horizontal(id="command_bar"):
            yield Label("Ready", id="status_label", markup=False)
            yield Button("Scan", id="start_scan", variant="primary", compact=True)
            yield Button("Match", id="find_matches", disabled=True, compact=True)
            yield Button("Preview", id="preview_changes", disabled=True, compact=True)
            yield Button("Apply", id="apply_changes", disabled=True, compact=True)
            yield Button("Convert", id="convert_m4b", disabled=True, compact=True)

        yield Footer(compact=True)

    async def on_mount(self) -> None:
        """Called when the app is mounted."""
        # Initialize default profiles if they don't exist
        self.config_manager.create_default_profiles()

        self.query_one("#warnings_panel", Static).border_title = "Activity"
        self._refresh_subtitle()
        self.refresh_pipeline()

        # Set initial status
        self.update_status(
            "Scanning preloaded folders..."
            if self.source_folders
            else "Pick a library folder on the Sources tab, then press s to scan"
        )

        # If folders were provided, start scanning automatically
        if self.source_folders:
            self.current_step = "scanning"
            self.set_timer(0.1, lambda: self.post_message(self.StartScan()))

    def _refresh_subtitle(self) -> None:
        """Show the active library folders in the header."""
        if not self.source_folders:
            self.sub_title = "no library selected"
        elif len(self.source_folders) == 1:
            self.sub_title = str(self.source_folders[0])
        else:
            self.sub_title = f"{len(self.source_folders)} library folders"

    def refresh_pipeline(self) -> None:
        """Render the scan -> match -> preview -> apply progress strip."""
        books = len(self.audiobook_sets)
        plan = self.query_one("#preview_screen", PreviewScreen).last_plan
        steps: list[tuple[str, str, str]] = []

        if self.current_step == "scanning":
            steps.append(("active", "Scan", "scanning"))
        elif self.scanning_complete:
            steps.append(("done", "Scan", f"{books} book{'s' if books != 1 else ''}"))
        else:
            steps.append(("todo", "Scan", ""))

        if self._matched_count is None:
            steps.append(("todo", "Match", ""))
        else:
            review = sum(1 for book in self.audiobook_sets if book.needs_review)
            state = "done" if self._matched_count == books and not review else "warn"
            detail = f"{self._matched_count}/{books}"
            if review:
                detail += f" · {review} to review"
            steps.append((state, "Match", detail))

        if plan is None:
            steps.append(("todo", "Preview", ""))
        else:
            count = len(plan.operations) + len(plan.planned_sidecars)
            state = "warn" if plan.skipped else "done"
            steps.append(
                (state, "Preview", f"{count} change{'s' if count != 1 else ''}")
            )

        if self._applied_this_scan:
            steps.append(("done", "Apply", self._applied_this_scan[:8]))
        else:
            steps.append(("todo", "Apply", ""))

        icons = {
            "done": ("✓", "green"),
            "warn": ("!", "yellow"),
            "active": ("●", "cyan"),
            "todo": ("○", "dim"),
        }
        text = Text()
        for index, (state, label, detail) in enumerate(steps):
            if index:
                text.append("  ›  ", style="dim")
            icon, style = icons[state]
            text.append(f"{icon} ", style=style)
            text.append(label, style="bold" if state != "todo" else "dim")
            if detail:
                text.append(f" {detail}", style="dim" if state == "todo" else "")
        self.query_one("#pipeline", Static).update(text)

    async def on_login_success(self, message: LoginSuccess) -> None:
        """Handle successful DRM login."""
        self.update_status("Successfully logged into Audible.")
        # Optionally, switch to a different screen or enable DRM-related features
        # For now, just update the status
        self.query_one(TabbedContent).active = "drm_tab"

    async def on_login_failure(self, message: LoginFailure) -> None:
        """Handle failed DRM login."""
        self.update_status(f"Audible login failed: {message.error}")
        self.query_one(TabbedContent).active = "drm_tab"

    def on_source_selection_screen_folders_changed(
        self, message: SourceSelectionScreen.FoldersChanged
    ) -> None:
        """Keep the header and scan button in sync with the chosen folders."""
        self._refresh_subtitle()
        count = len(self.source_folders)
        self.update_status(
            f"{count} folder(s) selected - press s to scan"
            if count
            else "Pick a library folder on the Sources tab, then press s to scan"
        )

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

        def __init__(
            self,
            applied_transaction: str | None = None,
            notice: str | None = None,
        ) -> None:
            super().__init__()
            self.applied_transaction = applied_transaction
            self.notice = notice

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
            self.action_scan()
        elif event.button.id == "find_matches":
            self.action_match()
        elif event.button.id == "preview_changes":
            self.action_preview_plan()
        elif event.button.id == "apply_changes":
            await self.action_apply_selected()
        elif event.button.id == "convert_m4b":
            self.show_conversion()

    async def on_book_bot_app_start_scan(self, event: StartScan) -> None:
        """Handle start scan message."""
        if not self.source_folders:
            self.update_status("No folders selected - add one on the Sources tab first")
            self.query_one(TabbedContent).active = "source"
            return
        self.run_worker(
            self._scan_library(event.applied_transaction, event.notice),
            group="scan",
            exclusive=True,
        )

    async def _scan_library(
        self,
        applied_transaction: str | None = None,
        notice: str | None = None,
    ) -> None:
        """Scan every source folder off the UI thread and reset later steps."""
        # Disable scan button
        scan_button = self.query_one("#start_scan", Button)
        scan_button.disabled = True
        scan_button.label = "Scanning…"

        self.update_status("Scanning for audiobooks...")
        self.current_step = "scanning"
        self.refresh_pipeline()

        try:
            scanner_tasks = []
            for folder in self.source_folders:
                self.update_status(f"Scanning {folder.name}...")
                scanner_tasks.append(
                    asyncio.to_thread(
                        AudioFileScanner(recursive=True, max_depth=5).scan_directory,
                        folder,
                        self._scan_progress_reporter(folder),
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
            self.current_step = "scan_complete"
            self._matched_count = None
            self._applied_this_scan = applied_transaction
            self._disarm_apply(quiet=True)
            self.query_one("#match_screen", MatchReviewScreen).reset()
            self.query_one("#preview_screen", PreviewScreen).reset()
            self.query_one("#preview_changes", Button).disabled = True
            self.query_one("#apply_changes", Button).disabled = True
            self.query_one("#convert_m4b", Button).disabled = not all_audiobook_sets

            # Update UI
            scan_screen = self.query_one("#scan_screen", ScanResultsScreen)
            scan_screen.set_audiobook_sets(self.audiobook_sets)
            self.query_one("#convert_screen", ConversionScreen).set_audiobook_sets(
                self.audiobook_sets
            )

            # Enable next step
            find_button = self.query_one("#find_matches", Button)
            find_button.disabled = False

            # Switch to mission control tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "mission"
            self.query_one("#scan_results_table").focus()

            warning_count = sum(len(book.warnings) for book in self.audiobook_sets)
            if notice:
                self.update_status(notice)
            elif applied_transaction:
                self.update_status(
                    f"Library refreshed after apply - press u to undo "
                    f"{applied_transaction[:8]}"
                )
            else:
                self.update_status(
                    f"Scan complete: Found {len(self.audiobook_sets)} audiobook set(s)"
                )
                self.update_warning_panel(
                    f"Queue loaded with {len(self.audiobook_sets)} books. "
                    f"Warnings detected: {warning_count}. Press m to match metadata."
                )

        except Exception as e:
            self.current_step = "source_selection"
            self.update_status(f"Scan failed: {e}")
            self.update_warning_panel(f"Scan failed: {e}")

        finally:
            # Re-enable scan button
            scan_button.disabled = False
            scan_button.label = "Scan"
            self.refresh_pipeline()

    def _scan_progress_reporter(self, folder: Path) -> Callable[[int, int, str], None]:
        """Build a thread-safe callback that shows scan progress in the status bar."""
        last_update = 0.0

        def report(done: int, total: int, name: str) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.1 and done + 1 < total:
                return
            last_update = now
            self.call_from_thread(
                self.update_status,
                f"Reading {done + 1}/{total} in {folder.name}: {name}",
            )

        return report

    def on_match_review_screen_match_progress(
        self, message: MatchReviewScreen.MatchProgress
    ) -> None:
        """Show live progress while providers are queried."""
        if not self._matching:
            return
        self.update_status(
            f"Matching {message.done}/{message.total} books "
            f"({message.matched} with candidates so far)..."
        )

    async def find_matches(self) -> None:
        """Find metadata matches for audiobooks."""
        if not self.audiobook_sets:
            self.update_status("No audiobooks to match")
            return

        find_button = self.query_one("#find_matches", Button)
        find_button.disabled = True
        find_button.label = "Matching…"
        self._matching = True

        self.update_status("Finding metadata matches...")

        try:
            match_screen = self.query_one("#match_screen", MatchReviewScreen)
            summary = await match_screen.find_matches(self.audiobook_sets)
            self._matching = False
            self._matched_count = summary.matched_count

            # Enable next step
            preview_button = self.query_one("#preview_changes", Button)
            preview_button.disabled = False
            self.query_one("#preview_screen", PreviewScreen).reset()
            self.query_one("#apply_changes", Button).disabled = True

            # Switch to mission control tab
            tabbed_content = self.query_one(TabbedContent)
            tabbed_content.active = "mission"
            self.query_one("#matches_table").focus()

            total_books = len(self.audiobook_sets)
            review = sum(1 for book in self.audiobook_sets if book.needs_review)
            if summary.matched_count == total_books and review:
                self.update_status(
                    f"Matched {total_books} book(s) - {review} need your review"
                )
                self.update_warning_panel(
                    f"{review} match(es) are below the auto-accept threshold or have "
                    "review flags. Highlight a row and press Enter (or c) to compare "
                    "candidates, then p to preview. Unreviewed books are skipped."
                )
            elif summary.matched_count == total_books:
                self.update_status("All books matched")
                self.update_warning_panel(
                    "Metadata candidates loaded. Press p to preview the rename plan."
                )
            elif summary.matched_count == 0:
                self.update_status("No matches found")
                self.update_warning_panel(
                    "No metadata candidates were found. Check network/providers, "
                    "then try manual search if available."
                )
            else:
                self.update_status(
                    f"Matched {summary.matched_count} of {total_books} - "
                    "review unmatched"
                )
                if summary.failed_count:
                    self.update_warning_panel(
                        f"{summary.unmatched_count} book(s) still need matches. "
                        "Some providers failed, so check network/providers or try "
                        "manual search if available."
                    )
                else:
                    self.update_warning_panel(
                        f"{summary.unmatched_count} book(s) still need matches. "
                        "Review the unmatched rows or try manual search if available."
                    )

        except Exception as e:
            self.update_status(f"Match finding failed: {e}")
            self.update_warning_panel(f"Match finding failed: {e}")

        finally:
            self._matching = False
            find_button.disabled = False
            find_button.label = "Match"
            self.refresh_pipeline()

    def show_preview(self) -> None:
        """Show preview of changes."""
        if not self.audiobook_sets:
            self.update_status("Nothing to preview yet - press s to scan a library")
            return
        preview_screen = self.query_one("#preview_screen", PreviewScreen)
        preview_screen.source_roots = self.source_folders
        preview_screen.set_audiobook_sets(self.audiobook_sets)
        plan = preview_screen.last_plan

        # Enable apply button
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.disabled = bool(
            plan is None or not (plan.operations or plan.planned_sidecars)
        )

        # Switch to mission control tab
        tabbed_content = self.query_one(TabbedContent)
        tabbed_content.active = "mission"
        self.query_one("#preview_table").focus()

        self.refresh_pipeline()
        skipped_count = len(plan.skipped) if plan is not None else 0
        if skipped_count:
            self.update_status(
                f"Preview ready - {skipped_count} book(s) still need review"
            )
            self.update_warning_panel(
                f"Preview updated. {skipped_count} book(s) were skipped because "
                "they still need review. Press d to inspect the diff."
            )
        elif plan is not None and not (plan.operations or plan.planned_sidecars):
            self.update_status("Nothing to change - the library already matches")
            self.update_warning_panel(
                "Every file is already where the current profile wants it."
            )
        elif self._matched_count is None:
            self.update_status("Preview ready - based on folder names and tags only")
            self.update_warning_panel(
                "No metadata match has run, so names come from local folder names "
                "and tags. Press m to match against providers first, or d to "
                "inspect the diff before applying."
            )
        else:
            self.update_status("Review changes and apply when ready")
            self.update_warning_panel(
                "Preview updated. Press d to inspect the diff or a to apply the plan."
            )

    async def apply_changes(self) -> None:
        """Apply the changes."""
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.disabled = True
        apply_button.label = "Applying…"

        self.update_status("Applying changes...")

        try:
            preview_screen = self.query_one("#preview_screen", PreviewScreen)
            success = await preview_screen.apply_changes()

            if success and preview_screen.last_transaction_id:
                applied = preview_screen.last_transaction_id
                self.last_transaction_id = applied
                self._applied_this_scan = applied
                self.update_status("Changes applied successfully!")
                self.update_warning_panel(
                    f"Plan applied as transaction {applied}. Press u to undo it. "
                    "The library view has been refreshed from disk."
                )
                # The old sets now point at moved files; rescan from disk.
                self.post_message(self.StartScan(applied_transaction=applied))
                return
            if success:
                self.update_status("Nothing was applied - the plan had no changes")
            else:
                self.update_status("Failed to apply changes")
                self.update_warning_panel(
                    "Apply did not complete. Nothing was left half-moved; check the "
                    "preview pane for the reason."
                )
            apply_button.disabled = False

        except Exception as e:
            self.update_status(f"Error applying changes: {e}")
            apply_button.disabled = False

        finally:
            apply_button.label = "Apply"
            self.refresh_pipeline()

    def show_conversion(self) -> None:
        """Show M4B conversion screen."""
        convert_screen = self.query_one("#convert_screen", ConversionScreen)
        convert_screen.set_audiobook_sets(self.audiobook_sets)

        # Switch to conversion tab
        tabbed_content = self.query_one(TabbedContent)
        tabbed_content.active = "convert"

        self.update_status("Configure conversion settings and start conversion")

    HELP_TEXT = """\
Library
  s           Scan the library folders
  m           Match every book against metadata providers
  c / Enter   Compare candidates for the highlighted match
  A           Accept every safe match (no BLOCK flag, confidence 0.75+)
  p           Preview the rename plan
  a, a        Apply the plan (press twice to confirm)
  u           Undo the latest applied transaction
  d           Show the full plan diff
  f           List scan warnings
  ctrl+r      Rescan the library from disk

Candidate picker
  Enter       Accept the highlighted candidate
  r           Re-run matching for this book
  /           Manual search by title, author, ASIN or ISBN
  s           Skip this book in future plans
  Esc         Close without changes

Anywhere
  Tab         Move between panes
  ctrl+s      Save configuration
  ctrl+p      Command palette
  ?           This help
  ctrl+c      Quit
"""

    def action_help(self) -> None:
        """Show every shortcut in a scrollable dialog."""
        self._disarm_apply()
        self.push_screen(TextDialog("Keyboard Shortcuts", self.HELP_TEXT))

    def action_scan(self) -> None:
        """Start or restart scanning."""
        self._disarm_apply()
        self.post_message(self.StartScan())

    def action_match(self) -> None:
        """Trigger metadata matching without blocking the interface."""
        self._disarm_apply()
        if not self.scanning_complete:
            self.update_status("Nothing to match yet - press s to scan a library")
            return
        self.run_worker(self.find_matches(), group="match", exclusive=True)

    async def action_choose_candidate(self) -> None:
        """Open the candidate picker for the focused match row."""
        self._disarm_apply()
        match_screen = self.query_one("#match_screen", MatchReviewScreen)
        if not match_screen.audiobook_sets:
            self.update_status("Press m to find matches first")
            return
        await match_screen.open_focused_candidate_picker()

    def action_accept_safe(self) -> None:
        """Bulk-accept top candidates that have no blocking review flags."""
        self._disarm_apply()
        match_screen = self.query_one("#match_screen", MatchReviewScreen)
        if not match_screen.audiobook_sets:
            self.update_status("Press m to find matches first")
            return
        accepted = match_screen.accept_safe_candidates()
        remaining = sum(1 for book in self.audiobook_sets if book.needs_review)
        self.refresh_pipeline()
        if accepted:
            self.update_status(
                f"Accepted {accepted} safe match(es) - {remaining} still need review"
            )
            self.update_warning_panel(
                f"Accepted the top candidate for {accepted} book(s) with no blocking "
                "flags. Press p to preview; nothing moves until you apply."
            )
        else:
            self.update_status(
                "No safe matches to accept - review the remaining rows by hand"
            )

    def action_preview_plan(self) -> None:
        """Show the rename preview."""
        self._disarm_apply()
        self.show_preview()

    async def action_apply_selected(self) -> None:
        """Apply the current plan (press twice: arm, then confirm)."""
        apply_button = self.query_one("#apply_changes", Button)
        if apply_button.disabled and not self._apply_armed:
            self.update_status("Nothing to apply yet - press p to preview a plan")
            return

        if not self._apply_armed:
            self._apply_armed = True
            apply_button.label = "Confirm"
            apply_button.variant = "warning"
            operation_count = self._pending_operation_count()
            count_text = (
                f"{operation_count} operation(s)"
                if operation_count is not None
                else "the current plan"
            )
            self.update_status(f"Press a again to apply {count_text}")
            self.update_warning_panel(
                f"Confirm apply: press a again (or click Confirm) to apply "
                f"{count_text}. Any other action, or waiting 10 seconds, cancels."
            )
            self._apply_timer = self.set_timer(10, self._disarm_apply)
            return

        self._reset_apply_button()
        self._apply_armed = False
        await self.apply_changes()

    def _pending_operation_count(self) -> int | None:
        """Best-effort count of operations the apply would perform."""
        try:
            preview_screen = self.query_one("#preview_screen", PreviewScreen)
            config = self.config_manager.load_config()
            if not preview_screen.audiobook_sets:
                return None
            common_root = Path(
                os.path.commonpath(
                    [str(book.source_path) for book in preview_screen.audiobook_sets]
                )
            )
            plan = PlanBuilder(config).create_plan(
                common_root,
                preview_screen.audiobook_sets,
                source_roots=preview_screen.source_roots or [common_root],
            )
            return len(plan.operations) + len(plan.planned_sidecars)
        except Exception:
            return None

    def _reset_apply_button(self) -> None:
        if self._apply_timer is not None:
            self._apply_timer.stop()
            self._apply_timer = None
        apply_button = self.query_one("#apply_changes", Button)
        apply_button.label = "Apply"
        apply_button.variant = "default"

    def _disarm_apply(self, quiet: bool = False) -> None:
        """Cancel a pending apply confirmation."""
        if self._apply_armed:
            self._apply_armed = False
            self._reset_apply_button()
            if not quiet:
                self.update_status("Apply cancelled")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Switching tabs cancels a pending apply confirmation."""
        self._disarm_apply()

    async def action_undo(self) -> None:
        """Undo the most recent transaction."""
        self._disarm_apply()
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

        self.update_status(f"Undoing transaction {transaction_id[:8]}...")
        success = await asyncio.to_thread(manager.undo_transaction, transaction_id)
        if success:
            self.last_transaction_id = None
            self.update_status(f"Undid transaction {transaction_id[:8]}")
            self.update_warning_panel(
                f"Transaction {transaction_id} rolled back. "
                "The library view has been refreshed from disk."
            )
            if self.source_folders:
                self.post_message(
                    self.StartScan(notice=f"Undid transaction {transaction_id[:8]}")
                )
        else:
            self.update_status("Undo failed")
            self.update_warning_panel(
                f"Could not undo transaction {transaction_id}. Run "
                f"`bookbot recover` or `bookbot history` from a shell for details."
            )

    def action_diff(self) -> None:
        """Show the full plan diff in a scrollable dialog."""
        self._disarm_apply()
        root = self._library_root()
        if root is None:
            self.update_status("No scanned library to diff")
            return
        plan = self.query_one("#preview_screen", PreviewScreen).last_plan
        if plan is None:
            plan = PlanBuilder(self.config_manager.load_config()).create_plan(
                root,
                self.audiobook_sets,
                source_roots=self.source_folders or [root],
            )
        self.push_screen(
            TextDialog("Plan Diff", self._styled_diff(format_plan_diff(plan)))
        )

    @staticmethod
    def _styled_diff(diff: str) -> Text:
        """Show each move as a dim source line followed by its new location."""
        styled = Text()
        for line in diff.splitlines():
            old, arrow, new = line.partition(" -> ")
            if arrow:
                styled.append(old + "\n", style="dim")
                styled.append("  → ", style="bold cyan")
                styled.append(new + "\n", style="bold")
            else:
                styled.append(line + "\n")
        return styled

    def action_filter_warnings(self) -> None:
        """List every scan warning in a scrollable dialog."""
        self._disarm_apply()
        if not self.audiobook_sets:
            self.update_status("No scan results yet - press s to scan")
            return

        lines = []
        for audiobook_set in self.audiobook_sets:
            if audiobook_set.warnings:
                lines.append(str(audiobook_set.source_path))
                lines.extend(f"  - {warning}" for warning in audiobook_set.warnings)
                lines.append("")
        self.push_screen(
            TextDialog(
                "Scan Warnings",
                "\n".join(lines) if lines else "No warnings in the current queue.",
            )
        )

    async def action_save_config(self) -> None:
        """Save current configuration without blocking the UI."""
        try:
            self.config_manager.load_config()
            await asyncio.to_thread(self.config_manager.save_config)
            self.update_status("Configuration saved")
        except Exception as e:
            self.update_status(f"Failed to save config: {e}")

    def action_refresh(self) -> None:
        """Rescan the library from disk."""
        self.action_scan()

    async def _close_provider(self) -> None:
        """Close provider resources exactly once across all exit paths."""
        if self._provider_closed or not self.provider:
            return

        self._provider_closed = True
        try:
            if isinstance(self.provider, ProviderManager):
                await self.provider.close_all()
            else:
                await self.provider.close()
        except Exception as e:
            logger.warning("Failed to close TUI provider", error=str(e))

    async def on_unmount(self) -> None:
        """Ensure provider teardown also runs on abort and direct exit paths."""
        await self._close_provider()

    async def action_quit(self) -> None:
        """Quit the application."""
        await self._close_provider()
        self.exit()
