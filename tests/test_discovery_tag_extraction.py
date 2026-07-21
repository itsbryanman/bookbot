"""Regression tests for audio tag normalization across container formats."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TIT2, TPE1, TPOS, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm

from bookbot.config.manager import ConfigManager
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import TrackStatus
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


def _write_tagged_mp3(path: Path) -> None:
    _write_silent_audio(path, ["-c:a", "libmp3lame"])

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=["The Title"]))
    audio.tags.add(TALB(encoding=3, text=["The Album"]))
    audio.tags.add(TPE1(encoding=3, text=["The Artist"]))
    audio.tags.add(TRCK(encoding=3, text=["3/12"]))
    audio.tags.add(TPOS(encoding=3, text=["1/2"]))
    audio.save()


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
