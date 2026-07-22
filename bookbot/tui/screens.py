"""TUI screens for BookBot."""

import asyncio
import os
from asyncio import to_thread
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    ProgressBar,
    Select,
    Static,
)

from ..config.manager import ConfigManager
from ..core.exceptions import MetadataError
from ..core.logging import get_logger
from ..core.models import AudiobookSet, MatchCandidate
from ..core.operations import TransactionManager
from ..core.planning import PlanBuilder
from ..providers.base import MetadataProvider
from ..providers.manager import ProviderManager

try:
    from ..drm.audible_client import AudibleAuthClient
except Exception:  # pragma: no cover
    AudibleAuthClient = None  # type: ignore[assignment,misc]

logger = get_logger("tui_screens")


@dataclass(slots=True)
class MatchSummary:
    """Summary of a match run across the current audiobook queue."""

    matched_count: int = 0
    unmatched_count: int = 0
    failed_count: int = 0


class LoginSuccess(Message):
    """Posted on successful login."""


class LoginFailure(Message):
    """Posted on login failure."""

    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class DRMLoginScreen(Static):
    """Widget for handling Audible DRM login."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.auth_client: Any | None
        self._auth_error: str | None

        if AudibleAuthClient is None:
            self.auth_client = None
            self._auth_error = (
                "Audible DRM support is not available (missing dependencies)."
            )
        else:
            try:
                self.auth_client = AudibleAuthClient()
            except Exception as exc:
                self.auth_client = None
                self._auth_error = str(exc)
            else:
                self._auth_error = None

    def compose(self) -> ComposeResult:
        yield Container(
            Label("Audible Authentication", classes="section-title"),
            Static(
                "To remove DRM from Audible audiobooks, you need to authenticate with "
                "your Audible account. This process will open a browser window for you "
                "to log in securely.",
                classes="description",
            ),
            Static("", id="user_code_display"),
            Button("Begin Login", id="begin_login", variant="primary"),
            id="drm_login_container",
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "begin_login":
            await self.begin_auth()

    async def begin_auth(self) -> None:
        """Start the authentication process."""
        self.query_one("#begin_login", Button).disabled = True
        user_code_display = self.query_one("#user_code_display", Static)
        user_code_display.update("Starting Audible authentication...")

        if not self.auth_client:
            error_message = self._auth_error or "Audible authentication is unavailable."
            user_code_display.update(error_message)
            self.post_message(LoginFailure(error_message))
            self.query_one("#begin_login", Button).disabled = False
            return

        try:
            success = await to_thread(self.auth_client.authenticate)
            if success:
                user_code_display.update("Authentication successful.")
                self.post_message(LoginSuccess())
            else:
                user_code_display.update("Failed to authenticate with Audible.")
                self.post_message(LoginFailure("Failed to authenticate"))
        except Exception as e:
            user_code_display.update(f"An error occurred: {e}")
            self.post_message(LoginFailure(str(e)))
        finally:
            self.query_one("#begin_login", Button).disabled = False


class SourceSelectionScreen(Static):
    """Screen for selecting source directories."""

    def __init__(
        self,
        config_manager: ConfigManager,
        source_folders: list[Path],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.source_folders = source_folders

    def compose(self) -> ComposeResult:
        yield Label("Source Folders:", classes="section-title")

        if self.source_folders:
            for folder in self.source_folders:
                yield Label(f"[DIR] {folder}")
        else:
            yield Label("No folders selected")

        yield Button("Add Folder", id="add_folder")
        yield Label("", id="add_folder_hint")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add_folder":
            hint_label = self.query_one("#add_folder_hint", Label)
            hint_label.update(
                "Tip: Launch BookBot with folder paths, e.g. `bookbot tui /audiobooks`."
            )
            try:
                status_label = self.app.query_one("#status_label", Label)
                status_label.update(
                    "Add folders by restarting BookBot with target directories."
                )
            except Exception:
                pass


class ScanResultsScreen(Static):
    """Screen showing scan results."""

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.last_transaction_id: str | None = None
        self.source_roots: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Label("Scan Results", classes="section-title")
        yield DataTable(id="scan_results_table")

    def set_audiobook_sets(self, audiobook_sets: list[AudiobookSet]) -> None:
        """Set the audiobook sets to display."""
        self.audiobook_sets = audiobook_sets

        table = self.query_one("#scan_results_table", DataTable)
        table.clear(columns=True)

        # Add columns
        table.add_columns("Folder", "Tracks", "Discs", "Title Guess", "Warnings")

        # Add rows
        for audiobook_set in audiobook_sets:
            warnings = (
                f"{len(audiobook_set.warnings)} warning(s)"
                if audiobook_set.warnings
                else "None"
            )
            table.add_row(
                audiobook_set.source_path.name,
                str(audiobook_set.total_tracks),
                str(audiobook_set.disc_count),
                audiobook_set.raw_title_guess or "Unknown",
                warnings,
            )


class MatchReviewScreen(Static):
    """Screen for reviewing metadata matches."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider: MetadataProvider | ProviderManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.provider = provider
        self.audiobook_sets: list[AudiobookSet] = []

    def compose(self) -> ComposeResult:
        yield Label("Metadata Matches", classes="section-title")
        yield DataTable(id="matches_table")

    @staticmethod
    def _format_reasons(candidate: MatchCandidate) -> str:
        """Render match reasons compactly for table display."""
        return "; ".join(candidate.match_reasons)

    def _request_timeout_seconds(self) -> float:
        """Return the configured timeout for a single provider match request."""
        timeout = self.config_manager.load_config().providers.request_timeout
        return max(1.0, float(timeout or 15))

    async def _find_matches_with_timeout(
        self,
        audiobook_set: AudiobookSet,
    ) -> list[MatchCandidate]:
        """Bound direct-provider lookups so the TUI never hangs offline."""
        timeout = self._request_timeout_seconds()
        try:
            return await asyncio.wait_for(
                self.provider.find_matches(audiobook_set),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MetadataError(
                f"Provider timed out after {timeout:.0f}s",
                details={"timeout": timeout, "kind": "timeout"},
                recoverable=True,
            ) from exc

    async def find_matches(self, audiobook_sets: list[AudiobookSet]) -> MatchSummary:
        """Find matches for audiobook sets using merged multi-provider results."""
        self.audiobook_sets = audiobook_sets

        table = self.query_one("#matches_table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Audiobook",
            "Best Match",
            "Confidence",
            "Action",
            "Reasons",
        )
        summary = MatchSummary()

        # Use ProviderManager.find_matches_merged when available
        if isinstance(self.provider, ProviderManager):
            match_tasks = [
                self.provider.find_matches_merged(a) for a in audiobook_sets
            ]
        else:
            match_tasks = [
                self._find_matches_with_timeout(a) for a in audiobook_sets
            ]
        results = await asyncio.gather(*match_tasks, return_exceptions=True)

        for audiobook_set, result in zip(audiobook_sets, results, strict=False):
            audiobook_set.provider_candidates = []
            audiobook_set.chosen_identity = None
            candidates: list[MatchCandidate] = []
            if isinstance(result, BaseException):
                summary.failed_count += 1
                logger.warning(
                    "Metadata provider failed during TUI matching",
                    title_guess=(
                        audiobook_set.raw_title_guess or audiobook_set.source_path.name
                    ),
                    error=str(result),
                )
            else:
                candidates = result

            if candidates:
                best_match = candidates[0]
                audiobook_set.provider_candidates = candidates
                audiobook_set.chosen_identity = best_match.identity
                summary.matched_count += 1

                table.add_row(
                    audiobook_set.raw_title_guess or "Unknown",
                    (
                        f"{best_match.identity.title} - "
                        f"{', '.join(best_match.identity.authors)}"
                    ),
                    f"{best_match.confidence:.2f}",
                    "Accept" if best_match.confidence > 0.85 else "Review",
                    self._format_reasons(best_match),
                )
            else:
                summary.unmatched_count += 1
                table.add_row(
                    audiobook_set.raw_title_guess or "Unknown",
                    "No matches",
                    "0.00",
                    "Manual",
                    "",
                )

        return summary


class PreviewScreen(Static):
    """Screen for previewing changes."""

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.last_transaction_id: str | None = None
        self.source_roots: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Label("Preview Changes", classes="section-title")
        yield DataTable(id="preview_table")

    @staticmethod
    def _format_preview_path(path: Path, common_root: Path) -> str:
        """Render a readable relative path when the destination is nearby."""
        try:
            relative_path = Path(os.path.relpath(path, common_root))
        except ValueError:
            return str(path)

        leading_parents = 0
        for part in relative_path.parts:
            if part != "..":
                break
            leading_parents += 1

        if leading_parents > 2:
            return str(path)

        return str(relative_path)

    def set_audiobook_sets(self, audiobook_sets: list[AudiobookSet]) -> None:
        """Set audiobook sets and generate preview."""
        self.audiobook_sets = audiobook_sets

        table = self.query_one("#preview_table", DataTable)
        table.clear(columns=True)
        table.add_columns("Current Name", "Proposed Path", "Status")

        if not audiobook_sets:
            return

        config = self.config_manager.load_config()
        common_root = Path(
            os.path.commonpath([str(book.source_path) for book in audiobook_sets])
        )
        plan = PlanBuilder(config).create_plan(
            common_root,
            audiobook_sets,
            source_roots=self.source_roots or [common_root],
        )
        operations_by_source = {
            operation.old_path: operation for operation in plan.operations
        }

        for audiobook_set in audiobook_sets:
            for track in audiobook_set.tracks:
                current_name = track.src_path.name
                operation = operations_by_source.get(track.src_path)
                proposed_name = (
                    self._format_preview_path(operation.new_path, common_root)
                    if operation is not None
                    else self._format_preview_path(track.src_path, common_root)
                )
                status = "✓ Ready" if operation is not None else "→ No change"
                table.add_row(current_name, proposed_name, status)

    async def apply_changes(self) -> bool:
        """Apply the previewed changes."""
        try:
            # Initialize transaction manager
            transaction_manager = TransactionManager(self.config_manager)

            if not self.audiobook_sets:
                return True  # No operations needed

            config = self.config_manager.load_config()
            common_root = Path(
                os.path.commonpath(
                    [str(book.source_path) for book in self.audiobook_sets]
                )
            )
            plan = PlanBuilder(config).create_plan(
                common_root,
                self.audiobook_sets,
                source_roots=self.source_roots or [common_root],
            )
            if not plan.operations:
                return True
            if plan.conflicts:
                raise ValueError("; ".join(plan.conflicts))

            await to_thread(transaction_manager.execute_plan, plan, dry_run=False)

            transaction_id = plan.applied_transaction_id or "unknown"
            self.last_transaction_id = transaction_id

            # Update table to show completion
            table = self.query_one("#preview_table", DataTable)
            table.clear()
            table.add_columns("Operation", "Result", "Transaction ID")
            table.add_row(
                f"Renamed {len(plan.operations)} files",
                "✓ Success",
                transaction_id[:8] + "...",
            )

            return True

        except Exception as e:
            # Show error in table
            table = self.query_one("#preview_table", DataTable)
            table.clear()
            table.add_columns("Operation", "Result", "Error")
            table.add_row("Apply Changes", "Failed", str(e))
            return False


class ConversionScreen(Static):
    """Screen for M4B conversion options."""

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.conversion_in_progress = False
        self._result_row_keys: list[Any] = []

    class ConversionComplete(Message):
        """Message sent when conversion is complete."""

        def __init__(self, success: bool, message: str):
            super().__init__()
            self.success = success
            self.message = message

    def compose(self) -> ComposeResult:
        yield Label("M4B Conversion", classes="section-title")

        with Container():
            yield Label("Conversion Settings:", classes="subsection-title")

            with Horizontal():
                yield Label("Output Directory:")
                yield Input(placeholder="/path/to/output", id="output_dir")

            with Horizontal():
                yield Label("Audio Quality:")
                yield Select(
                    [
                        ("High Quality (192k)", "192k"),
                        ("Standard (128k)", "128k"),
                        ("Compressed (64k)", "64k"),
                        ("VBR High", "vbr5"),
                        ("VBR Standard", "vbr4"),
                    ],
                    value="128k",
                    id="quality_select",
                )

            with Horizontal():
                yield Checkbox("Normalize Audio", False, id="normalize_check")
                yield Checkbox("Include Cover Art", True, id="cover_art_check")

        with Container():
            yield Label("Progress:", classes="subsection-title")
            yield ProgressBar(id="conversion_progress")
            yield Label("Ready", id="conversion_status")

        with Container():
            yield DataTable(id="conversion_results")

        with Horizontal():
            yield Button("Start Conversion", id="start_conversion", variant="primary")
            yield Button("Cancel", id="cancel_conversion", disabled=True)

    def set_audiobook_sets(self, audiobook_sets: list[AudiobookSet]) -> None:
        """Set the audiobook sets for conversion."""
        self.audiobook_sets = audiobook_sets
        self._result_row_keys = []

        # Update results table to show what will be converted
        table = self.query_one("#conversion_results", DataTable)
        table.clear(columns=True)
        table.add_columns("Audiobook", "Tracks", "Output File", "Status")

        for audiobook_set in audiobook_sets:
            if audiobook_set.chosen_identity:
                title = audiobook_set.chosen_identity.title
                authors = ", ".join(audiobook_set.chosen_identity.authors)
                output_name = f"{authors} - {title}.m4b"
            else:
                output_name = f"{audiobook_set.source_path.name}.m4b"

            row_key = table.add_row(
                audiobook_set.raw_title_guess or "Unknown",
                str(audiobook_set.total_tracks),
                output_name,
                "Pending",
            )
            self._result_row_keys.append(row_key)

        # Enable conversion button if we have audiobooks
        start_button = self.query_one("#start_conversion", Button)
        start_button.disabled = len(audiobook_sets) == 0

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "start_conversion":
            await self.start_conversion()
        elif event.button.id == "cancel_conversion":
            await self.cancel_conversion()

    async def start_conversion(self) -> None:
        """Start the M4B conversion process."""
        if self.conversion_in_progress:
            return

        # Get conversion settings
        output_dir_input = self.query_one("#output_dir", Input)
        quality_select = self.query_one("#quality_select", Select)
        normalize_check = self.query_one("#normalize_check", Checkbox)
        cover_art_check = self.query_one("#cover_art_check", Checkbox)

        output_dir = (
            Path(output_dir_input.value)
            if output_dir_input.value
            else Path.cwd() / "converted"
        )
        quality_raw = quality_select.value
        quality: str = str(quality_raw) if quality_raw is not None else "128k"
        normalize = normalize_check.value
        include_cover = cover_art_check.value

        # Validate output directory
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.update_status(f"Error creating output directory: {e}")
            return

        # Update UI state
        self.conversion_in_progress = True
        start_button = self.query_one("#start_conversion", Button)
        cancel_button = self.query_one("#cancel_conversion", Button)
        start_button.disabled = True
        cancel_button.disabled = False

        progress_bar = self.query_one("#conversion_progress", ProgressBar)
        progress_bar.total = len(self.audiobook_sets)
        progress_bar.progress = 0

        try:
            # Import conversion pipeline
            from ..convert.pipeline import ConversionPipeline

            pipeline = ConversionPipeline(self.config_manager)

            # Setup conversion config
            conv_config = self.config_manager.load_config().conversion.model_copy()
            conv_config.output_directory = output_dir
            conv_config.normalize_audio = normalize
            conv_config.write_cover_art = include_cover

            if isinstance(quality, str) and quality.startswith("vbr"):
                conv_config.use_vbr = True
                conv_config.vbr_quality = int(quality[-1])
            else:
                conv_config.use_vbr = False
                conv_config.bitrate = str(quality)

            table = self.query_one("#conversion_results", DataTable)

            # Convert each audiobook set
            for i, audiobook_set in enumerate(self.audiobook_sets):
                if not self.conversion_in_progress:  # Check for cancellation
                    break

                self.update_status(
                    f"Converting {audiobook_set.raw_title_guess or 'Unknown'}..."
                )

                # Update table row status
                if i < len(self._result_row_keys):
                    table.update_cell(
                        self._result_row_keys[i], "Status", "Converting"
                    )

                try:
                    # Perform conversion
                    success = await pipeline.convert_audiobook_set(
                        audiobook_set, conv_config
                    )

                    if success:
                        if i < len(self._result_row_keys):
                            table.update_cell(
                                self._result_row_keys[i], "Status", "Complete"
                            )
                    else:
                        if i < len(self._result_row_keys):
                            table.update_cell(
                                self._result_row_keys[i], "Status", "Failed"
                            )

                except Exception as e:
                    if i < len(self._result_row_keys):
                        table.update_cell(
                            self._result_row_keys[i],
                            "Status",
                            f"Error: {str(e)[:20]}...",
                        )

                # Update progress
                progress_bar.progress = i + 1

            if self.conversion_in_progress:
                self.update_status("Conversion completed!")
                self.post_message(
                    self.ConversionComplete(True, "All conversions completed")
                )
            else:
                self.update_status("Conversion cancelled")
                self.post_message(
                    self.ConversionComplete(False, "Conversion was cancelled")
                )

        except ImportError:
            self.update_status("Error: FFmpeg is required for conversion")
            self.post_message(self.ConversionComplete(False, "FFmpeg not available"))

        except Exception as e:
            self.update_status(f"Conversion failed: {e}")
            self.post_message(self.ConversionComplete(False, str(e)))

        finally:
            # Reset UI state
            self.conversion_in_progress = False
            start_button.disabled = False
            cancel_button.disabled = True

    async def cancel_conversion(self) -> None:
        """Cancel the ongoing conversion."""
        self.conversion_in_progress = False
        self.update_status("Cancelling conversion...")

    def update_status(self, message: str) -> None:
        """Update the conversion status label."""
        status_label = self.query_one("#conversion_status", Label)
        status_label.update(message)
