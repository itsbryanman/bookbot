"""TUI screens for BookBot."""

import asyncio
import os
from asyncio import to_thread
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
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
from ..core.matching import AdvancedMatcher
from ..core.models import AudiobookSet, MatchCandidate, RenamePlan
from ..core.operations import TransactionManager
from ..core.planning import PlanBuilder
from ..core.review import (
    accept_candidate_for_set,
    detect_review_flags,
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
            Label("Protected Audiobook Access", classes="section-title"),
            Static(
                "Authenticate with your Audible account to access books you own. "
                "AAX output is decrypted to a temporary file, decoded for integrity, "
                "and only then published. AAXC requires its matching voucher and "
                "content key.",
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
        self.review_store = ReviewDecisionStore(config_manager)
        self.sidecar_manager = SidecarManager()
        self._row_states: list[str] = []
        self._row_errors: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Label("Metadata Matches", classes="section-title")
        yield DataTable(id="matches_table")

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
        return Text(audiobook_set.raw_title_guess or "Unknown", style=style)

    @staticmethod
    def _best_match_cell(
        audiobook_set: AudiobookSet,
        candidates: list[MatchCandidate],
        error: str | None,
    ) -> Text:
        """Render the best current candidate or an error state."""
        if error:
            return Text("No matches", style="red")
        if not candidates:
            return Text("No matches", style="dim")
        best_match = candidates[0]
        style = "yellow" if audiobook_set.needs_review else "green"
        return Text(
            f"{best_match.identity.title} - {', '.join(best_match.identity.authors)}",
            style=style,
        )

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
        table.clear(columns=True)
        table.cursor_type = "row"
        table.add_columns(
            "Audiobook",
            "Best Match",
            "Confidence",
            "Flags",
            "Action",
            "Reasons",
        )

        for index, audiobook_set in enumerate(self.audiobook_sets):
            candidates = audiobook_set.provider_candidates
            error = self._row_errors[index] if index < len(self._row_errors) else None
            state = self._row_states[index] if index < len(self._row_states) else ""

            if candidates:
                best_match = candidates[0]
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
                reasons,
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
            f"Folder: {audiobook_set.source_path.name}",
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

    async def open_focused_candidate_picker(self) -> None:
        """Open the picker for the currently highlighted match row."""
        table = self.query_one("#matches_table", DataTable)
        await self.open_candidate_picker(table.cursor_row)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the picker when a match row is selected."""
        if event.data_table.id != "matches_table":
            return
        await self.open_candidate_picker(event.cursor_row)

    async def find_matches(self, audiobook_sets: list[AudiobookSet]) -> MatchSummary:
        """Find matches for audiobook sets using merged multi-provider results."""
        self.audiobook_sets = audiobook_sets
        self._row_states = []
        self._row_errors = []
        summary = MatchSummary()
        matching = self.config_manager.load_config().matching

        if isinstance(self.provider, ProviderManager):
            match_tasks = [self.provider.find_matches_merged(a) for a in audiobook_sets]
        else:
            match_tasks = [self._find_matches_with_timeout(a) for a in audiobook_sets]
        results = await asyncio.gather(*match_tasks, return_exceptions=True)

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
        with Container(classes="candidate-modal"):
            yield Label("Manual Search", classes="section-title")
            yield Label(str(self.audiobook_set.source_path))
            with Horizontal():
                yield Label("Title")
                yield Input(
                    value=self.audiobook_set.raw_title_guess or "",
                    id="manual_title",
                )
            with Horizontal():
                yield Label("Author")
                yield Input(
                    value=self.audiobook_set.author_guess or "",
                    id="manual_author",
                )
            with Horizontal():
                yield Label("ASIN / ISBN")
                yield Input(
                    value=(
                        self.audiobook_set.asin_guess
                        or self.audiobook_set.isbn_guess
                        or ""
                    ),
                    id="manual_identifier",
                )
            with Horizontal():
                yield Button("Search", id="manual_search_submit", variant="primary")
                yield Button("Cancel", id="manual_search_cancel")

    def on_mount(self) -> None:
        """Focus the title input on open."""
        self.query_one("#manual_title", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle search/cancel buttons."""
        if event.button.id == "manual_search_submit":
            self.action_submit()
        elif event.button.id == "manual_search_cancel":
            self.action_cancel()

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
        with Container(classes="candidate-modal"):
            yield Label("Review Candidates", classes="section-title")
            yield Label(str(self.audiobook_set.source_path))
            yield DataTable(id="candidate_table")
            yield Static(self.status_message, id="candidate_status")
            with Horizontal(classes="candidate-columns"):
                yield Static(self.local_evidence, id="candidate_local")
                yield Static("", id="candidate_detail")

    def on_mount(self) -> None:
        """Populate the candidate table and show the first candidate."""
        table = self.query_one("#candidate_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("#", "Title", "Author", "Confidence", "Flags")
        self._refresh_candidates()
        table.focus()

    def _refresh_candidates(self) -> None:
        """Render the current candidate list and detail pane."""
        table = self.query_one("#candidate_table", DataTable)
        table.clear(columns=False)

        if not self.candidates:
            table.add_row("-", "No candidates", "", "", "")
            self.query_one("#candidate_detail", Static).update(
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
                candidate.identity.title,
                authors,
                f"{candidate.confidence:.2f}",
                flags,
            )

        table.move_cursor(row=0)
        self._update_candidate_detail(0)
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
        detail = self.query_one("#candidate_detail", Static)
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

    async def action_manual_search(self) -> None:
        """Prompt for a manual search and replace the candidate list."""
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
                table.add_row(current_name, proposed_name, status)

        for sidecar_path in plan.planned_sidecars:
            table.add_row(
                sidecar_path.name,
                self._format_preview_path(sidecar_path, common_root),
                "✎ Sidecar",
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
                table = self.query_one("#preview_table", DataTable)
                table.clear(columns=True)
                table.add_columns("Operation", "Result", "Details")
                table.add_row(
                    "Apply Changes",
                    "Blocked",
                    f"{len(plan.skipped)} book(s) still need review",
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
            table = self.query_one("#preview_table", DataTable)
            table.clear(columns=True)
            table.add_columns("Operation", "Result", "Transaction ID")
            table.add_row(
                (
                    f"Applied {len(plan.operations)} rename(s), "
                    f"{len(plan.planned_sidecars)} sidecar write(s)"
                ),
                "✓ Success",
                transaction_id[:8] + "...",
            )

            return True

        except Exception as e:
            # Show error in table
            table = self.query_one("#preview_table", DataTable)
            table.clear(columns=True)
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
                    table.update_cell(self._result_row_keys[i], "Status", "Converting")

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
