"""Regression tests for audio tag normalization across container formats."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TCOM, TIT2, TPE1, TPOS, TRCK, TSSE, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import TrackStatus
from bookbot.core.planning import PlanBuilder, save_plan
from bookbot.tui.app import BookBotApp

_FFMPEG = shutil.which("ffmpeg")


def _write_silent_audio(path: Path, codec_args: list[str]) -> None:
    if _FFMPEG is None:
        pytest.skip("ffmpeg is required for container tag extraction tests")

    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-t",
            "0.1",
            *codec_args,
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_tagged_mp3(
    path: Path,
    *,
    narrator: str | None = None,
    narrator_frame: str = "NARRATEDBY",
    composer: str | None = None,
) -> None:
    _write_silent_audio(path, ["-c:a", "libmp3lame"])

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["The Title"]))
    audio.tags.add(TALB(encoding=3, text=["The Album"]))
    audio.tags.add(TPE1(encoding=3, text=["The Artist"]))
    audio.tags.add(TRCK(encoding=3, text=["3/12"]))
    audio.tags.add(TPOS(encoding=3, text=["1/2"]))
    audio.tags.add(TSSE(encoding=3, text=["Lavf"]))
    if narrator is not None:
        audio.tags.add(TXXX(encoding=3, desc=narrator_frame, text=[narrator]))
    if composer is not None:
        audio.tags.add(TCOM(encoding=3, text=[composer]))
    audio.save()


def _write_tagged_m4b(
    path: Path,
    *,
    narrator: str | None = None,
    composer: str | None = None,
) -> None:
    _write_silent_audio(path, ["-c:a", "aac"])

    audio = MP4(path)
    if narrator is not None:
        audio["\xa9nrt"] = [narrator]
    if composer is not None:
        audio["\xa9wrt"] = [composer]
    audio.save()


def _write_tagged_flac(path: Path, *, narrator: str | None = None) -> None:
    _write_silent_audio(path, ["-c:a", "flac"])

    audio = FLAC(path)
    if narrator is not None:
        audio["narrator"] = [narrator]
    audio.save()


def _scan_single_file(tmp_path: Path, filename: str) -> str | None:
    audiobook_sets = AudioFileScanner().scan_directory(tmp_path)

    assert len(audiobook_sets) == 1
    assert audiobook_sets[0].tracks[0].src_path.name == filename
    return audiobook_sets[0].narrator_guess


class _FakeUrlFrame:
    def __init__(self, url: str) -> None:
        self.url = url


def _fake_asin_mutagen_file(file_path: Path) -> dict[str, object]:
    if file_path.name == "The Divorce - Freida McFadden.m4b":
        return {
            "\xa9alb": ["The Divorce"],
            "\xa9ART": ["Freida McFadden"],
            "asin": ["B0GL9665Q1"],
            "----:com.pilabor.tone:AUDIBLE_ASIN": [b"B0GL9665Q1"],
        }
    if file_path.name == "Can't Hurt Me....mp3":
        return {
            "TALB": "Can't Hurt Me",
            "TPE1": "David Goggins",
            "WOAS": _FakeUrlFrame("https://www.audible.com/pd/B07KKPGDZF"),
        }
    raise AssertionError(f"Unexpected path for fake mutagen tags: {file_path}")


@pytest.mark.requires_ffmpeg
def test_extract_audio_tags_normalizes_id3_frames(tmp_path: Path) -> None:
    mp3_path = tmp_path / "01 - tagged.mp3"
    _write_tagged_mp3(mp3_path)

    tags = AudioFileScanner()._extract_audio_tags(mp3_path)

    assert tags.title == "The Title"
    assert tags.album == "The Album"
    assert tags.artist == "The Artist"
    assert tags.track == 3
    assert tags.disc == 1


def test_scan_directory_extracts_asin_guess_from_mp4_audible_asin_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "The Divorce - Freida McFadden.m4b"
    file_path.touch()

    monkeypatch.setattr(
        "bookbot.core.discovery.MutagenFile", _fake_asin_mutagen_file
    )
    monkeypatch.setattr(
        AudioFileScanner,
        "_extract_audio_properties",
        lambda self, _: (8_000.0, None, None, None),
    )

    audiobook_sets = AudioFileScanner().scan_directory(tmp_path)

    assert len(audiobook_sets) == 1
    assert audiobook_sets[0].asin_guess == "B0GL9665Q1"


def test_scan_directory_extracts_asin_guess_from_woas_audible_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_path = tmp_path / "Can't Hurt Me....mp3"
    file_path.touch()

    monkeypatch.setattr(
        "bookbot.core.discovery.MutagenFile", _fake_asin_mutagen_file
    )
    monkeypatch.setattr(
        AudioFileScanner,
        "_extract_audio_properties",
        lambda self, _: (8_000.0, None, None, None),
    )

    audiobook_sets = AudioFileScanner().scan_directory(tmp_path)

    assert len(audiobook_sets) == 1
    assert audiobook_sets[0].asin_guess == "B07KKPGDZF"


def test_scan_cli_plan_json_includes_asin_guess_from_embedded_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m4b_path = tmp_path / "The Divorce - Freida McFadden.m4b"
    mp3_path = tmp_path / "Can't Hurt Me....mp3"
    m4b_path.touch()
    mp3_path.touch()

    monkeypatch.setattr(
        "bookbot.core.discovery.MutagenFile", _fake_asin_mutagen_file
    )
    monkeypatch.setattr(
        AudioFileScanner,
        "_extract_audio_properties",
        lambda self, _: (8_000.0, None, None, None),
    )

    plan_path = tmp_path / "scan-plan.json"
    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(tmp_path / "config"),
            "scan",
            str(tmp_path),
            "--plan",
            str(plan_path),
        ],
    )

    assert result.exit_code == 0
    assert "ASIN: B0GL9665Q1" in result.output
    assert "ASIN: B07KKPGDZF" in result.output

    plan_data = json.loads(plan_path.read_text())
    assert {entry["asin_guess"] for entry in plan_data["audiobook_sets"]} == {
        "B0GL9665Q1",
        "B07KKPGDZF",
    }


@pytest.mark.requires_ffmpeg
def test_plan_serialization_handles_tsse_raw_tags(tmp_path: Path) -> None:
    book_dir = tmp_path / "Author - Title"
    book_dir.mkdir()
    mp3_path = book_dir / "01 - tagged.mp3"
    _write_tagged_mp3(mp3_path)

    audiobook_sets = AudioFileScanner().scan_directory(tmp_path)
    raw_tags = audiobook_sets[0].tracks[0].existing_tags.raw_tags
    assert isinstance(raw_tags.get("TSSE"), str)

    config = ConfigManager(tmp_path / "config").load_config()
    plan = PlanBuilder(config).create_plan(
        library_root=tmp_path,
        audiobook_sets=audiobook_sets,
        source_roots=[tmp_path],
    )

    save_plan(plan, tmp_path / "plan.json")

    assert (tmp_path / "plan.json").exists()


@pytest.mark.requires_ffmpeg
def test_extract_audio_tags_normalizes_mp4_atoms(tmp_path: Path) -> None:
    mp4_path = tmp_path / "01 - tagged.m4a"
    _write_silent_audio(mp4_path, ["-c:a", "aac"])

    audio = MP4(mp4_path)
    audio["\xa9nam"] = ["The Title"]
    audio["\xa9alb"] = ["The Album"]
    audio["\xa9ART"] = ["The Artist"]
    audio["trkn"] = [(3, 12)]
    audio["disk"] = [(1, 2)]
    audio["----:com.apple.iTunes:ASIN"] = [MP4FreeForm(b"b0abcdefgh")]
    audio.save()

    tags = AudioFileScanner()._extract_audio_tags(mp4_path)

    assert tags.title == "The Title"
    assert tags.album == "The Album"
    assert tags.artist == "The Artist"
    assert tags.track == 3
    assert tags.disc == 1
    assert tags.asin == "B0ABCDEFGH"


@pytest.mark.requires_ffmpeg
def test_extract_audio_tags_normalizes_vorbis_comments(tmp_path: Path) -> None:
    flac_path = tmp_path / "01 - tagged.flac"
    _write_silent_audio(flac_path, ["-c:a", "flac"])

    audio = FLAC(flac_path)
    audio["title"] = ["The Title"]
    audio["album"] = ["The Album"]
    audio["artist"] = ["The Artist"]
    audio["tracknumber"] = ["3"]
    audio.save()

    tags = AudioFileScanner()._extract_audio_tags(flac_path)

    assert tags.title == "The Title"
    assert tags.album == "The Album"
    assert tags.artist == "The Artist"
    assert tags.track == 3


@pytest.mark.requires_ffmpeg
def test_scan_directory_extracts_narrator_guess_from_mp4_atom(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.m4b"
    _write_tagged_m4b(file_path, narrator="Julia Whelan")

    assert _scan_single_file(tmp_path, file_path.name) == "Julia Whelan"


@pytest.mark.requires_ffmpeg
def test_scan_directory_uses_mp4_composer_as_low_priority_narrator_fallback(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.m4b"
    _write_tagged_m4b(file_path, composer="Danny Campbell")

    assert _scan_single_file(tmp_path, file_path.name) == "Danny Campbell"


@pytest.mark.requires_ffmpeg
def test_scan_directory_extracts_narrator_guess_from_mp3_narratedby_frame(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.mp3"
    _write_tagged_mp3(file_path, narrator="Ray Porter")

    assert _scan_single_file(tmp_path, file_path.name) == "Ray Porter"


@pytest.mark.requires_ffmpeg
def test_scan_directory_uses_tcom_as_low_priority_narrator_fallback(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.mp3"
    _write_tagged_mp3(file_path, composer="Bahni Turpin")

    assert _scan_single_file(tmp_path, file_path.name) == "Bahni Turpin"


@pytest.mark.requires_ffmpeg
def test_scan_directory_extracts_narrator_guess_from_vorbis_comments(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.flac"
    _write_tagged_flac(file_path, narrator="George Guidall")

    assert _scan_single_file(tmp_path, file_path.name) == "George Guidall"


@pytest.mark.requires_ffmpeg
def test_scan_directory_extracts_narrator_guess_from_nfo_sidecar(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.mp3"
    _write_tagged_mp3(file_path)
    (tmp_path / "book.nfo").write_text(
        "Title: Book\nRead By: Emma Grant Williams\n",
        encoding="utf-8",
    )

    assert _scan_single_file(tmp_path, file_path.name) == "Emma Grant Williams"


@pytest.mark.requires_ffmpeg
def test_scan_directory_leaves_narrator_guess_empty_without_supported_tags(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "Book.mp3"
    _write_tagged_mp3(file_path)

    assert _scan_single_file(tmp_path, file_path.name) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TRCK(encoding=3, text=["3/12"]), 3),
        ((1, 10), 1),
        ("1/10", 1),
        (b"7/9", 7),
    ],
)
def test_normalize_numeric_tag_handles_mutagen_shapes(
    value: object, expected: int
) -> None:
    scanner = AudioFileScanner()

    assert scanner._normalize_numeric_tag(value) == expected


def test_create_track_from_file_handles_empty_id3_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mp3_path = tmp_path / "01 - empty-frame.mp3"
    mp3_path.touch()

    def fake_mutagen_file(_: Path) -> dict[str, object]:
        return {"TIT2": TIT2(encoding=3, text=[])}

    monkeypatch.setattr("bookbot.core.discovery.MutagenFile", fake_mutagen_file)

    track = AudioFileScanner()._create_track_from_file(mp3_path)

    assert track is not None
    assert track.status == TrackStatus.VALID
    assert track.track_index == 1
    assert track.existing_tags.title is None


def test_scan_directory_keeps_processing_after_bad_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book_dir = tmp_path / "The Book"
    book_dir.mkdir()

    good_one = book_dir / "01 - good.mp3"
    good_two = book_dir / "02 - also-good.mp3"
    bad_file = book_dir / "03 - bad.mp3"

    for file_path in (good_one, good_two, bad_file):
        file_path.touch()

    scanner = AudioFileScanner(recursive=True, max_depth=2)
    original_extract = scanner._extract_audio_tags

    def flaky_extract(file_path: Path):
        if file_path == bad_file:
            raise RuntimeError("simulated tag read failure")
        return original_extract(file_path)

    monkeypatch.setattr(scanner, "_extract_audio_tags", flaky_extract)

    audiobook_sets = scanner.scan_directory(tmp_path)

    assert len(audiobook_sets) == 1

    tracks = audiobook_sets[0].tracks
    assert len(tracks) == 3
    assert {track.src_path.name for track in tracks} == {
        "01 - good.mp3",
        "02 - also-good.mp3",
        "03 - bad.mp3",
    }
    assert sum(track.status == TrackStatus.ERROR for track in tracks) == 1
    assert sum(track.status == TrackStatus.VALID for track in tracks) == 2


@pytest.mark.requires_ffmpeg
def test_tui_scan_survives_mixed_library(tmp_path: Path) -> None:
    library = tmp_path / "The Book"
    library.mkdir()

    _write_tagged_mp3(library / "01 - tagged.mp3")
    (library / "02 - malformed.mp3").write_bytes(b"not a valid mp3")

    config_manager = ConfigManager(tmp_path / "config")
    app = BookBotApp(config_manager, [library])

    async def run_app() -> None:
        async with app.run_test() as pilot:
            for _ in range(30):
                if app.scanning_complete:
                    break
                await pilot.pause(0.1)

        assert app.scanning_complete
        assert len(app.audiobook_sets) == 1
        assert len(app.audiobook_sets[0].tracks) == 2

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_app())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
