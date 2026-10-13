"""Generate deterministic BookBot TUI screenshots for the README."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from textual.widgets import Button, Input, Static, TabbedContent

from bookbot.config.manager import ConfigManager
from bookbot.core.models import (
    AudiobookSet,
    AudioFormat,
    MatchCandidate,
    MatchConfidence,
    ProviderIdentity,
    Track,
    TrackStatus,
)
from bookbot.tui.app import BookBotApp
from bookbot.tui.screens import (
    ConversionScreen,
    MatchReviewScreen,
    PreviewScreen,
    ScanResultsScreen,
)


class DemoProvider:
    """Minimal provider used only while rendering the static showcase."""

    async def close(self) -> None:
        return None


def _demo_books(root: Path) -> list[AudiobookSet]:
    records = [
        ("Dune", "Frank Herbert", "Simon Vance", 0.98),
        ("The Left Hand of Darkness", "Ursula K. Le Guin", "George Guidall", 0.96),
        ("Piranesi", "Susanna Clarke", "Chiwetel Ejiofor", 0.94),
    ]
    books: list[AudiobookSet] = []
    for index, (title, author, narrator, confidence) in enumerate(records, 1):
        book_dir = root / f"{author} - {title}"
        book_dir.mkdir(parents=True)
        audio_path = book_dir / "01 - Opening.mp3"
        audio_path.write_bytes(b"showcase")
        track = Track(
            src_path=audio_path,
            track_index=1,
            duration=3600,
            file_size=8,
            audio_format=AudioFormat.MP3,
            status=TrackStatus.VALID,
        )
        identity = ProviderIdentity(
            provider="showcase",
            external_id=str(index),
            title=title,
            authors=[author],
            narrator=narrator,
            language="en",
            edition="Unabridged",
        )
        candidate = MatchCandidate(
            identity=identity,
            confidence=confidence,
            confidence_level=MatchConfidence.HIGH,
            match_reasons=["Title match", "Author match", "Edition verified"],
        )
        books.append(
            AudiobookSet(
                source_path=book_dir,
                raw_title_guess=title,
                author_guess=author,
                narrator_guess=narrator,
                total_tracks=1,
                total_duration=3600,
                tracks=[track],
                provider_candidates=[candidate],
                chosen_identity=identity,
            )
        )
    return books


async def capture(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="bookbot-showcase-") as temp_dir:
        root = Path(temp_dir) / "Audiobooks"
        root.mkdir()
        books = _demo_books(root)
        app = BookBotApp(
            ConfigManager(Path(temp_dir) / "config"),
            [],
            provider=DemoProvider(),  # type: ignore[arg-type]
        )

        async with app.run_test(size=(150, 46)) as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "mission"
            scan = app.query_one(ScanResultsScreen)
            scan.set_audiobook_sets(books)
            app.audiobook_sets = books
            app.update_status("Scan complete  /  3 books  /  3 editions ready")
            app.query_one("#find_matches", Button).disabled = False
            await pilot.pause()
            app.save_screenshot(
                filename="bookbot-mission-control.svg",
                path=str(output_dir),
            )

            match = app.query_one(MatchReviewScreen)
            match.audiobook_sets = books
            match._row_states = ["accepted_auto"] * len(books)
            match._row_errors = [None] * len(books)
            match._render_table()
            preview = app.query_one(PreviewScreen)
            preview.source_roots = [root]
            preview.set_audiobook_sets(books)
            app.update_status(
                "Plan ready  /  3 books matched  /  durable undo journal enabled"
            )
            app.update_warning_panel(
                "READY TO APPLY\n"
                "Narrator, language, and edition checks passed for every match."
            )
            app.query_one("#preview_changes", Button).disabled = False
            app.query_one("#apply_changes", Button).disabled = False
            tabs.active = "mission"
            await pilot.pause()
            app.save_screenshot(
                filename="bookbot-preview.svg",
                path=str(output_dir),
            )

            tabs.active = "convert"
            conversion = app.query_one(ConversionScreen)
            conversion.set_audiobook_sets(books)
            conversion.query_one("#output_dir", Input).value = "/audiobooks/converted"
            app.update_status("Conversion queue ready  /  3 M4B outputs")
            await pilot.pause()
            app.save_screenshot(
                filename="bookbot-conversion.svg",
                path=str(output_dir),
            )

            tabs.active = "drm_tab"
            app.query_one("#user_code_display", Static).update(
                "AAX pipeline ready  /  temporary output + decode verification"
            )
            app.update_status("Protected audiobook tools ready")
            await pilot.pause()
            app.save_screenshot(
                filename="bookbot-drm.svg",
                path=str(output_dir),
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("images"))
    args = parser.parse_args()
    asyncio.run(capture(args.output_dir))


if __name__ == "__main__":
    main()
