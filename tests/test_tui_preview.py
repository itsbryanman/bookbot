"""Tests for preview path rendering in the TUI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    RenameOperation,
    RenamePlan,
    Track,
    TrackStatus,
)
from bookbot.tui.screens import PreviewScreen


class PreviewHarness(App[None]):
    """Minimal Textual app for exercising the preview screen."""

    def __init__(self, config_manager: ConfigManager) -> None:
        super().__init__()
        self.config_manager = config_manager

    def compose(self) -> ComposeResult:
        yield PreviewScreen(self.config_manager, id="preview")


def _plain_row(table: DataTable, index: int) -> list[str]:
    return [str(cell) for cell in table.get_row_at(index)]


def _make_track(path: Path) -> Track:
    return Track(
        src_path=path,
        disc=1,
        track_index=1,
        file_size=0,
        audio_format=AudioFormat.MP3,
        status=TrackStatus.VALID,
    )


def _make_audiobook_set(source_path: Path, track: Track) -> AudiobookSet:
    return AudiobookSet(
        source_path=source_path,
        raw_title_guess=source_path.name,
        total_tracks=1,
        tracks=[track],
    )


def _make_plan(
    source_root: Path,
    audiobook_sets: list[AudiobookSet],
    operations: list[RenameOperation],
) -> RenamePlan:
    return RenamePlan(
        plan_id="preview-test",
        created_at=datetime.now(),
        source_path=source_root,
        audiobook_sets=audiobook_sets,
        operations=operations,
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_preview_table_uses_relative_path_under_common_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    source_path = library / "Book One"
    source_path.mkdir(parents=True)
    src_track_path = source_path / "01.mp3"
    src_track_path.touch()

    track = _make_track(src_track_path)
    audiobook_set = _make_audiobook_set(source_path, track)
    new_path = source_path / "Renamed" / "01.mp3"
    plan = _make_plan(
        source_path,
        [audiobook_set],
        [RenameOperation(old_path=src_track_path, new_path=new_path, track=track)],
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.PlanBuilder.create_plan",
        lambda *args, **kwargs: plan,
    )

    app = PreviewHarness(ConfigManager(tmp_path / "config"))
    async with app.run_test():
        screen = app.query_one(PreviewScreen)
        screen.source_roots = [library]
        screen.set_audiobook_sets([audiobook_set])

        table = screen.query_one("#preview_table", DataTable)
        assert _plain_row(table, 0) == ["01.mp3", "Renamed/01.mp3", "✓ Ready"]


@pytest.mark.asyncio
async def test_preview_table_uses_readable_relative_path_for_sibling_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "scan"
    source_path = source_root / "Book One"
    output_root = tmp_path / "output"
    source_path.mkdir(parents=True)
    src_track_path = source_path / "01.mp3"
    src_track_path.touch()

    track = _make_track(src_track_path)
    audiobook_set = _make_audiobook_set(source_path, track)
    new_path = output_root / "Book One" / "01.mp3"
    plan = _make_plan(
        source_path,
        [audiobook_set],
        [RenameOperation(old_path=src_track_path, new_path=new_path, track=track)],
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.PlanBuilder.create_plan",
        lambda *args, **kwargs: plan,
    )

    app = PreviewHarness(ConfigManager(tmp_path / "config"))
    async with app.run_test():
        screen = app.query_one(PreviewScreen)
        screen.source_roots = [source_root]
        screen.set_audiobook_sets([audiobook_set])

        table = screen.query_one("#preview_table", DataTable)
        assert _plain_row(table, 0) == [
            "01.mp3",
            "../../output/Book One/01.mp3",
            "✓ Ready",
        ]


@pytest.mark.asyncio
async def test_preview_table_lists_planned_sidecar_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "Book One"
    source_path.mkdir(parents=True)
    src_track_path = source_path / "01.mp3"
    src_track_path.touch()

    track = _make_track(src_track_path)
    audiobook_set = _make_audiobook_set(source_path, track)
    plan = RenamePlan(
        plan_id="preview-sidecar",
        created_at=datetime.now(),
        source_path=source_path,
        audiobook_sets=[audiobook_set],
        operations=[],
        planned_sidecars=[source_path / "metadata.json"],
        dry_run=True,
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.PlanBuilder.create_plan",
        lambda *args, **kwargs: plan,
    )

    app = PreviewHarness(ConfigManager(tmp_path / "config"))
    async with app.run_test():
        screen = app.query_one(PreviewScreen)
        screen.source_roots = [tmp_path]
        screen.set_audiobook_sets([audiobook_set])

        table = screen.query_one("#preview_table", DataTable)
        assert _plain_row(table, 1) == [
            "metadata.json",
            "metadata.json",
            "✎ Sidecar",
        ]


@pytest.mark.asyncio
async def test_preview_apply_changes_runs_for_sidecar_only_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "Book One"
    source_path.mkdir(parents=True)
    src_track_path = source_path / "01.mp3"
    src_track_path.touch()

    track = _make_track(src_track_path)
    audiobook_set = _make_audiobook_set(source_path, track)
    plan = RenamePlan(
        plan_id="preview-sidecar-apply",
        created_at=datetime.now(),
        source_path=source_path,
        audiobook_sets=[audiobook_set],
        operations=[],
        planned_sidecars=[source_path / "metadata.json"],
        dry_run=True,
    )

    monkeypatch.setattr(
        "bookbot.tui.screens.PlanBuilder.create_plan",
        lambda *args, **kwargs: plan,
    )

    def fake_execute_plan(
        _self, passed_plan: RenamePlan, dry_run: bool = False
    ) -> bool:
        passed_plan.applied_transaction_id = "tx-sidecar"
        passed_plan.dry_run = dry_run
        return True

    monkeypatch.setattr(
        "bookbot.tui.screens.TransactionManager.execute_plan",
        fake_execute_plan,
    )

    app = PreviewHarness(ConfigManager(tmp_path / "config"))
    async with app.run_test():
        screen = app.query_one(PreviewScreen)
        screen.source_roots = [tmp_path]
        screen.set_audiobook_sets([audiobook_set])

        assert await screen.apply_changes() is True

        table = screen.query_one("#preview_table", DataTable)
        assert _plain_row(table, 0) == [
            "Applied 0 rename(s), 1 sidecar write(s)",
            "✓ Success",
            "tx-sidec...",
        ]


def test_preview_path_formatter_falls_back_to_absolute_for_far_disjoint_roots() -> None:
    common_root = Path("/tmp/bookbot/source")
    disjoint_path = Path("/var/lib/bookbot/output/01.mp3")

    rendered = PreviewScreen._format_preview_path(disjoint_path, common_root)

    assert rendered == str(disjoint_path)


@pytest.mark.asyncio
async def test_conversion_queue_updates_status_per_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Statuses change as each book converts (no missing-column errors)."""
    from textual.widgets import Input

    from bookbot.tui.screens import ConversionScreen

    class ConversionHarness(App[None]):
        def __init__(self, config_manager: ConfigManager) -> None:
            super().__init__()
            self.config_manager = config_manager

        def compose(self) -> ComposeResult:
            yield ConversionScreen(self.config_manager, id="convert_screen")

    class FakePipeline:
        def __init__(self, _config_manager: ConfigManager) -> None:
            pass

        async def convert_audiobook_set(self, audiobook_set, _config):  # type: ignore[no-untyped-def]
            return audiobook_set.raw_title_guess == "Book One"

    monkeypatch.setattr("bookbot.convert.pipeline.ConversionPipeline", FakePipeline)
    book_one = _make_audiobook_set(
        tmp_path / "Book One", _make_track(tmp_path / "Book One" / "01.mp3")
    )
    book_one.raw_title_guess = "Book One"
    book_two = _make_audiobook_set(
        tmp_path / "Book Two", _make_track(tmp_path / "Book Two" / "01.mp3")
    )
    book_two.raw_title_guess = "Book Two"

    app = ConversionHarness(ConfigManager(tmp_path / "config"))
    async with app.run_test() as pilot:
        screen = app.query_one(ConversionScreen)
        screen.set_audiobook_sets([book_one, book_two])
        screen.query_one("#output_dir", Input).value = str(tmp_path / "out")
        await screen.start_conversion()
        await pilot.pause()

        table = screen.query_one("#conversion_results", DataTable)
        assert _plain_row(table, 0)[3] == "Complete"
        assert _plain_row(table, 1)[3] == "Failed"
