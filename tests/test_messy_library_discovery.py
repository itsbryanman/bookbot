"""Regression tests for discovery on a damaged library (see /bbmess).

Junk files, loose files in the library root, generic folder names, swapped
"Title - Author" names, missing track numbers, placeholder tags, duplicate
books and multi-disc ordering. Audio tags and durations are patched in, so
no real audio is needed.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.chapters.detector import ChapterDetector
from bookbot.config.models import Config
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudioFormat, AudioTags
from bookbot.core.organizer import SmartOrganizer
from bookbot.core.planning import PlanBuilder, load_plan, save_plan


def _scanner(
    monkeypatch: pytest.MonkeyPatch,
    tag_map: dict[Path, AudioTags] | None = None,
    duration: float = 600.0,
) -> AudioFileScanner:
    scanner = AudioFileScanner()
    tags = tag_map or {}

    def fake_tags(file_path: Path) -> AudioTags:
        return tags.get(file_path, AudioTags()).model_copy()

    def fake_properties(
        file_path: Path,
    ) -> tuple[float | None, int | None, int | None, int | None]:
        return duration, None, None, None

    monkeypatch.setattr(scanner, "_extract_audio_tags", fake_tags)
    monkeypatch.setattr(scanner, "_extract_audio_properties", fake_properties)
    return scanner


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    return path


def _by_title(sets: list) -> dict:
    return {s.raw_title_guess: s for s in sets}


# ── 1. junk files ──


def test_appledouble_and_os_junk_files_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _touch(tmp_path / "Stormbreaker.m4b")
    for junk in ("._Stormbreaker.m4b", ".DS_Store", "desktop.ini", "Thumbs.db"):
        _touch(tmp_path / junk)
    scanner = _scanner(
        monkeypatch,
        {book: AudioTags(album="Alex Rider 01 - Stormbreaker", artist="A. H.")},
    )

    sets = scanner.scan_directory(tmp_path)

    assert [track.src_path.name for s in sets for track in s.tracks] == [
        "Stormbreaker.m4b"
    ]


def test_junk_companions_do_not_travel_with_a_book(tmp_path: Path) -> None:
    from bookbot.core.models import AudiobookSet, Track

    folder = tmp_path / "Frank Herbert - Dune"
    audio = _touch(folder / "Dune.m4b")
    for name in ("cover.jpg", "._cover.jpg", "._Dune.m4b", "Thumbs.db", "desktop.ini"):
        _touch(folder / name)
    audiobook = AudiobookSet(
        source_path=folder,
        raw_title_guess="Dune",
        author_guess="Frank Herbert",
        tracks=[
            Track(
                src_path=audio,
                disc=1,
                track_index=1,
                file_size=1,
                audio_format=AudioFormat.M4B,
            )
        ],
    )

    operations = PlanBuilder(Config())._companion_operations(
        audiobook, tmp_path / "out"
    )

    assert sorted(op.old_path.name for op in operations) == ["cover.jpg"]


# ── 2. loose files in the library root ──


def test_loose_root_files_are_named_from_tags_or_file_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "lib"
    storm = _touch(library / "Stormbreaker.m4b")
    vampires = _touch(library / "vampires dont wear polka dots.m4b")
    _touch(library / "Some Author - Some Book" / "book.m4b")
    scanner = _scanner(
        monkeypatch,
        {
            storm: AudioTags(
                album="Alex Rider 01 - Stormbreaker", artist="Anthony Horowitz"
            ),
            vampires: AudioTags(
                title="Unknown Book-Part1",
                artist="Marcia Thornton Jones, Debbie Dadey",
            ),
        },
    )

    sets = _by_title(scanner.scan_directory(library))

    assert set(sets) == {
        "Stormbreaker",
        "Vampires Dont Wear Polka Dots",
        "Some Book",
    }
    assert sets["Stormbreaker"].display_name == "Stormbreaker.m4b"
    assert (
        sets["Vampires Dont Wear Polka Dots"].display_name
        == "vampires dont wear polka dots.m4b"
    )
    assert sets["Some Book"].display_name == "Some Author - Some Book"
    assert all(s.display_name != "lib" for s in sets.values())


# ── 3. generic / junk folder and file names ──


def test_junk_folder_name_loses_to_consistent_album_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "audiobook_final_FINAL"
    tag_map: dict[Path, AudioTags] = {}
    for disc in range(1, 13):
        for track in (1, 2):
            path = _touch(book / f"CD {disc}" / f"Track{track:02d}.mp3")
            tag_map[path] = AudioTags(
                title=f"Track{track:02d}",
                album=f"NOS4A2 (Disc {disc:02d})",
                artist="Joe Hill",
                track=track,
            )
    _touch(book / "desktop.ini")
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "NOS4A2"
    assert audiobook.author_guess == "Joe Hill"
    assert audiobook.disc_count == 12
    assert [(t.disc, t.track_index) for t in audiobook.tracks][:5] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
        (3, 1),
    ]
    assert [t.src_path.parent.name for t in audiobook.tracks][-2:] == [
        "CD 12",
        "CD 12",
    ]
    assert audiobook.warnings == []


def test_album_disc_suffix_in_parentheses_is_stripped() -> None:
    scanner = AudioFileScanner()

    assert scanner._album_disc_split("NOS4A2 (Disc 01)") == ("NOS4A2", 1)
    assert scanner._album_disc_split("NOS4A2 [CD 3]") == ("NOS4A2", 3)
    assert scanner._album_disc_split("NOS4A2 (Disc 2 of 17)") == ("NOS4A2", 2)
    assert scanner._album_disc_split("Catch-22") == ("Catch-22", None)


def test_generic_folder_and_file_names_with_no_tags_need_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "lib"
    _touch(library / "New Folder (3)" / "audio.m4b")
    _touch(library / "New Folder (3)" / "cover.jpg")
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(library)

    assert audiobook.raw_title_guess is None
    assert audiobook.needs_review is True
    assert audiobook.review_reason is not None
    assert "generic" in audiobook.review_reason.lower()

    plan = PlanBuilder(Config()).create_plan(library, [audiobook])
    assert plan.operations == []
    assert [item["source_path"] for item in plan.skipped] == [
        str(library / "New Folder (3)")
    ]


def test_generic_file_stem_falls_back_to_folder_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(tmp_path / "Alice Feeney - Rock Paper Scissors" / "book.m4b")
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(tmp_path)

    assert audiobook.raw_title_guess == "Rock Paper Scissors"
    assert audiobook.author_guess == "Alice Feeney"


def test_underscored_lowercase_folder_becomes_title_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "the_inmate"
    tag_map = {
        _touch(book / f"inm_{n:06d}.mp3"): AudioTags(
            title=f"Freida McFadden - The Inmate Pt{index:02d} of 3",
            artist="Freida McFadden",
        )
        for index, n in ((1, 989720), (2, 120001), (3, 554433))
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "The Inmate"
    assert audiobook.author_guess == "Freida McFadden"


# ── 4. "Title – Author ★" ──


@pytest.mark.parametrize("album", ["The Silent Patient", None])
def test_title_dash_author_uses_artist_tag_and_drops_decorations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, album: str | None
) -> None:
    book = (
        tmp_path
        / "Educated by Tara Westover"
        / ("The Silent Patient – Alex Michaelides ★")
    )
    tag_map = {
        _touch(book / f"Track {n:02d}.mp3"): AudioTags(
            title=f"{n:02d}-71 The Silent Patient",
            album=album,
            artist="Alex Michaelides",
            track=n,
        )
        for n in (1, 2, 3)
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "The Silent Patient"
    assert audiobook.author_guess == "Alex Michaelides"


def test_title_dash_author_without_tags_uses_personal_name_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "The Silent Patient - Alex Michaelides"
    for n in (1, 2):
        _touch(book / f"{n:02d}.mp3")
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "The Silent Patient"
    assert audiobook.author_guess == "Alex Michaelides"


# ── 5. "Title by Author" ──


def test_title_by_author_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Along Came a Spider by James Patterson"
    tag_map = {
        _touch(book / f"{name}.mp3"): AudioTags(
            title=f"{n:02d}-91 Along Came a Spider",
            album="Alex Cross 01",
            artist="James Patterson",
            track=n,
        )
        for n, name in ((1, "3fa2c91e"), (2, "022ae79a"), (3, "b8b27056"))
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "Along Came a Spider"
    assert audiobook.author_guess == "James Patterson"
    assert audiobook.series_guess == "Alex Cross"
    assert audiobook.volume_guess == "1"
    assert [t.track_index for t in audiobook.tracks] == [1, 2, 3]


def test_title_by_author_needs_a_name_after_by() -> None:
    scanner = AudioFileScanner()

    title, author, _series, _volume = scanner._extract_metadata_guesses(
        Path("Stand by Me"), []
    )

    assert (title, author) == ("Stand by Me", None)


def test_nfo_series_with_book_number_is_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Along Came a Spider by James Patterson"
    for n in (1, 2):
        _touch(book / f"{n:02d}.mp3")
    (book / "info.nfo").write_text(
        "Title: Along Came a Spider\nAuthor: James Patterson\n"
        "Series: Alex Cross, Book 1\nRead by: Taye Diggs\n",
        encoding="utf-8",
    )
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.series_guess == "Alex Cross"
    assert audiobook.volume_guess == "1"


# ── 6. "<Series> <NN> - <Title>" ──


def test_series_number_title_single_file_with_artist_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _touch(tmp_path / "Earthsea 01 - A Wizard of Earthsea" / "book.m4b")
    scanner = _scanner(monkeypatch, {path: AudioTags(artist="Ursula K. Le Guin")})

    [audiobook] = scanner.scan_directory(tmp_path)

    assert audiobook.raw_title_guess == "A Wizard of Earthsea"
    assert audiobook.author_guess == "Ursula K. Le Guin"
    assert audiobook.series_guess == "Earthsea"
    assert audiobook.volume_guess == "1"


# ── 7. track order without track-number tags ──


def test_track_order_from_numeric_filename_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "extreme_ownership_jocko"
    tag_map = {
        _touch(
            book / f"Extreme Ownership How U.S. Navy SEALs Lead and Win "
            f"(Unabridged) - {n:03d}.mp3"
        ): AudioTags(
            album="Extreme Ownership (Unabridged)",
            artist="Jocko Willink, Leif Babin",
            albumartist="Jocko Willink, Leif Babin",
        )
        for n in range(1, 13)
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert [t.track_index for t in audiobook.tracks] == list(range(1, 13))
    assert audiobook.warnings == []
    assert audiobook.raw_title_guess == "Extreme Ownership"
    assert audiobook.author_guess == "Jocko Willink, Leif Babin"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Freida McFadden - The Inmate Pt07 of 58", 7),
        ("The Inmate Part 12", 12),
        ("Part 3 of 9", 3),
        ("21-91 Along Came a Spider", 21),
    ],
)
def test_track_order_from_title_tag(title: str, expected: int) -> None:
    scanner = AudioFileScanner()

    assert (
        scanner._get_track_number(Path("inm_989720.mp3"), AudioTags(title=title))
        == expected
    )


def test_title_tag_order_beats_shuffled_file_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "the_inmate"
    order = {"inm_900001.mp3": 3, "inm_100002.mp3": 1, "inm_500003.mp3": 2}
    tag_map = {
        _touch(book / name): AudioTags(
            title=f"Freida McFadden - The Inmate Pt{part:02d} of 3",
            artist="Freida McFadden",
        )
        for name, part in order.items()
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert [t.src_path.name for t in audiobook.tracks] == [
        "inm_100002.mp3",
        "inm_500003.mp3",
        "inm_900001.mp3",
    ]
    assert [t.track_index for t in audiobook.tracks] == [1, 2, 3]


# ── 8. placeholder tags ──


@pytest.mark.parametrize(
    "placeholder",
    ["Unknown Book-Part1", "Unknown", "Track 01", "Untitled", "Unknown Album"],
)
def test_placeholder_tags_are_not_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    path = _touch(tmp_path / "Frank Herbert - Dune" / "book.m4b")
    scanner = _scanner(
        monkeypatch, {path: AudioTags(title=placeholder, album=placeholder)}
    )

    [audiobook] = scanner.scan_directory(tmp_path)

    assert audiobook.raw_title_guess == "Dune"
    assert audiobook.author_guess == "Frank Herbert"


def test_placeholder_album_on_multi_track_set_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Dune"
    tag_map = {
        _touch(book / f"{n:02d}.mp3"): AudioTags(
            album="Unknown Album", artist="Frank Herbert", track=n
        )
        for n in (1, 2)
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.raw_title_guess == "Dune"


def test_narrator_artist_tag_is_not_the_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "What If？ Serious Scientific Answers…"
    tag_map = {
        _touch(book / f"{n:02d} - Chapter.mp3"): AudioTags(
            album="What If?: Serious Scientific Answers",
            artist="Wil Wheaton",
            track=n,
            comment="Read by Wil Wheaton",
        )
        for n in (1, 2)
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.author_guess is None
    assert audiobook.narrator_guess == "Wil Wheaton"


# ── 9. one duplicate book must not block the whole plan ──


def _gone_girl_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    library = tmp_path / "lib"
    tags = AudioTags(title="Gone Girl", album="Gone Girl", artist="Gillian Flynn")
    tag_map = {
        _touch(
            library / "Gillian Flynn - Gone Girl" / "Gillian Flynn - Gone Girl.m4b"
        ): tags,
        _touch(library / "gone girl - Copy" / "Gillian Flynn - Gone Girl.m4b"): tags,
        _touch(library / "Stephen King - It" / "It.m4b"): AudioTags(
            album="It", artist="Stephen King"
        ),
    }
    _touch(library / "Gillian Flynn - Gone Girl" / "cover.jpg")
    _touch(library / "gone girl - Copy" / "cover.jpg")
    scanner = _scanner(monkeypatch, tag_map)
    return library, scanner.scan_directory(library)


def test_duplicate_book_is_skipped_and_rest_of_plan_stays_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, sets = _gone_girl_library(tmp_path, monkeypatch)

    plan = PlanBuilder(Config()).create_plan(library, sets)

    assert plan.conflicts == []
    assert [item["source_path"] for item in plan.skipped] == [
        str(library / "gone girl - Copy")
    ]
    reason = plan.skipped[0]["reason"]
    assert "Gillian Flynn - Gone Girl" in reason
    assert "same destination" in reason
    moved_from = {op.old_path.parent.name for op in plan.operations}
    assert moved_from == {"Gillian Flynn - Gone Girl", "Stephen King - It"}
    copy_set = next(s for s in plan.audiobook_sets if "Copy" in s.source_path.name)
    assert copy_set.needs_review is True
    assert copy_set.review_reason == reason

    # A saved plan reloads (and re-validates) the same way.
    plan_file = tmp_path / "plan.json"
    save_plan(plan, plan_file)
    reloaded = load_plan(plan_file)
    assert reloaded.conflicts == []
    assert reloaded.skipped == plan.skipped


def test_cli_scan_and_apply_with_duplicate_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bookbot.cli import cli

    library, sets = _gone_girl_library(tmp_path, monkeypatch)
    monkeypatch.setattr(
        AudioFileScanner, "scan_directory", lambda self, path, progress=None: sets
    )
    plan_file = tmp_path / "plan.json"
    config_dir = tmp_path / "cfg"
    runner = CliRunner()

    scanned = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "scan",
            str(library),
            "--plan",
            str(plan_file),
        ],
    )
    assert "must be fixed before apply" not in scanned.output
    assert "gone girl - Copy" in scanned.output

    applied = runner.invoke(
        cli, ["--config-dir", str(config_dir), "apply", str(plan_file), "--yes"]
    )
    assert applied.exit_code == 0, applied.output
    assert (library / "gone girl - Copy" / "Gillian Flynn - Gone Girl.m4b").exists()
    assert not (library / "Stephen King - It" / "It.m4b").exists()


def test_set_colliding_with_itself_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bookbot.core.models import AudiobookSet, Track

    library = tmp_path / "lib"
    good = _touch(library / "Frank Herbert - Dune" / "Dune.m4b")
    bad_a = _touch(library / "Broken" / "a.mp3")
    bad_b = _touch(library / "Broken" / "b.mp3")
    sets = [
        AudiobookSet(
            source_path=good.parent,
            raw_title_guess="Dune",
            author_guess="Frank Herbert",
            tracks=[
                Track(
                    src_path=good,
                    disc=1,
                    track_index=1,
                    file_size=1,
                    audio_format=AudioFormat.M4B,
                )
            ],
        ),
        AudiobookSet(
            source_path=bad_a.parent,
            raw_title_guess="Broken",
            author_guess="Someone",
            tracks=[
                Track(
                    src_path=path,
                    disc=1,
                    track_index=1,
                    file_size=1,
                    audio_format=AudioFormat.MP3,
                )
                for path in (bad_a, bad_b)
            ],
        ),
    ]

    plan = PlanBuilder(Config()).create_plan(
        library, sets, file_template="{TrackPad} - {Title}"
    )

    assert plan.conflicts == []
    assert [item["source_path"] for item in plan.skipped] == [str(bad_a.parent)]
    assert {op.old_path for op in plan.operations} >= {good}


def test_organizer_skips_duplicate_book_instead_of_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, sets = _gone_girl_library(tmp_path, monkeypatch)

    plan = SmartOrganizer().propose_reorganization(library, None, "abs", sets)

    assert plan.conflicts == []
    assert [item["source_path"] for item in plan.skipped] == [
        str(library / "gone girl - Copy")
    ]
    assert all("Copy" not in str(op.source) for op in plan.operations)


# ── 10. numeric ordering of CD folders ──


def test_cd_folders_sort_numerically_without_any_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Joe Hill - NOS4A2"
    for disc in range(1, 18):
        _touch(book / f"CD {disc}" / "Track.mp3")
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(book)

    assert [t.src_path.parent.name for t in audiobook.tracks] == [
        f"CD {disc}" for disc in range(1, 18)
    ]


def test_untagged_unnumbered_files_sort_naturally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Frank Herbert - Dune"
    for name in ("dune a10.mp3", "dune a2.mp3", "dune a1.mp3"):
        _touch(book / name)
    scanner = _scanner(monkeypatch)

    [audiobook] = scanner.scan_directory(book)

    assert [t.src_path.name for t in audiobook.tracks] == [
        "dune a1.mp3",
        "dune a2.mp3",
        "dune a10.mp3",
    ]


def test_silence_chapter_detection_keeps_disc_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    book = tmp_path / "Joe Hill - NOS4A2"
    for disc in (1, 2, 10):
        _touch(book / f"CD {disc}" / "Track01.mp3")
    scanner = _scanner(monkeypatch)
    [audiobook] = scanner.scan_directory(book)
    for track in audiobook.tracks:
        track.existing_tags = AudioTags()
    seen: list[list[Path]] = []

    detector = ChapterDetector()
    monkeypatch.setattr(detector, "detect_from_tracks", lambda _s: [])
    monkeypatch.setattr(
        detector,
        "detect_from_silence",
        lambda files, **_kw: seen.append(list(files)) or [],
    )
    for name in ("detect_from_embedded", "detect_from_cue", "detect_from_audnexus"):
        if hasattr(detector, name):
            monkeypatch.setattr(detector, name, lambda *_a, **_k: [])

    asyncio.run(detector.auto_detect(audiobook))

    assert seen, "silence detection was not reached"
    assert [path.parent.name for path in seen[0]] == ["CD 1", "CD 2", "CD 10"]


def test_title_case_keeps_alphanumeric_all_caps_titles() -> None:
    from bookbot.core.templates import TemplateEngine

    engine = TemplateEngine()

    assert engine._smart_title_case("NOS4A2") == "NOS4A2"
    assert engine._smart_title_case("dune part 01 FINAL") == "Dune Part 01 Final"


def test_bracketed_disc_folders_collapse_into_one_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "NOS4A2 by Joe Hill"
    tag_map: dict[Path, AudioTags] = {}
    for disc in (1, 2, 10):
        for track in (1, 2):
            path = _touch(book / f"NOS4A2 (Disc {disc:02d})" / f"Track{track:02d}.mp3")
            tag_map[path] = AudioTags(
                album=f"NOS4A2 (Disc {disc:02d})", artist="Joe Hill", track=track
            )
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(tmp_path)

    assert (audiobook.raw_title_guess, audiobook.author_guess) == ("NOS4A2", "Joe Hill")
    assert audiobook.disc_count == 3
    assert [t.disc for t in audiobook.tracks] == [1, 1, 2, 2, 10, 10]


@pytest.mark.parametrize(
    ("name", "generic"),
    [
        ("audiobook_final_FINAL", True),
        ("New Folder (3)", True),
        ("audio", True),
        ("001", True),
        ("3fa2c91e", True),
        ("1984", False),
        ("11-22-63", False),
        ("the_inmate", False),
        ("The Book Thief", False),
    ],
)
def test_generic_name_detection(name: str, generic: bool) -> None:
    from bookbot.core.discovery import is_generic_name

    assert is_generic_name(name) is generic


def test_comment_narrator_keeps_only_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Randall Munroe - What If"
    tag_map = {
        _touch(book / f"{n:02d}.mp3"): AudioTags(
            album="What If?",
            artist="Randall Munroe",
            track=n,
            comment="Read by Wil Wheaton, with an introduction by the author",
        )
        for n in (1, 2)
    }
    scanner = _scanner(monkeypatch, tag_map)

    [audiobook] = scanner.scan_directory(book)

    assert audiobook.narrator_guess == "Wil Wheaton"
    assert audiobook.author_guess == "Randall Munroe"
