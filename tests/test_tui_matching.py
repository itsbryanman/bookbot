"""Tests for TUI matching status reporting and provider teardown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Label, Static, TabbedContent

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
)
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
        assert table.get_row_at(0) == [
            "Book One",
            "No matches",
            "0.00",
            "Manual",
            "",
        ]
        assert table.get_row_at(1) == [
            "Book Two",
            "No matches",
            "0.00",
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
        assert table.get_row_at(0) == [
            "Matched Book",
            "Found Match - Author Name",
            "0.91",
            "Accept",
            "stub",
        ]
        assert table.get_row_at(1) == [
            "Broken Book",
            "No matches",
            "0.00",
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
        assert table.get_row_at(0) == [
            "Timed Out Book",
            "No matches",
            "0.00",
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
        assert "Check network/providers" in app.query_one(
            "#warnings_panel", Static
        ).content


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
        assert str(app.query_one("#status_label", Label).content).startswith(
            "Scanning"
        )


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
        assert "Some providers failed" in app.query_one(
            "#warnings_panel", Static
        ).content


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
