"""TUI screens for BookBot."""

import asyncio
import contextlib
import io
import os
from asyncio import to_thread
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Input,
    Label,
    ProgressBar,
    Select,
    Static,
    Tree,
)

from ..config.manager import ConfigManager
from ..core.exceptions import MetadataError
from ..core.logging import get_logger
from ..core.matching import AdvancedMatcher
from ..core.models import AudiobookSet, MatchCandidate, RenamePlan
from ..core.operations import TransactionManager
from ..core.planning import PlanBuilder
from ..core.review import (
    accept_candidate_for_set,
    detect_review_flags,
    has_flag_at_or_above,
    mark_set_needs_review,
    skip_set_from_plans,
    worst_flag_severity,
)
from ..core.review_store import ReviewDecisionStore
from ..io.sidecar import SidecarManager
from ..providers.base import MetadataProvider
from ..providers.manager import ProviderManager

try:
    from ..drm.audible_client import AudibleAuthClient
except Exception:  # pragma: no cover
    AudibleAuthClient = None  # type: ignore[assignment,misc]

logger = get_logger("tui_screens")


def _column_widths(table: DataTable, specs: Sequence[int | float]) -> list[int | None]:
    """Resolve column widths so a table fills its pane without scrolling sideways.

    Integers are fixed widths; floats are relative shares of whatever width is
    left over. Before the table has been laid out every flexible column is
    auto-sized instead.
    """
    available = table.size.width
    if available <= 0:
        return [spec if isinstance(spec, int) else None for spec in specs]
    padding = 2 * table.cell_padding * len(specs)
    fixed = sum(spec for spec in specs if isinstance(spec, int))
    scrollbar = table.styles.scrollbar_size_vertical
    spare = max(0, available - padding - fixed - scrollbar)
    shares = sum(spec for spec in specs if isinstance(spec, float)) or 1.0
    return [
        spec if isinstance(spec, int) else max(6, int(spare * spec / shares))
        for spec in specs
    ]


def _cell(value: str, style: str = "") -> Text:
    """Render a single-line cell that ends in an ellipsis when it is too long."""
    return Text(value, style=style, overflow="ellipsis", no_wrap=True)


def _format_runtime(seconds: float | None) -> str:
    """Render a runtime as a compact h:mm value."""
    if not seconds:
        return "-"
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h{minutes % 60:02d}"


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
            Static(
                "Authenticate with your Audible account to access books you own. "
                "AAX output is decrypted to a temporary file, decoded for integrity, "
                "and only then published. AAXC requires its matching voucher and "
                "content key.",
                classes="description",
            ),
            Static(
                "Not signed in. Login opens a browser window on this machine.",
                id="user_code_display",
                markup=False,
            ),
            Button("Begin Login", id="begin_login", variant="primary", compact=True),
            id="drm_login_container",
            classes="pane",
        )

    def on_mount(self) -> None:
        self.query_one(
            "#drm_login_container"
        ).border_title = "Protected Audiobook Access"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "begin_login":
            await self.begin_auth()

    def _authenticate_quietly(self) -> bool:
        """Run the browser login without its console prints drawing over the UI."""
        assert self.auth_client is not None
        with contextlib.redirect_stdout(io.StringIO()):
            return bool(self.auth_client.authenticate())

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
            success = await to_thread(self._authenticate_quietly)
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


class FolderTree(DirectoryTree):
    """Directory browser that only lists folders, hiding dot-directories."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        visible: list[Path] = []
        for path in paths:
            try:
                if path.is_dir() and not path.name.startswith("."):
                    visible.append(path)
            except OSError:
                continue
        return visible


class SourceSelectionScreen(Static):
    """Pick the library folders to scan."""

    class FoldersChanged(Message):
        """Posted when the selected library folders change."""

    def __init__(
        self,
        config_manager: ConfigManager,
        source_folders: list[Path],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.source_folders = source_folders

    def _initial_browse_root(self) -> Path:
        if self.source_folders:
            parent = self.source_folders[0].expanduser().resolve().parent
            if parent.is_dir():
                return parent
        return Path.home()

    def compose(self) -> ComposeResult:
        root = self._initial_browse_root()
        with Horizontal():
            with Vertical(id="source_selected", classes="pane"):
                yield DataTable(id="source_list", cursor_type="row")
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Scan", id="scan_sources", variant="primary", compact=True
                    )
                    yield Button("Remove", id="remove_folder", compact=True)
            with Vertical(id="source_browser", classes="pane"):
                yield FolderTree(root, id="source_tree")
                yield Input(
                    value=str(root),
                    placeholder="Type a folder path and press Enter",
                    id="source_path",
                )
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Add Folder", id="add_folder", variant="primary", compact=True
                    )
                    yield Button("Up", id="browse_up", compact=True)
                yield Label("", id="add_folder_hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#source_selected").border_title = "Library Folders"
        browser = self.query_one("#source_browser")
        browser.border_title = "Browse"
        browser.border_subtitle = "enter expands · Add Folder selects"
        self._render_folders()

    def _render_folders(self) -> None:
        table = self.query_one("#source_list", DataTable)
        table.clear(columns=True)
        table.add_column("Folder")
        for folder in self.source_folders:
            table.add_row(_cell(str(folder)))
        if not self.source_folders:
            table.add_row(_cell("No folders yet - browse on the right", "dim"))
        self.query_one("#scan_sources", Button).disabled = not self.source_folders
        self.query_one("#remove_folder", Button).disabled = not self.source_folders
        count = len(self.source_folders)
        self.query_one("#source_selected").border_subtitle = (
            f"{count} selected" if count else ""
        )

    def _set_hint(self, message: str, style: str = "") -> None:
        self.query_one("#add_folder_hint", Label).update(Text(message, style=style))

    def add_folder(self, folder: Path) -> bool:
        """Add one folder to the scan list; returns False when rejected."""
        folder = folder.expanduser()
        if not folder.is_dir():
            self._set_hint(f"Not a folder: {folder}", "yellow")
            return False
        folder = folder.resolve()
        if folder in self.source_folders:
            self._set_hint(f"Already selected: {folder.name}", "dim")
            return False
        for existing in self.source_folders:
            if folder.is_relative_to(existing):
                self._set_hint(f"Already covered by {existing}", "dim")
                return False
        self.source_folders[:] = [
            existing
            for existing in self.source_folders
            if not existing.is_relative_to(folder)
        ]
        self.source_folders.append(folder)
        self._render_folders()
        self._set_hint(f"Added {folder}", "green")
        self.post_message(self.FoldersChanged())
        return True

    def _browse_to(self, folder: Path) -> None:
        tree = self.query_one("#source_tree", FolderTree)
        tree.path = folder
        self.query_one("#source_path", Input).value = str(folder)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[Any]) -> None:
        node = event.node
        if node.data is not None and hasattr(node.data, "path"):
            self.query_one("#source_path", Input).value = str(node.data.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "source_path":
            return
        folder = Path(event.value.strip()).expanduser()
        if folder.is_dir():
            self._browse_to(folder.resolve())
            self._set_hint("Press Add Folder to scan this folder", "dim")
        else:
            self._set_hint(f"Not a folder: {folder}", "yellow")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "add_folder":
            value = self.query_one("#source_path", Input).value.strip()
            if not value:
                self._set_hint("Highlight or type a folder first", "yellow")
                return
            self.add_folder(Path(value))
        elif event.button.id == "browse_up":
            tree = self.query_one("#source_tree", FolderTree)
            current = Path(tree.path).resolve()
            self._browse_to(current.parent)
        elif event.button.id == "remove_folder":
            table = self.query_one("#source_list", DataTable)
            row = table.cursor_row
            if 0 <= row < len(self.source_folders):
                removed = self.source_folders.pop(row)
                self._render_folders()
                self._set_hint(f"Removed {removed.name}", "dim")
                self.post_message(self.FoldersChanged())
        elif event.button.id == "scan_sources":
            self.app.action_scan()  # type: ignore[attr-defined]


class ScanResultsScreen(Static):
    """Screen showing scan results."""

    COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("Folder", 1.0),
        ("Tracks", 6),
        ("Runtime", 7),
        ("Warnings", 8),
    )

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.last_transaction_id: str | None = None
        self.source_roots: list[Path] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="scan_results_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.border_title = "Library"
        self._render_table()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_table)

    def set_audiobook_sets(self, audiobook_sets: list[AudiobookSet]) -> None:
        """Set the audiobook sets to display."""
        self.audiobook_sets = audiobook_sets
        self._render_table()

    def _render_table(self) -> None:
        table = self.query_one("#scan_results_table", DataTable)
        cursor_row = table.cursor_row
        table.clear(columns=True)
        widths = _column_widths(table, [spec for _, spec in self.COLUMNS])
        for (label, _), width in zip(self.COLUMNS, widths, strict=True):
            table.add_column(label, width=width)

        warning_total = 0
        for audiobook_set in self.audiobook_sets:
            warning_count = len(audiobook_set.warnings)
            warning_total += warning_count
            warnings = (
                _cell(f"⚠ {warning_count}", "yellow")
                if warning_count
                else _cell("✓", "green")
            )
            name = audiobook_set.display_name
            if audiobook_set.disc_count > 1:
                name = f"{name}  ({audiobook_set.disc_count} discs)"
            table.add_row(
                _cell(name),
                Text(str(audiobook_set.total_tracks), justify="right"),
                Text(_format_runtime(audiobook_set.total_duration), justify="right"),
                warnings,
            )
        if self.audiobook_sets:
            table.move_cursor(row=min(cursor_row, len(self.audiobook_sets) - 1))

        count = len(self.audiobook_sets)
        self.border_title = f"Library · {count} book{'s' if count != 1 else ''}"
        self.border_subtitle = f"{warning_total} warning(s)" if warning_total else ""


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
        self.review_store = ReviewDecisionStore(config_manager)
        self.sidecar_manager = SidecarManager()
        self._row_states: list[str] = []
        self._row_errors: list[str | None] = []

    MATCH_CONCURRENCY = 8

    class MatchProgress(Message):
        """Posted as each book finishes matching."""

        def __init__(self, done: int, total: int, matched: int) -> None:
            super().__init__()
            self.done = done
            self.total = total
            self.matched = matched

    COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("Audiobook", 1.0),
        ("Best Match", 1.3),
        ("Conf", 4),
        ("Flags", 6),
        ("Action", 7),
        ("Reasons", 0.7),
    )

    def compose(self) -> ComposeResult:
        yield DataTable(id="matches_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.border_title = "Matches"
        self.border_subtitle = "m to match · enter to review"
        self._render_table()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_table)

    def reset(self) -> None:
        """Forget match results after the library is rescanned."""
        self.audiobook_sets = []
        self._row_states = []
        self._row_errors = []
        self.border_title = "Matches"
        self.border_subtitle = "m to match · enter to review"
        self._render_table()

    @staticmethod
    def _format_reasons(candidate: MatchCandidate) -> str:
        """Render match reasons compactly for table display."""
        return "; ".join(candidate.match_reasons)

    @staticmethod
    def _flag_cell(candidate: MatchCandidate) -> Text:
        """Render the strongest review severity in the flags column."""
        severity = worst_flag_severity(candidate.review_flags)
        if severity is None:
            return Text("[none]", style="green")
        count = len(candidate.review_flags)
        style = {"info": "cyan", "warn": "yellow", "block": "bold red"}[severity]
        suffix = f" ({count})" if count > 1 else ""
        return Text(f"{severity.upper()}{suffix}", style=style)

    @staticmethod
    def _title_cell(audiobook_set: AudiobookSet) -> Text:
        """Highlight books that still need review or were skipped."""
        style = "green"
        if audiobook_set.needs_review:
            style = "bold yellow"
        elif audiobook_set.review_skipped:
            style = "yellow"
        return _cell(audiobook_set.raw_title_guess or "Unknown", style)

    @staticmethod
    def _best_match_cell(
        audiobook_set: AudiobookSet,
        candidates: list[MatchCandidate],
        error: str | None,
    ) -> Text:
        """Render the best current candidate or an error state."""
        if error:
            return _cell("No matches", "red")
        if not candidates:
            return _cell("No matches", "dim")
        best_match = MatchReviewScreen._displayed_candidate(audiobook_set)
        style = "yellow" if audiobook_set.needs_review else "green"
        return _cell(
            f"{best_match.identity.title} - {', '.join(best_match.identity.authors)}",
            style,
        )

    @staticmethod
    def _displayed_candidate(audiobook_set: AudiobookSet) -> MatchCandidate:
        """The candidate the user chose, falling back to the top-ranked one."""
        chosen = audiobook_set.chosen_identity
        if chosen is not None:
            for candidate in audiobook_set.provider_candidates:
                if candidate.identity == chosen:
                    return candidate
        return audiobook_set.provider_candidates[0]

    @staticmethod
    def _action_cell(audiobook_set: AudiobookSet, state: str) -> Text:
        """Render the current review state for a row."""
        if state in {"error", "unmatched"}:
            return Text("Manual", style="yellow")
        if audiobook_set.review_skipped:
            return Text("Skipped", style="yellow")
        if audiobook_set.needs_review:
            return Text("Review", style="bold yellow")
        if state == "accepted_saved":
            return Text("Saved", style="cyan")
        if state == "accepted_auto":
            return Text("Accept", style="green")
        return Text("Accept", style="green")

    def _render_table(self) -> None:
        """Refresh the matches table from current state."""
        table = self.query_one("#matches_table", DataTable)
        cursor_row = table.cursor_row
        table.clear(columns=True)
        table.cursor_type = "row"
        widths = _column_widths(table, [spec for _, spec in self.COLUMNS])
        for (label, _), width in zip(self.COLUMNS, widths, strict=True):
            table.add_column(label, width=width)

        for index, audiobook_set in enumerate(self.audiobook_sets):
            candidates = audiobook_set.provider_candidates
            error = self._row_errors[index] if index < len(self._row_errors) else None
            state = self._row_states[index] if index < len(self._row_states) else ""

            if candidates:
                best_match = self._displayed_candidate(audiobook_set)
                confidence = f"{best_match.confidence:.2f}"
                flags = self._flag_cell(best_match)
                reasons = self._format_reasons(best_match)
            else:
                confidence = "0.00"
                flags = Text("[none]", style="dim")
                reasons = ""

            table.add_row(
                self._title_cell(audiobook_set),
                self._best_match_cell(audiobook_set, candidates, error),
                confidence,
                flags,
                self._action_cell(audiobook_set, state),
                _cell(reasons, "dim"),
            )
        if self.audiobook_sets:
            table.move_cursor(row=min(cursor_row, len(self.audiobook_sets) - 1))

        if self._row_states:
            review = sum(
                1
                for book, state in zip(
                    self.audiobook_sets, self._row_states, strict=False
                )
                if book.needs_review or state in {"error", "unmatched"}
            )
            self.border_title = (
                f"Matches · {len(self.audiobook_sets) - review}/"
                f"{len(self.audiobook_sets)} ready"
            )
            self.border_subtitle = (
                f"{review} need review · c or enter to choose"
                if review
                else "enter to review a match"
            )

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
        if isinstance(self.provider, ProviderManager):
            coro = self.provider.find_matches_merged(audiobook_set)
        else:
            coro = self.provider.find_matches(audiobook_set)
        try:
            return await asyncio.wait_for(
                coro,
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MetadataError(
                f"Provider timed out after {timeout:.0f}s",
                details={"timeout": timeout, "kind": "timeout"},
                recoverable=True,
            ) from exc

    async def _rerun_matches_for_set(
        self, audiobook_set: AudiobookSet
    ) -> list[MatchCandidate]:
        """Re-run provider matching for just one audiobook set."""
        if isinstance(self.provider, ProviderManager):
            return await self.provider.find_matches_merged(audiobook_set)
        return await self._find_matches_with_timeout(audiobook_set)

    def _primary_provider(self) -> MetadataProvider | None:
        """Return the current primary provider for manual searches."""
        if isinstance(self.provider, ProviderManager):
            return self.provider.get_primary_provider()
        return self.provider

    async def _manual_search_candidates(
        self,
        audiobook_set: AudiobookSet,
        *,
        title: str | None,
        author: str | None,
        identifier: str | None,
    ) -> list[MatchCandidate]:
        """Search the primary provider directly using manual input."""
        primary = self._primary_provider()
        if primary is None:
            raise MetadataError("No metadata providers are enabled", recoverable=True)

        search_title = title or audiobook_set.raw_title_guess
        search_author = author or audiobook_set.author_guess
        identities = []

        if identifier:
            cleaned = identifier.strip()
            if cleaned and len(cleaned) == 10 and cleaned.isalnum():
                identity = await primary.get_by_id(cleaned)
                if identity is not None:
                    identities = [identity]
            if not identities:
                identities = await primary.search(
                    title=search_title,
                    author=search_author,
                    isbn=cleaned,
                    language=audiobook_set.language_guess,
                    limit=10,
                )
        else:
            identities = await primary.search(
                title=search_title,
                author=search_author,
                series=audiobook_set.series_guess,
                language=audiobook_set.language_guess,
                limit=10,
            )

        if not identities:
            return []

        candidates = primary._score_identities(audiobook_set, identities)
        if not isinstance(self.provider, ProviderManager):
            return candidates

        matcher = AdvancedMatcher()
        adjusted: list[MatchCandidate] = []
        for candidate in candidates:
            candidate = self.provider._apply_duration_check(
                candidate,
                audiobook_set.total_duration,
            )
            candidate.review_flags = detect_review_flags(
                audiobook_set, candidate, matcher
            )
            adjusted.append(candidate)
        return adjusted

    @staticmethod
    def _distinct_track_values(
        audiobook_set: AudiobookSet,
        field_name: str,
    ) -> list[str]:
        """Collect distinct non-empty embedded tag values for one field."""
        values: list[str] = []
        seen: set[str] = set()
        for track in audiobook_set.tracks:
            value = getattr(track.existing_tags, field_name, None)
            if not value:
                continue
            normalized = str(value).strip()
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            values.append(normalized)
        return values

    def _local_evidence_text(self, audiobook_set: AudiobookSet) -> str:
        """Render the local evidence shown beside a provider candidate."""
        lines = [
            f"Folder: {audiobook_set.display_name}",
            f"Source: {audiobook_set.source_path}",
            f"Tracks: {audiobook_set.total_tracks}",
            (
                f"Runtime: {audiobook_set.total_duration / 60.0:.1f} min"
                if audiobook_set.total_duration
                else "Runtime: [none]"
            ),
            f"Title Guess: {audiobook_set.raw_title_guess or '[none]'}",
            f"Author Guess: {audiobook_set.author_guess or '[none]'}",
            f"Narrator Guess: {audiobook_set.narrator_guess or '[none]'}",
            f"ASIN Guess: {audiobook_set.asin_guess or '[none]'}",
            f"ISBN Guess: {audiobook_set.isbn_guess or '[none]'}",
            "",
            "Embedded Tags:",
        ]

        tag_fields = [
            ("title", "Title"),
            ("artist", "Artist"),
            ("albumartist", "Album Artist"),
            ("album", "Album"),
            ("narrator", "Narrator"),
            ("series", "Series"),
            ("isbn", "ISBN"),
            ("asin", "ASIN"),
        ]
        found_tags = False
        for field_name, label in tag_fields:
            values = self._distinct_track_values(audiobook_set, field_name)
            if not values:
                continue
            found_tags = True
            lines.append(f"  {label}: {' | '.join(values)}")
        if not found_tags:
            lines.append("  [none]")

        sidecar = self.sidecar_manager.auto_detect_sidecar(audiobook_set.source_path)
        lines.extend(["", "Sidecar Evidence:"])
        if sidecar is None:
            lines.append("  [none]")
        else:
            lines.append(f"  Title: {sidecar.title}")
            authors_str = ", ".join(sidecar.authors) if sidecar.authors else "[none]"
            lines.append(f"  Author: {authors_str}")
            lines.append(f"  Narrator: {sidecar.narrator or '[none]'}")
            lines.append(
                f"  IDs: ASIN {sidecar.asin or '[none]'} | "
                f"ISBN {sidecar.isbn_13 or sidecar.isbn_10 or '[none]'}"
            )
            lines.append(
                f"  Series: {sidecar.series_name or '[none]'}"
                + (
                    f" #{sidecar.series_index}"
                    if sidecar.series_name and sidecar.series_index
                    else ""
                )
            )

        return "\n".join(lines)

    async def open_candidate_picker(self, row_index: int) -> None:
        """Open the manual candidate picker for one review row."""
        if row_index < 0 or row_index >= len(self.audiobook_sets):
            return

        audiobook_set = self.audiobook_sets[row_index]
        screen = CandidateDetailScreen(
            audiobook_set,
            list(audiobook_set.provider_candidates),
            local_evidence=self._local_evidence_text(audiobook_set),
            rerun_matches=lambda: self._rerun_matches_for_set(audiobook_set),
            manual_search=lambda query: self._manual_search_candidates(
                audiobook_set,
                title=query.get("title"),
                author=query.get("author"),
                identifier=query.get("identifier"),
            ),
        )
        result = await self.app.push_screen(screen, wait_for_dismiss=True)

        if result is None:
            return

        audiobook_set.provider_candidates = list(screen.candidates)
        if result == -1:
            skip_set_from_plans(audiobook_set)
            self.review_store.set_skip(audiobook_set)
            self._row_states[row_index] = "skipped"
            self._row_errors[row_index] = None
        elif 0 <= result < len(screen.candidates):
            selected = screen.candidates[result]
            accept_candidate_for_set(audiobook_set, selected)
            self.review_store.set_accept(
                audiobook_set,
                selected,
                candidate_index=result + 1,
            )
            self._row_states[row_index] = "accepted_saved"
            self._row_errors[row_index] = None
        else:
            return

        self._render_table()

    SAFE_ACCEPT_FLOOR = 0.75

    def accept_safe_candidates(self) -> int:
        """Accept every top candidate that has no blocking flag and decent confidence.

        Returns how many books were accepted. Nothing touches the disk; the
        choices only feed the next preview and are saved like manual picks.
        """
        accepted = 0
        for index, audiobook_set in enumerate(self.audiobook_sets):
            if not audiobook_set.needs_review or not audiobook_set.provider_candidates:
                continue
            best = audiobook_set.provider_candidates[0]
            if best.confidence < self.SAFE_ACCEPT_FLOOR:
                continue
            if has_flag_at_or_above(best.review_flags, "block"):
                continue
            accept_candidate_for_set(audiobook_set, best)
            self.review_store.set_accept(audiobook_set, best, candidate_index=1)
            if index < len(self._row_states):
                self._row_states[index] = "accepted_saved"
                self._row_errors[index] = None
            accepted += 1
        if accepted:
            self._render_table()
        return accepted

    def _start_candidate_picker(self, row_index: int) -> None:
        """Run the picker in a worker so it can wait for the modal to close."""
        self.run_worker(
            self.open_candidate_picker(row_index),
            group="candidate-picker",
            exclusive=True,
        )

    async def open_focused_candidate_picker(self) -> None:
        """Open the picker for the currently highlighted match row."""
        table = self.query_one("#matches_table", DataTable)
        self._start_candidate_picker(table.cursor_row)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the picker when a match row is selected."""
        if event.data_table.id != "matches_table":
            return
        self._start_candidate_picker(event.cursor_row)

    async def find_matches(self, audiobook_sets: list[AudiobookSet]) -> MatchSummary:
        """Find matches for audiobook sets using merged multi-provider results."""
        self.audiobook_sets = audiobook_sets
        self._row_states = []
        self._row_errors = []
        summary = MatchSummary()
        matching = self.config_manager.load_config().matching

        # Query a few books at a time: firing hundreds of lookups at once gets
        # throttled by the providers and turns into timeouts.
        limit = asyncio.Semaphore(self.MATCH_CONCURRENCY)
        total = len(audiobook_sets)
        done = 0
        matched = 0

        async def match_one(audiobook_set: AudiobookSet) -> list[MatchCandidate]:
            nonlocal done, matched
            async with limit:
                try:
                    if isinstance(self.provider, ProviderManager):
                        result = await self.provider.find_matches_merged(audiobook_set)
                    else:
                        result = await self._find_matches_with_timeout(audiobook_set)
                    if result:
                        matched += 1
                    return result
                finally:
                    done += 1
                    self.post_message(self.MatchProgress(done, total, matched))

        results = await asyncio.gather(
            *(match_one(audiobook_set) for audiobook_set in audiobook_sets),
            return_exceptions=True,
        )

        for audiobook_set, result in zip(audiobook_sets, results, strict=False):
            state = "unmatched"
            error_text: str | None = None

            if isinstance(result, BaseException):
                summary.failed_count += 1
                summary.unmatched_count += 1
                audiobook_set.provider_candidates = []
                saved = self.review_store.apply_saved_decision(audiobook_set, [])
                if saved is not None and saved[0] == "skip":
                    state = "skipped"
                else:
                    mark_set_needs_review(audiobook_set, str(result))
                    state = "error"
                error_text = str(result)
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
                    summary.matched_count += 1
                    state, _selected = self.review_store.resolve_match_state(
                        audiobook_set,
                        candidates,
                        matching,
                    )
                else:
                    summary.unmatched_count += 1
                    mark_set_needs_review(audiobook_set, "No matches found")
                    state = "unmatched"

            self._row_states.append(state)
            self._row_errors.append(error_text)

        self._render_table()
        return summary


class TextDialog(ModalScreen[None]):
    """Scrollable read-only dialog for help, diffs, and warning lists."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, title: str, body: str | Text) -> None:
        super().__init__()
        self.dialog_title = title
        self.body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="candidate-modal text-dialog") as modal:
            modal.border_title = self.dialog_title
            modal.border_subtitle = "esc to close"
            yield Static(self.body, markup=False, id="text_dialog_body")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class ManualSearchScreen(ModalScreen[dict[str, str] | None]):
    """Prompt for a manual provider query."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "submit", "Search", show=False),
    ]

    def __init__(self, audiobook_set: AudiobookSet) -> None:
        super().__init__()
        self.audiobook_set = audiobook_set

    def compose(self) -> ComposeResult:
        with Container(classes="candidate-modal") as modal:
            modal.border_title = "Manual Search"
            modal.border_subtitle = "enter search · esc cancel"
            yield Label(
                str(self.audiobook_set.source_path), classes="modal-path", markup=False
            )
            with Horizontal(classes="form-row"):
                yield Label("Title")
                yield Input(
                    value=self.audiobook_set.raw_title_guess or "",
                    id="manual_title",
                )
            with Horizontal(classes="form-row"):
                yield Label("Author")
                yield Input(
                    value=self.audiobook_set.author_guess or "",
                    id="manual_author",
                )
            with Horizontal(classes="form-row"):
                yield Label("ASIN / ISBN")
                yield Input(
                    value=(
                        self.audiobook_set.asin_guess
                        or self.audiobook_set.isbn_guess
                        or ""
                    ),
                    id="manual_identifier",
                )
            with Horizontal(classes="button-row"):
                yield Button(
                    "Search", id="manual_search_submit", variant="primary", compact=True
                )
                yield Button("Cancel", id="manual_search_cancel", compact=True)

    def on_mount(self) -> None:
        """Focus the title input on open."""
        self.query_one("#manual_title", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle search/cancel buttons."""
        if event.button.id == "manual_search_submit":
            self.action_submit()
        elif event.button.id == "manual_search_cancel":
            self.action_cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in any field runs the search."""
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        """Dismiss the modal with the requested search query."""
        self.dismiss(
            {
                "title": self.query_one("#manual_title", Input).value.strip(),
                "author": self.query_one("#manual_author", Input).value.strip(),
                "identifier": self.query_one("#manual_identifier", Input).value.strip(),
            }
        )

    def action_cancel(self) -> None:
        """Dismiss without changing the current review state."""
        self.dismiss(None)


class CandidateDetailScreen(ModalScreen[int | None]):
    """Manual candidate picker for review-required matches."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=True),
        Binding("enter", "accept_selected", "Choose", show=True),
        Binding("r", "rerun_match", "Rerun", show=True),
        Binding("/", "manual_search", "Search", show=True),
        Binding("s", "skip_set", "Skip", show=True),
    ]

    def __init__(
        self,
        audiobook_set: AudiobookSet,
        candidates: list[MatchCandidate],
        *,
        local_evidence: str,
        rerun_matches: Any,
        manual_search: Any,
    ) -> None:
        super().__init__()
        self.audiobook_set = audiobook_set
        self.candidates = list(candidates)
        self.local_evidence = local_evidence
        self._rerun_matches = rerun_matches
        self._manual_search = manual_search
        self.status_message = (
            "Highlight a candidate, press Enter to accept, or s to skip."
        )

    def compose(self) -> ComposeResult:
        with Container(classes="candidate-modal") as modal:
            modal.border_title = "Review Candidates"
            modal.border_subtitle = (
                "enter choose · r rerun · / search · s skip · esc close"
            )
            yield Label(
                str(self.audiobook_set.source_path), classes="modal-path", markup=False
            )
            yield DataTable(id="candidate_table", zebra_stripes=True)
            yield Static(self.status_message, id="candidate_status", markup=False)
            with Horizontal(classes="candidate-columns"):
                with VerticalScroll(id="candidate_local", classes="pane"):
                    yield Static(self.local_evidence, markup=False)
                with VerticalScroll(id="candidate_detail", classes="pane"):
                    yield Static("", markup=False, id="candidate_detail_text")

    def on_mount(self) -> None:
        """Populate the candidate table and show the first candidate."""
        table = self.query_one("#candidate_table", DataTable)
        table.cursor_type = "row"
        self.query_one("#candidate_local").border_title = "On Disk"
        self.query_one("#candidate_detail").border_title = "Candidate"
        self._refresh_candidates()
        table.focus()

    CANDIDATE_COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("#", 2),
        ("Title", 1.4),
        ("Author", 1.0),
        ("Confidence", 10),
        ("Flags", 6),
    )

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_candidates, False)

    def _refresh_candidates(self, reset_cursor: bool = True) -> None:
        """Render the current candidate list and detail pane."""
        table = self.query_one("#candidate_table", DataTable)
        cursor_row = table.cursor_row
        table.clear(columns=True)
        widths = _column_widths(table, [spec for _, spec in self.CANDIDATE_COLUMNS])
        for (label, _), width in zip(self.CANDIDATE_COLUMNS, widths, strict=True):
            table.add_column(label, width=width)

        if not self.candidates:
            table.add_row("-", _cell("No candidates", "dim"), "", "", "")
            self.query_one("#candidate_detail_text", Static).update(
                "No candidates yet. Press / for manual search or r to rerun."
            )
            self.query_one("#candidate_status", Static).update(self.status_message)
            return

        for index, candidate in enumerate(self.candidates, start=1):
            authors = ", ".join(candidate.identity.authors) or "Unknown author"
            severity = worst_flag_severity(candidate.review_flags)
            flags = severity.upper() if severity else "[none]"
            table.add_row(
                str(index),
                _cell(candidate.identity.title),
                _cell(authors),
                f"{candidate.confidence:.2f}",
                Text(flags),
            )

        row = 0 if reset_cursor else min(max(cursor_row, 0), len(self.candidates) - 1)
        table.move_cursor(row=row)
        self._update_candidate_detail(row)
        self.query_one("#candidate_status", Static).update(self.status_message)

    @staticmethod
    def _provider_labels(candidate: MatchCandidate) -> list[str]:
        """Extract merged provider labels for a candidate."""
        providers = candidate.identity.raw_data.get(
            "providers", [candidate.identity.provider]
        )
        if isinstance(providers, list):
            return [str(provider) for provider in providers]
        return [str(providers)]

    def _candidate_detail_text(self, candidate: MatchCandidate) -> str:
        """Render one candidate with full details and review flags."""
        identity = candidate.identity
        runtime_text = (
            f"{identity.runtime_minutes} min"
            if identity.runtime_minutes is not None
            else "[none]"
        )
        ids: list[str] = []
        if identity.asin:
            ids.append(f"ASIN {identity.asin}")
        if identity.isbn_13:
            ids.append(f"ISBN-13 {identity.isbn_13}")
        elif identity.isbn_10:
            ids.append(f"ISBN-10 {identity.isbn_10}")

        lines = [
            f"Title: {identity.title}",
            f"Author: {', '.join(identity.authors) or '[none]'}",
            f"Narrator: {identity.narrator or '[none]'}",
            f"IDs: {' | '.join(ids) if ids else '[none]'}",
            f"Runtime: {runtime_text}",
            (
                f"Series: {identity.series_name}"
                + (f" #{identity.series_index}" if identity.series_index else "")
                if identity.series_name
                else "Series: [none]"
            ),
            f"Year: {identity.year or '[none]'}",
            f"Providers: {', '.join(self._provider_labels(candidate))}",
            f"Confidence: {candidate.confidence:.2f}"
            f" ({candidate.confidence_level.value})",
            f"Reasons: {'; '.join(candidate.match_reasons) or '[none]'}",
            "Review Flags:",
        ]
        if not candidate.review_flags:
            lines.append("  [none]")
        else:
            for flag in candidate.review_flags:
                detail_parts = [flag.message]
                if flag.local_value is not None:
                    detail_parts.append(f"local={flag.local_value}")
                if flag.provider_value is not None:
                    detail_parts.append(f"provider={flag.provider_value}")
                lines.append(
                    f"  {flag.severity.upper()} {flag.kind.value}: "
                    + " | ".join(detail_parts)
                )
        return "\n".join(lines)

    def _selected_index(self) -> int | None:
        """Return the highlighted candidate index, if any."""
        if not self.candidates:
            return None
        table = self.query_one("#candidate_table", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.candidates):
            return None
        return table.cursor_row

    def _update_candidate_detail(self, index: int | None) -> None:
        """Refresh the right-hand detail pane from the selected candidate."""
        detail = self.query_one("#candidate_detail_text", Static)
        if index is None or index < 0 or index >= len(self.candidates):
            detail.update("No candidate selected.")
            return
        detail.update(self._candidate_detail_text(self.candidates[index]))

    async def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """Update details as the highlighted candidate changes."""
        if event.data_table.id != "candidate_table":
            return
        self._update_candidate_detail(event.cursor_row)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Accept the selected candidate on row activation."""
        if event.data_table.id != "candidate_table":
            return
        self.action_accept_selected()

    def action_accept_selected(self) -> None:
        """Choose the highlighted candidate."""
        selected_index = self._selected_index()
        if selected_index is None:
            self.status_message = "No candidate is selected yet."
            self.query_one("#candidate_status", Static).update(self.status_message)
            return
        self.dismiss(selected_index)

    def action_skip_set(self) -> None:
        """Skip the current audiobook set from future plans."""
        self.dismiss(-1)

    def action_cancel(self) -> None:
        """Close the modal without changing the match selection."""
        self.dismiss(None)

    async def action_rerun_match(self) -> None:
        """Re-run provider matching for this set only."""
        self.status_message = "Refreshing candidates..."
        self.query_one("#candidate_status", Static).update(self.status_message)
        try:
            self.candidates = await self._rerun_matches()
            self.status_message = (
                "Candidates refreshed."
                if self.candidates
                else "No candidates returned. Try manual search."
            )
        except Exception as exc:
            self.status_message = f"Refresh failed: {exc}"
        self._refresh_candidates()

    def action_manual_search(self) -> None:
        """Prompt for a manual search and replace the candidate list."""
        self.run_worker(
            self._manual_search_flow(), group="manual-search", exclusive=True
        )

    async def _manual_search_flow(self) -> None:
        query = await self.app.push_screen(
            ManualSearchScreen(self.audiobook_set),
            wait_for_dismiss=True,
        )
        if query is None:
            return

        self.status_message = "Searching primary provider..."
        self.query_one("#candidate_status", Static).update(self.status_message)
        try:
            self.candidates = await self._manual_search(query)
            self.status_message = (
                "Manual search returned candidates."
                if self.candidates
                else "Manual search returned no candidates."
            )
        except Exception as exc:
            self.status_message = f"Manual search failed: {exc}"
        self._refresh_candidates()


class PreviewScreen(Static):
    """Screen for previewing changes."""

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.last_transaction_id: str | None = None
        self.last_plan: RenamePlan | None = None
        self.source_roots: list[Path] = []
        self._columns: list[tuple[str, int | float]] = list(self.PREVIEW_COLUMNS)
        self._rows: list[tuple[str, str, str]] = []

    PREVIEW_COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("Current Name", 1.0),
        ("Proposed Path", 1.6),
        ("Status", 15),
    )
    RESULT_COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("Operation", 1.4),
        ("Result", 10),
        ("Details", 1.0),
    )

    def compose(self) -> ComposeResult:
        yield DataTable(id="preview_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.border_title = "Rename Preview"
        self.border_subtitle = "p to preview · d for diff"
        self._render_table()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_table)

    def reset(self) -> None:
        """Forget the previous plan after the library is rescanned."""
        self.audiobook_sets = []
        self.last_plan = None
        self.border_title = "Rename Preview"
        self.border_subtitle = "p to preview · d for diff"
        self._show(self.PREVIEW_COLUMNS, [])

    def _show(
        self,
        columns: Sequence[tuple[str, int | float]],
        rows: list[tuple[str, str, str]],
    ) -> None:
        self._columns = list(columns)
        self._rows = rows
        self._render_table()

    _STATUS_STYLES = {
        "✓": "green",
        "→": "dim",
        "✎": "cyan",
        "Review": "yellow",
        "Blocked": "yellow",
        "Failed": "bold red",
    }

    def _render_table(self) -> None:
        table = self.query_one("#preview_table", DataTable)
        table.clear(columns=True)
        widths = _column_widths(table, [spec for _, spec in self._columns])
        for (label, _), width in zip(self._columns, widths, strict=True):
            table.add_column(label, width=width)
        for first, second, status in self._rows:
            style = next(
                (
                    value
                    for prefix, value in self._STATUS_STYLES.items()
                    if status.startswith(prefix)
                ),
                "",
            )
            table.add_row(_cell(first), _cell(second), _cell(status, style))

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

        rows: list[tuple[str, str, str]] = []

        if not audiobook_sets:
            self._show(self.PREVIEW_COLUMNS, rows)
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
        self.last_plan = plan
        operations_by_source = {
            operation.old_path: operation for operation in plan.operations
        }

        for audiobook_set in audiobook_sets:
            for track in audiobook_set.tracks:
                current_name = track.src_path.name
                operation = operations_by_source.get(track.src_path)
                if operation is not None:
                    proposed_name = self._format_preview_path(
                        operation.new_path, common_root
                    )
                    status = "✓ Ready"
                elif (
                    audiobook_set.chosen_identity is None
                    and audiobook_set.review_reason
                ):
                    proposed_name = audiobook_set.review_reason
                    status = "Review required"
                else:
                    proposed_name = self._format_preview_path(
                        track.src_path, common_root
                    )
                    status = "→ No change"
                rows.append((current_name, proposed_name, status))

        for sidecar_path in plan.planned_sidecars:
            rows.append(
                (
                    sidecar_path.name,
                    self._format_preview_path(sidecar_path, common_root),
                    "✎ Sidecar",
                )
            )

        self._show(self.PREVIEW_COLUMNS, rows)
        changes = len(plan.operations) + len(plan.planned_sidecars)
        self.border_title = f"Rename Preview · {changes} change(s)"
        self.border_subtitle = (
            f"{len(plan.skipped)} skipped for review · d for diff"
            if plan.skipped
            else "a to apply · d for diff"
        )

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
            self.last_plan = plan
            has_pending_changes = bool(plan.operations or plan.planned_sidecars)
            if not has_pending_changes and plan.skipped:
                self._show(
                    self.RESULT_COLUMNS,
                    [
                        (
                            "Apply Changes",
                            "Blocked",
                            f"{len(plan.skipped)} book(s) still need review",
                        )
                    ],
                )
                return False
            if not has_pending_changes:
                return True
            if plan.conflicts:
                raise ValueError("; ".join(plan.conflicts))

            # Use a short-lived executor for apply operations. Keeping this work
            # on asyncio's global executor can hold the TUI event loop open during
            # shutdown after an apply completes.
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="bookbot-apply",
            ) as executor:
                await loop.run_in_executor(
                    executor,
                    partial(transaction_manager.execute_plan, plan, dry_run=False),
                )

            transaction_id = plan.applied_transaction_id or "unknown"
            self.last_transaction_id = transaction_id

            # Update table to show completion
            self._show(
                (*self.RESULT_COLUMNS[:2], ("Transaction ID", 1.0)),
                [
                    (
                        (
                            f"Applied {len(plan.operations)} rename(s), "
                            f"{len(plan.planned_sidecars)} sidecar write(s)"
                        ),
                        "✓ Success",
                        transaction_id[:8] + "...",
                    )
                ],
            )
            self.border_subtitle = "u to undo"

            return True

        except Exception as e:
            # Show error in table
            self._show(
                (*self.RESULT_COLUMNS[:2], ("Error", 1.0)),
                [("Apply Changes", "Failed", str(e))],
            )
            return False


class ConversionScreen(Static):
    """Screen for M4B conversion options."""

    def __init__(self, config_manager: ConfigManager, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_manager = config_manager
        self.audiobook_sets: list[AudiobookSet] = []
        self.conversion_in_progress = False
        self._statuses: list[str] = []

    class ConversionComplete(Message):
        """Message sent when conversion is complete."""

        def __init__(self, success: bool, message: str):
            super().__init__()
            self.success = success
            self.message = message

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="convert_settings", classes="pane"):
                with Horizontal(classes="form-row"):
                    yield Label("Output folder")
                    yield Input(placeholder="./converted", id="output_dir")
                with Horizontal(classes="form-row"):
                    yield Label("Audio quality")
                    yield Select(
                        [
                            ("High Quality (192k)", "192k"),
                            ("Standard (128k)", "128k"),
                            ("Compressed (64k)", "64k"),
                            ("VBR High", "vbr5"),
                            ("VBR Standard", "vbr4"),
                        ],
                        value="128k",
                        allow_blank=False,
                        id="quality_select",
                    )
                with Horizontal(classes="checkbox-row"):
                    yield Checkbox("Normalize loudness", False, id="normalize_check")
                    yield Checkbox("Embed cover art", True, id="cover_art_check")
                with Horizontal(classes="button-row"):
                    yield Button(
                        "Start Conversion",
                        id="start_conversion",
                        variant="primary",
                        disabled=True,
                        compact=True,
                    )
                    yield Button(
                        "Cancel", id="cancel_conversion", disabled=True, compact=True
                    )
            with Vertical(id="convert_queue", classes="pane"):
                yield DataTable(id="conversion_results", cursor_type="row")
                yield ProgressBar(id="conversion_progress", show_eta=False)
                yield Label(
                    "Scan a library to fill the queue",
                    id="conversion_status",
                    markup=False,
                )

    def on_mount(self) -> None:
        self.query_one("#convert_settings").border_title = "M4B Settings"
        queue = self.query_one("#convert_queue")
        queue.border_title = "Conversion Queue"
        self.query_one("#conversion_progress", ProgressBar).display = False
        self._render_queue()

    QUEUE_COLUMNS: tuple[tuple[str, int | float], ...] = (
        ("Audiobook", 1.0),
        ("Tracks", 6),
        ("Output File", 1.2),
        ("Status", 12),
    )

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_queue)

    def _output_name(self, audiobook_set: AudiobookSet) -> str:
        if audiobook_set.chosen_identity:
            title = audiobook_set.chosen_identity.title
            authors = ", ".join(audiobook_set.chosen_identity.authors)
            return f"{authors} - {title}.m4b"
        return f"{audiobook_set.source_path.name}.m4b"

    def _set_row_status(self, index: int, status: str) -> None:
        if 0 <= index < len(self._statuses):
            self._statuses[index] = status
            self._render_queue()

    _STATUS_STYLES = {
        "Pending": "dim",
        "Converting": "cyan",
        "Complete": "green",
        "Failed": "bold red",
    }

    def _render_queue(self) -> None:
        table = self.query_one("#conversion_results", DataTable)
        cursor_row = table.cursor_row
        table.clear(columns=True)
        widths = _column_widths(table, [spec for _, spec in self.QUEUE_COLUMNS])
        for (label, _), width in zip(self.QUEUE_COLUMNS, widths, strict=True):
            table.add_column(label, width=width)
        for audiobook_set, status in zip(
            self.audiobook_sets, self._statuses, strict=False
        ):
            style = self._STATUS_STYLES.get(status.split(":")[0], "red")
            table.add_row(
                _cell(audiobook_set.raw_title_guess or "Unknown"),
                Text(str(audiobook_set.total_tracks), justify="right"),
                _cell(self._output_name(audiobook_set), "dim"),
                _cell(status, style),
            )
        if self.audiobook_sets:
            table.move_cursor(row=min(max(cursor_row, 0), len(self.audiobook_sets) - 1))

    def set_audiobook_sets(self, audiobook_sets: list[AudiobookSet]) -> None:
        """Set the audiobook sets for conversion."""
        self.audiobook_sets = audiobook_sets
        self._statuses = ["Pending"] * len(audiobook_sets)
        self._render_queue()

        # Enable conversion button if we have audiobooks
        start_button = self.query_one("#start_conversion", Button)
        start_button.disabled = len(audiobook_sets) == 0
        count = len(audiobook_sets)
        self.query_one(
            "#convert_queue"
        ).border_title = f"Conversion Queue · {count} book{'s' if count != 1 else ''}"
        self.update_status("Ready to convert" if count else "Nothing to convert")

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
        if not self.audiobook_sets:
            self.update_status("Nothing to convert - scan a library first")
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
        progress_bar.display = True

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

            # Convert each audiobook set
            for i, audiobook_set in enumerate(self.audiobook_sets):
                if not self.conversion_in_progress:  # Check for cancellation
                    break

                self.update_status(
                    f"Converting {audiobook_set.raw_title_guess or 'Unknown'}..."
                )
                self._set_row_status(i, "Converting")

                try:
                    success = await pipeline.convert_audiobook_set(
                        audiobook_set, conv_config
                    )
                    self._set_row_status(i, "Complete" if success else "Failed")
                except Exception as e:
                    self._set_row_status(i, f"Error: {e}")

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
