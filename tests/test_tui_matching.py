"""Tests for TUI matching status reporting and provider teardown."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Label, Static, TabbedContent

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    ReviewFlag,
    ReviewFlagKind,
    Track,
    TrackStatus,
)
from bookbot.core.planning import PlanBuilder
from bookbot.providers.base import MetadataProvider
from bookbot.providers.manager import ProviderManager
from bookbot.tui.app import BookBotApp
from bookbot.tui.screens import MatchReviewScreen, MatchSummary


class MatchHarness(App[None]):
    """Minimal Textual app for exercising the match review screen."""

    def __init__(
        self,
        config_manager: ConfigManager,
        provider: MetadataProvider | ProviderManager,
    ) -> None:
        super().__init__()
        self.config_manager = config_manager
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield MatchReviewScreen(self.config_manager, self.provider, id="match")


class BehaviorProvider(MetadataProvider):
    """Provider stub with per-title scripted behaviors."""

    def __init__(self, behavior: dict[str, object] | None = None) -> None:
        super().__init__("behavior")
        self.behavior = behavior or {}
        self.close_calls = 0

    async def search(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        series: str | None = None,
        isbn: str | None = None,
        year: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[ProviderIdentity]:
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None

    async def find_matches(
        self, audiobook_set: AudiobookSet, limit: int = 10
    ) -> list[MatchCandidate]:
        result = self.behavior.get(audiobook_set.raw_title_guess or "", [])
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    async def close(self) -> None:
        self.close_calls += 1


class HangingProvider(MetadataProvider):
    """Provider stub that exceeds the configured timeout."""

    def __init__(self) -> None:
        super().__init__("hanging")

    async def search(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        series: str | None = None,
        isbn: str | None = None,
        year: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> list[ProviderIdentity]:
        await asyncio.sleep(1)
        return []

    async def get_by_id(self, external_id: str) -> ProviderIdentity | None:
        return None


def _make_audiobook(title: str) -> AudiobookSet:
    slug = title.lower().replace(" ", "_")
    return AudiobookSet(
        source_path=Path(f"/tmp/{slug}"),
        raw_title_guess=title,
        total_tracks=1,
    )


def _make_tracked_audiobook(source_path: Path, title: str) -> AudiobookSet:
    track_path = source_path / "01.mp3"
    source_path.mkdir(parents=True, exist_ok=True)
    track_path.touch()
    track = Track(
        src_path=track_path,
        disc=1,
        track_index=1,
        file_size=0,
        audio_format=AudioFormat.MP3,
        status=TrackStatus.VALID,
    )
    return AudiobookSet(
        source_path=source_path,
        raw_title_guess=title,
        total_tracks=1,
        tracks=[track],
    )


def _make_candidate(
    title: str, author: str, confidence: float = 0.91
) -> MatchCandidate:
    return MatchCandidate(
        identity=ProviderIdentity(
            provider="behavior",
            external_id=title.lower().replace(" ", "-"),
            title=title,
            authors=[author],
        ),
        confidence=confidence,
        confidence_level=MatchConfidence.HIGH,
        match_reasons=["stub"],
    )


def _row_values(table: DataTable, index: int) -> list[str]:
    row = table.get_row_at(index)
    values = []
    for value in row:
        if isinstance(value, Text):
            values.append(value.plain)
        else:
            values.append(str(value))
    return values


@pytest.mark.asyncio
async def test_match_review_screen_reports_failures_as_unmatched_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = [_make_audiobook("Book One"), _make_audiobook("Book Two")]
    provider = BehaviorProvider(
        {
            "Book One": RuntimeError("provider offline"),
            "Book Two": RuntimeError("request timed out"),
        }
    )
    warnings: list[dict[str, str]] = []

    def fake_warning(message: str, **kwargs: str) -> None:
        warnings.append({"message": message, **kwargs})

    monkeypatch.setattr("bookbot.tui.screens.logger.warning", fake_warning)

    app = MatchHarness(ConfigManager(tmp_path / "config"), provider)
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        summary = await screen.find_matches(books)

        table = screen.query_one("#matches_table", DataTable)
        assert summary == MatchSummary(
            matched_count=0,
            unmatched_count=2,
            failed_count=2,
        )
        assert _row_values(table, 0) == [
            "Book One",
            "No matches",
            "0.00",
            "[none]",
            "Manual",
            "",
        ]
        assert _row_values(table, 1) == [
            "Book Two",
            "No matches",
            "0.00",
            "[none]",
            "Manual",
            "",
        ]
        assert all(book.chosen_identity is None for book in books)
        assert all(book.provider_candidates == [] for book in books)

    assert warnings == [
        {
            "message": "Metadata provider failed during TUI matching",
            "title_guess": "Book One",
            "error": "provider offline",
        },
        {
            "message": "Metadata provider failed during TUI matching",
            "title_guess": "Book Two",
            "error": "request timed out",
        },
    ]


@pytest.mark.asyncio
async def test_match_review_screen_reports_mixed_success_and_failure_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = [_make_audiobook("Matched Book"), _make_audiobook("Broken Book")]
    provider = BehaviorProvider(
        {
            "Matched Book": [_make_candidate("Found Match", "Author Name")],
            "Broken Book": RuntimeError("service unavailable"),
        }
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.logger.warning",
        lambda *args, **kwargs: None,
    )

    app = MatchHarness(ConfigManager(tmp_path / "config"), provider)
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        summary = await screen.find_matches(books)

        table = screen.query_one("#matches_table", DataTable)
        assert summary == MatchSummary(
            matched_count=1,
            unmatched_count=1,
            failed_count=1,
        )
        assert _row_values(table, 0) == [
            "Matched Book",
            "Found Match - Author Name",
            "0.91",
            "[none]",
            "Accept",
            "stub",
        ]
        assert _row_values(table, 1) == [
            "Broken Book",
            "No matches",
            "0.00",
            "[none]",
            "Manual",
            "",
        ]
        assert books[0].chosen_identity is not None
        assert books[0].chosen_identity.title == "Found Match"
        assert books[1].chosen_identity is None


@pytest.mark.asyncio
async def test_match_review_screen_times_out_provider_manager_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_manager = ConfigManager(tmp_path / "config")
    config = config_manager.load_config()
    config.providers.request_timeout = 1
    config_manager.save_config(config)

    manager = object.__new__(ProviderManager)
    manager.config_manager = config_manager
    manager.cache_manager = None
    manager.providers = {"openlibrary": HangingProvider()}
    manager.get_enabled_providers = lambda: list(manager.providers.values())  # type: ignore[method-assign]

    warnings: list[dict[str, str]] = []

    def fake_warning(message: str, **kwargs: str) -> None:
        warnings.append({"message": message, **kwargs})

    monkeypatch.setattr("bookbot.tui.screens.logger.warning", fake_warning)

    app = MatchHarness(config_manager, manager)
    books = [_make_audiobook("Timed Out Book")]
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        summary = await asyncio.wait_for(screen.find_matches(books), timeout=2)

        table = screen.query_one("#matches_table", DataTable)
        assert summary == MatchSummary(
            matched_count=0,
            unmatched_count=1,
            failed_count=1,
        )
        assert _row_values(table, 0) == [
            "Timed Out Book",
            "No matches",
            "0.00",
            "[none]",
            "Manual",
            "",
        ]

    assert warnings == [
        {
            "message": "Metadata provider failed during TUI matching",
            "title_guess": "Timed Out Book",
            "error": "All metadata providers unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_bookbot_app_reports_no_matches_found_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    app.audiobook_sets = [_make_audiobook("Unmatched Book")]

    async def fake_find_matches(_: list[AudiobookSet]) -> MatchSummary:
        return MatchSummary(matched_count=0, unmatched_count=1, failed_count=1)

    async with app.run_test():
        match_screen = app.query_one("#match_screen", MatchReviewScreen)
        monkeypatch.setattr(match_screen, "find_matches", fake_find_matches)
        await app.find_matches()

        assert app.query_one("#status_label", Label).content == "No matches found"
        assert (
            "Check network/providers"
            in app.query_one("#warnings_panel", Static).content
        )


@pytest.mark.asyncio
async def test_bookbot_app_with_preloaded_folders_starts_in_scanning_state(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()

    app = BookBotApp(ConfigManager(tmp_path / "config"), [library])

    async with app.run_test() as pilot:
        await pilot.pause(0)

        assert app.current_step == "scanning"
        assert app.query_one(TabbedContent).active == "mission"
        # The scan worker may already have advanced the label from
        # "Scanning preloaded folders..." to "Scanning <dir>..." depending on
        # scheduling, so assert the scanning phase rather than an exact
        # mid-race string.
        assert str(app.query_one("#status_label", Label).content).startswith("Scanning")


@pytest.mark.asyncio
async def test_bookbot_app_reports_partial_match_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    app.audiobook_sets = [_make_audiobook("Book One"), _make_audiobook("Book Two")]

    async def fake_find_matches(_: list[AudiobookSet]) -> MatchSummary:
        return MatchSummary(matched_count=1, unmatched_count=1, failed_count=1)

    async with app.run_test():
        match_screen = app.query_one("#match_screen", MatchReviewScreen)
        monkeypatch.setattr(match_screen, "find_matches", fake_find_matches)
        await app.find_matches()

        assert app.query_one("#status_label", Label).content == (
            "Matched 1 of 2 - review unmatched"
        )
        assert (
            "Some providers failed" in app.query_one("#warnings_panel", Static).content
        )


@pytest.mark.asyncio
async def test_match_review_screen_requires_manual_review_for_warn_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = [_make_audiobook("Flagged Book")]
    provider = BehaviorProvider(
        {
            "Flagged Book": [
                MatchCandidate(
                    identity=ProviderIdentity(
                        provider="behavior",
                        external_id="flagged-book",
                        title="Found Match",
                        authors=["Author Name"],
                    ),
                    confidence=0.97,
                    confidence_level=MatchConfidence.HIGH,
                    match_reasons=["stub"],
                    review_flags=[
                        ReviewFlag(
                            kind=ReviewFlagKind.RUNTIME_MISMATCH,
                            severity="warn",
                            message="Local runtime differs materially",
                        )
                    ],
                )
            ]
        }
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.logger.warning",
        lambda *args, **kwargs: None,
    )

    app = MatchHarness(ConfigManager(tmp_path / "config"), provider)
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        summary = await screen.find_matches(books)

        table = screen.query_one("#matches_table", DataTable)
        assert summary.matched_count == 1
        assert _row_values(table, 0) == [
            "Flagged Book",
            "Found Match - Author Name",
            "0.97",
            "WARN",
            "Review",
            "stub",
        ]
        assert books[0].chosen_identity is None
        assert books[0].needs_review is True
        assert "Local runtime differs materially" in (books[0].review_reason or "")


@pytest.mark.asyncio
async def test_match_review_screen_manual_candidate_selection_clears_review_and_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_manager = ConfigManager(tmp_path / "config")
    book = _make_tracked_audiobook(tmp_path / "rough-book", "Rough Book")
    provider = BehaviorProvider(
        {
            "Rough Book": [
                _make_candidate("Wrong Match", "Wrong Author", confidence=0.72),
                _make_candidate("Clean Title", "Author Name", confidence=0.95),
            ]
        }
    )

    app = MatchHarness(config_manager, provider)
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        summary = await screen.find_matches([book])

        assert summary.matched_count == 1
        assert book.needs_review is True
        assert book.chosen_identity is None

        async def fake_push_screen(_screen, wait_for_dismiss=False):  # type: ignore[no-untyped-def]
            assert wait_for_dismiss is True
            return 1

        monkeypatch.setattr(app, "push_screen", fake_push_screen)
        await screen.open_candidate_picker(0)

        assert book.needs_review is False
        assert book.chosen_identity is not None
        assert book.chosen_identity.title == "Clean Title"

        decisions_path = config_manager.config_dir / "review-decisions.json"
        saved = json.loads(decisions_path.read_text(encoding="utf-8"))
        assert len(saved) == 1

        plan = PlanBuilder(config_manager.load_config()).create_plan(
            tmp_path,
            [book],
            source_roots=[tmp_path],
        )
        assert plan.skipped == []
        assert plan.operations


def test_tui_cli_allows_manual_launch_without_folder_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    launched: dict[str, list[Path]] = {}

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: object(),
    )

    def fake_run(self: BookBotApp) -> None:
        launched["folders"] = self.source_folders

    monkeypatch.setattr(BookBotApp, "run", fake_run)

    result = runner.invoke(
        cli,
        ["--config-dir", str(tmp_path / "config"), "tui"],
    )

    assert result.exit_code == 0
    assert launched["folders"] == []


def test_tui_cli_passes_folder_args_through_to_preloaded_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    library.mkdir()
    launched: dict[str, list[Path]] = {}

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: object(),
    )

    def fake_run(self: BookBotApp) -> None:
        launched["folders"] = self.source_folders

    monkeypatch.setattr(BookBotApp, "run", fake_run)

    result = runner.invoke(
        cli,
        ["--config-dir", str(tmp_path / "config"), "tui", str(library)],
    )

    assert result.exit_code == 0
    assert launched["folders"] == [library]


def test_bookbot_app_defaults_to_provider_manager(tmp_path: Path) -> None:
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])

    assert isinstance(app.provider, ProviderManager)


@pytest.mark.asyncio
async def test_bookbot_app_closes_provider_on_quit_action(tmp_path: Path) -> None:
    provider = BehaviorProvider()
    app = BookBotApp(ConfigManager(tmp_path / "config"), [], provider=provider)

    async with app.run_test() as pilot:
        await app.action_quit()
        await pilot.pause()

    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_bookbot_app_closes_provider_manager_on_quit_action(
    tmp_path: Path,
) -> None:
    manager = object.__new__(ProviderManager)
    close_calls = {"count": 0}

    async def fake_close_all() -> None:
        close_calls["count"] += 1

    manager.close_all = fake_close_all  # type: ignore[method-assign]
    app = BookBotApp(ConfigManager(tmp_path / "config"), [], provider=manager)

    async with app.run_test() as pilot:
        await app.action_quit()
        await pilot.pause()

    assert close_calls["count"] == 1


@pytest.mark.asyncio
async def test_bookbot_app_closes_provider_on_direct_exit(tmp_path: Path) -> None:
    provider = BehaviorProvider()
    app = BookBotApp(ConfigManager(tmp_path / "config"), [], provider=provider)

    async with app.run_test() as pilot:
        app.exit()
        await pilot.pause()

    assert provider.close_calls == 1


@pytest.mark.asyncio
async def test_apply_requires_two_presses(tmp_path: Path) -> None:
    """First `a` arms the apply; a second press is required to run it."""
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    applied = []

    async def fake_apply() -> None:
        applied.append(True)

    app.apply_changes = fake_apply  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause(0)
        app.query_one("#apply_changes").disabled = False  # a plan was previewed
        await app.action_apply_selected()
        assert app._apply_armed is True
        assert applied == []

        await app.action_apply_selected()
        assert app._apply_armed is False
        assert applied == [True]


@pytest.mark.asyncio
async def test_other_action_disarms_apply(tmp_path: Path) -> None:
    """Any non-apply action cancels a pending apply confirmation."""
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    applied = []

    async def fake_apply() -> None:
        applied.append(True)

    app.apply_changes = fake_apply  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause(0)
        app.query_one("#apply_changes").disabled = False  # a plan was previewed
        await app.action_apply_selected()
        assert app._apply_armed is True

        app.action_help()
        assert app._apply_armed is False

        await app.action_apply_selected()
        assert applied == []  # re-armed, not applied
        assert app._apply_armed is True


@pytest.mark.asyncio
async def test_selecting_match_row_opens_picker_without_crashing(
    tmp_path: Path,
) -> None:
    """Enter on a match row, `/` search, and Escape must work in the real app."""
    from bookbot.tui.screens import CandidateDetailScreen, ManualSearchScreen

    config_manager = ConfigManager(tmp_path / "config")
    book = _make_tracked_audiobook(tmp_path / "rough-book", "Rough Book")
    provider = BehaviorProvider(
        {
            "Rough Book": [
                _make_candidate("Wrong Match", "Wrong Author", confidence=0.72),
                _make_candidate("Clean Title", "Author Name", confidence=0.95),
            ]
        }
    )

    app = MatchHarness(config_manager, provider)
    async with app.run_test() as pilot:
        screen = app.query_one(MatchReviewScreen)
        await screen.find_matches([book])
        screen.query_one("#matches_table", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CandidateDetailScreen)

        await pilot.press("slash")
        await pilot.pause()
        assert isinstance(app.screen, ManualSearchScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, CandidateDetailScreen)

        await pilot.press("down", "enter")
        await pilot.pause()
        assert not isinstance(app.screen, CandidateDetailScreen)
        assert book.chosen_identity is not None
        assert book.chosen_identity.title == "Clean Title"
        assert app.is_running


@pytest.mark.asyncio
async def test_apply_without_preview_does_nothing(tmp_path: Path) -> None:
    """Pressing a twice before any preview must not report a fake success."""
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    applied = []

    async def fake_apply() -> None:
        applied.append(True)

    app.apply_changes = fake_apply  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await pilot.pause(0)
        await app.action_apply_selected()
        await app.action_apply_selected()
        assert applied == []
        assert app._apply_armed is False
        assert "preview" in str(app.query_one("#status_label", Label).content)


@pytest.mark.asyncio
async def test_switching_tabs_disarms_apply(tmp_path: Path) -> None:
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])

    async with app.run_test() as pilot:
        await pilot.pause(0)
        app.query_one("#apply_changes").disabled = False
        await app.action_apply_selected()
        assert app._apply_armed is True
        app.query_one(TabbedContent).active = "convert"
        await pilot.pause()
        assert app._apply_armed is False


@pytest.mark.asyncio
async def test_bracketed_names_render_literally(tmp_path: Path) -> None:
    """Folder names and tags like [unabridged] or [/] are text, not markup."""
    from bookbot.tui.screens import ScanResultsScreen

    book = _make_audiobook("Live [/] Recording")
    book.source_path = tmp_path / "Some Book [unabridged]"
    app = BookBotApp(ConfigManager(tmp_path / "config"), [])

    async with app.run_test() as pilot:
        await pilot.pause(0)
        scan = app.query_one("#scan_screen", ScanResultsScreen)
        scan.set_audiobook_sets([book])
        await pilot.pause()
        table = scan.query_one("#scan_results_table", DataTable)
        assert _row_values(table, 0)[0] == "Some Book [unabridged]"
        app.update_status("Scan failed: bad tag [/]")
        await pilot.pause()


@pytest.mark.asyncio
async def test_successful_apply_disables_apply_and_rescans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an apply the old plan is stale, so it can't be re-applied."""
    from bookbot.tui.screens import PreviewScreen

    app = BookBotApp(ConfigManager(tmp_path / "config"), [])
    scans: list[str | None] = []

    async with app.run_test() as pilot:
        await pilot.pause(0)
        preview = app.query_one("#preview_screen", PreviewScreen)

        async def fake_apply() -> bool:
            preview.last_transaction_id = "tx-123456789"
            return True

        monkeypatch.setattr(preview, "apply_changes", fake_apply)

        async def record_scan(_self: BookBotApp, event: BookBotApp.StartScan) -> None:
            scans.append(event.applied_transaction)

        monkeypatch.setattr(BookBotApp, "on_book_bot_app_start_scan", record_scan)
        app.query_one("#apply_changes").disabled = False
        await app.action_apply_selected()
        await app.action_apply_selected()
        await pilot.pause()

        assert app.query_one("#apply_changes").disabled is True
        assert app.last_transaction_id == "tx-123456789"
        assert scans == ["tx-123456789"]


@pytest.mark.asyncio
async def test_accept_safe_candidates_skips_blocked_and_weak_matches(
    tmp_path: Path,
) -> None:
    from bookbot.core.models import ReviewFlag, ReviewFlagKind

    config_manager = ConfigManager(tmp_path / "config")
    good = _make_tracked_audiobook(tmp_path / "good", "Good Book")
    weak = _make_tracked_audiobook(tmp_path / "weak", "Weak Book")
    blocked = _make_tracked_audiobook(tmp_path / "blocked", "Blocked Book")
    provider = BehaviorProvider(
        {
            "Good Book": [_make_candidate("Good Book", "Author", confidence=0.82)],
            "Weak Book": [_make_candidate("Weak Book", "Author", confidence=0.6)],
            "Blocked Book": [
                _make_candidate("Blocked Book", "Author", confidence=0.85)
            ],
        }
    )

    app = MatchHarness(config_manager, provider)
    async with app.run_test():
        screen = app.query_one(MatchReviewScreen)
        await screen.find_matches([good, weak, blocked])
        blocked.provider_candidates[0].review_flags = [
            ReviewFlag(
                kind=ReviewFlagKind.AUTHOR_CONFLICT,
                severity="block",
                message="author differs",
            )
        ]
        for book in (good, weak, blocked):
            book.needs_review = True

        assert screen.accept_safe_candidates() == 1
        assert good.chosen_identity is not None
        assert good.needs_review is False
        assert weak.chosen_identity is None
        assert blocked.chosen_identity is None
