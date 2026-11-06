"""Edition dedupe must not group different works or trust fuzzy-only matches."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.core.dedupe import DedupeEngine
from bookbot.core.models import AudiobookSet, AudioFormat, Track, TrackStatus


def _book(
    root: Path,
    folder: str,
    title: str,
    author: str | None,
    *,
    minutes: float,
    bitrate: int = 64,
    asin: str | None = None,
    payload: bytes = b"\0",
) -> AudiobookSet:
    source = root / folder
    source.mkdir(parents=True, exist_ok=True)
    path = source / "01.mp3"
    path.write_bytes(payload * 100)
    return AudiobookSet(
        source_path=source,
        raw_title_guess=title,
        author_guess=author,
        asin_guess=asin,
        total_tracks=1,
        total_duration=minutes * 60,
        tracks=[
            Track(
                src_path=path,
                track_index=1,
                audio_format=AudioFormat.MP3,
                bitrate=bitrate,
                duration=minutes * 60,
                file_size=100,
                status=TrackStatus.VALID,
            )
        ],
    )


def _quarantined(engine: DedupeEngine, sets: list[AudiobookSet]) -> set[Path]:
    plan = engine.build_plan(engine.analyze_editions(sets), None, set())
    return {op.source.parent for op in plan.operations}


def test_dune_and_dune_messiah_are_not_duplicates(tmp_path: Path) -> None:
    dune = _book(
        tmp_path, "Frank Herbert - Dune", "Dune", "Frank Herbert", minutes=1260
    )
    messiah = _book(
        tmp_path,
        "Frank Herbert - Dune Messiah",
        "Dune Messiah",
        "Frank Herbert",
        minutes=530,
    )
    engine = DedupeEngine(tmp_path)

    assert engine.analyze_editions([dune, messiah]) == []
    assert engine.possible_duplicates == []
    assert _quarantined(engine, [dune, messiah]) == set()


def test_same_title_reencode_still_groups(tmp_path: Path) -> None:
    low = _book(tmp_path, "Frank Herbert - Dune", "Dune", "Frank Herbert", minutes=1260)
    high = _book(
        tmp_path,
        "Frank Herbert - Dune [128k]",
        "Dune",
        "Frank Herbert",
        minutes=1259,
        bitrate=128,
    )
    engine = DedupeEngine(tmp_path)

    groups = engine.analyze_editions([low, high])

    assert len(groups) == 1
    assert groups[0].keeper is not None
    assert groups[0].keeper.audiobook_set is high
    assert _quarantined(engine, [low, high]) == {low.source_path}


def test_fuzzy_only_match_without_corroboration_is_review_only(
    tmp_path: Path,
) -> None:
    first = _book(tmp_path, "a", "Leviathan Wakes", "James S. A. Corey", minutes=1200)
    second = _book(tmp_path, "b", "Leviathan Wake", "James S. A. Corey", minutes=600)
    engine = DedupeEngine(tmp_path)

    assert engine.analyze_editions([first, second]) == []
    assert len(engine.possible_duplicates) == 1
    assert {s.source_path for s in engine.possible_duplicates[0].members} == {
        first.source_path,
        second.source_path,
    }
    assert "review" in engine.possible_duplicates[0].reason.lower()
    assert _quarantined(engine, [first, second]) == set()


def test_fuzzy_match_corroborated_by_duration_groups(tmp_path: Path) -> None:
    first = _book(tmp_path, "a", "Leviathan Wakes", "James S. A. Corey", minutes=1200)
    second = _book(tmp_path, "b", "Leviathan Wake", "James S. A. Corey", minutes=1210)
    engine = DedupeEngine(tmp_path, duration_tolerance_pct=2.0)

    assert len(engine.analyze_editions([first, second])) == 1

    strict = DedupeEngine(tmp_path, duration_tolerance_pct=0.5)
    assert strict.analyze_editions([first, second]) == []
    assert len(strict.possible_duplicates) == 1


def test_fuzzy_match_corroborated_by_asin_groups(tmp_path: Path) -> None:
    first = _book(
        tmp_path,
        "a",
        "Leviathan Wakes",
        "James S. A. Corey",
        minutes=1200,
        asin="B0047Y16MO",
    )
    second = _book(
        tmp_path,
        "b",
        "Leviathan Wake",
        "James S. A. Corey",
        minutes=300,
        asin="B0047Y16MO",
    )
    engine = DedupeEngine(tmp_path)
    assert len(engine.analyze_editions([first, second])) == 1


def test_missing_author_is_not_a_match(tmp_path: Path) -> None:
    known = _book(tmp_path, "a", "Dune", "Frank Herbert", minutes=1260)
    unknown = _book(tmp_path, "b", "Dune", None, minutes=200)
    engine = DedupeEngine(tmp_path)

    assert engine.analyze_editions([known, unknown]) == []
    assert _quarantined(engine, [known, unknown]) == set()
    assert len(engine.possible_duplicates) == 1


def test_authorless_same_title_needs_corroboration(tmp_path: Path) -> None:
    first = _book(tmp_path, "a", "Dune", None, minutes=1260)
    second = _book(tmp_path, "b", "Dune", None, minutes=200)
    engine = DedupeEngine(tmp_path)
    assert engine.analyze_editions([first, second]) == []
    assert len(engine.possible_duplicates) == 1

    same_length = _book(tmp_path, "c", "Dune", None, minutes=1262)
    assert len(engine.analyze_editions([first, same_length])) == 1


def test_cli_lists_possible_duplicates_and_quarantines_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    sets = [
        _book(library, "Frank Herbert - Dune", "Dune", "Frank Herbert", minutes=1260),
        _book(
            library,
            "Frank Herbert - Dune Messiah",
            "Dune Messiah",
            "Frank Herbert",
            minutes=530,
            payload=b"\1",
        ),
        _book(
            library,
            "x",
            "Leviathan Wakes",
            "James S. A. Corey",
            minutes=1200,
            payload=b"\2",
        ),
        _book(
            library,
            "y",
            "Leviathan Wake",
            "James S. A. Corey",
            minutes=600,
            payload=b"\3",
        ),
    ]
    monkeypatch.setattr(
        "bookbot.core.discovery.AudioFileScanner.scan_directory",
        lambda self, path, progress=None: sets,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "cfg"),
            "dedupe",
            str(library),
            "--editions-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Possible duplicates" in result.output
    assert "review" in result.output.lower()
    assert "[QUARANTINE]" not in result.output
    assert "No duplicates to quarantine." in result.output


def test_audio_hash_help_matches_behavior(tmp_path: Path) -> None:
    runner = CliRunner()
    help_text = runner.invoke(cli, ["dedupe", "--help"]).output
    assert "only checks ffmpeg availability" not in help_text
    assert "not implemented" in help_text.lower()

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "cfg"),
            "dedupe",
            str(tmp_path),
            "--audio-hash",
        ],
    )
    assert result.exit_code == 1
    assert "not implemented" in result.output


# ── Codex review on #11 ──


def test_language_qualifiers_keep_editions_apart(tmp_path: Path) -> None:
    english = _book(
        tmp_path,
        "a",
        "Dune (English)",
        "Frank Herbert",
        minutes=1260,
        asin="B00ENGLISH",
    )
    german = _book(
        tmp_path,
        "b",
        "Dune (German)",
        "Frank Herbert",
        minutes=1500,
        asin="B00GERMAN0",
    )
    engine = DedupeEngine(tmp_path)

    assert engine.analyze_editions([english, german]) == []
    assert _quarantined(engine, [english, german]) == set()


@pytest.mark.parametrize(
    "noisy",
    ["Dune (Unabridged)", "Dune [128k]", "Dune (2007)", "Dune [MP3]", "Dune, The"],
)
def test_release_noise_in_brackets_is_still_ignored(tmp_path: Path, noisy: str) -> None:
    engine = DedupeEngine(tmp_path)
    plain = _book(tmp_path, "a", "Dune", "Frank Herbert", minutes=1260)
    other = _book(tmp_path, "b", noisy, "Frank Herbert", minutes=1260)
    assert engine._edition_key(plain) == engine._edition_key(other)


def test_bracket_noise_check_is_linear_on_pathological_titles(
    tmp_path: Path,
) -> None:
    import time

    engine = DedupeEngine(tmp_path)
    book = _book(tmp_path, "a", "(" + "mp3 " * 20000 + "x", None, minutes=1)
    started = time.perf_counter()
    engine._edition_key(book)
    assert time.perf_counter() - started < 1.0
