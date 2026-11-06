"""Discovery on Author/Series/NN - Title layouts and album-as-series tags."""

from pathlib import Path

import pytest

from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import AudioTags


def _scanner(
    monkeypatch: pytest.MonkeyPatch, tags: dict[str, AudioTags] | None = None
) -> AudioFileScanner:
    scanner = AudioFileScanner()
    tag_map = tags or {}

    def fake_tags(file_path: Path) -> AudioTags:
        return tag_map.get(file_path.name, AudioTags()).model_copy()

    def fake_properties(
        file_path: Path,
    ) -> tuple[float | None, int | None, int | None, int | None]:
        return 600.0, None, None, None

    monkeypatch.setattr(scanner, "_extract_audio_tags", fake_tags)
    monkeypatch.setattr(scanner, "_extract_audio_properties", fake_properties)
    return scanner


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    return path


def test_author_series_numbered_title_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "Martha Wells" / "The Murderbot Diaries" / "01 - All Systems Red"
    _touch(book / "01 - Part 1.mp3")
    _touch(book / "02 - Part 2.mp3")

    sets = _scanner(monkeypatch).scan_directory(tmp_path)

    assert len(sets) == 1
    found = sets[0]
    assert found.raw_title_guess == "All Systems Red"
    assert found.author_guess == "Martha Wells"
    assert found.series_guess == "The Murderbot Diaries"
    assert found.volume_guess == "1"


def test_folder_title_wins_over_album_tag_naming_the_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "All Systems Red"
    tags = {
        name: AudioTags(
            album="The Murderbot Diaries", artist="Martha Wells", track=index
        )
        for index, name in enumerate(("01.mp3", "02.mp3"), start=1)
    }
    for name in tags:
        _touch(book / name)

    sets = _scanner(monkeypatch, tags).scan_directory(tmp_path)

    assert len(sets) == 1
    found = sets[0]
    assert found.raw_title_guess == "All Systems Red"
    assert found.author_guess == "Martha Wells"
    assert found.series_guess == "The Murderbot Diaries"
    assert found.needs_review is True
    assert found.review_reason is not None
    assert "album" in found.review_reason.lower()


def test_track_titles_backing_the_folder_skip_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "All Systems Red"
    tags = {
        name: AudioTags(
            album="The Murderbot Diaries",
            artist="Martha Wells",
            title=f"All Systems Red - Part {index}",
            track=index,
        )
        for index, name in enumerate(("01.mp3", "02.mp3"), start=1)
    }
    for name in tags:
        _touch(book / name)

    found = _scanner(monkeypatch, tags).scan_directory(tmp_path)[0]

    assert found.raw_title_guess == "All Systems Red"
    assert found.series_guess == "The Murderbot Diaries"
    assert found.needs_review is False
