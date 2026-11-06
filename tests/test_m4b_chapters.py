"""M4B conversion: chapter timing, naming, FFMETADATA escaping, tagging."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.convert.ffmpeg import (
    FFmpegWrapper,
    escape_ffmetadata,
    validate_chapter_timeline,
)
from bookbot.convert.pipeline import ConversionOperation, ConversionPipeline
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import ProviderIdentity
from bookbot.core.templates import derive_track_title

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)

DURATIONS = (7.03, 14.03, 21.03)
NAMES = ("01 - Part 1.mp3", "02 - Prelude.mp3", "03 - A; B = C # D.mp3")


def _sine_mp3(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-y",
            str(path),
        ],
        check=True,
    )


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_chapters",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _audio_duration(probe: dict) -> float:
    return next(
        float(stream["duration"])
        for stream in probe["streams"]
        if stream["codec_type"] == "audio"
    )


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    root = tmp_path_factory.mktemp("m4b")
    book = root / "book"
    book.mkdir()
    for name, seconds in zip(NAMES, DURATIONS, strict=True):
        _sine_mp3(book / name, seconds)

    config_manager = ConfigManager(root / "cfg")
    config = config_manager.load_config().conversion.model_copy(
        update={"output_directory": root / "out", "write_cover_art": False}
    )
    audiobook_set = AudioFileScanner().scan_directory(book)[0]
    output = root / "out" / "book.m4b"
    pipeline = ConversionPipeline(config_manager)
    asyncio.run(
        pipeline._execute_operation(ConversionOperation(audiobook_set, output, config))
    )
    return {"output": output, "probe": _probe(output), "root": root}


@requires_ffmpeg
def test_chapter_ends_match_real_boundaries(converted: dict) -> None:
    chapters = converted["probe"]["chapters"]
    assert len(chapters) == 3
    expected_end = 0.0
    for chapter, seconds in zip(chapters, DURATIONS, strict=True):
        expected_end += seconds
        assert abs(float(chapter["end_time"]) - expected_end) <= 0.05, chapter
    assert float(chapters[0]["start_time"]) == 0.0
    for left, right in zip(chapters, chapters[1:], strict=False):
        assert left["end_time"] == right["start_time"]


@requires_ffmpeg
def test_last_chapter_end_within_stream_duration(converted: dict) -> None:
    probe = converted["probe"]
    last_end = float(probe["chapters"][-1]["end_time"])
    assert last_end <= _audio_duration(probe) + 1e-3


@requires_ffmpeg
def test_auto_chapter_names_come_from_filenames(converted: dict) -> None:
    titles = [c["tags"]["title"] for c in converted["probe"]["chapters"]]
    assert titles == ["Part 1", "Prelude", "A; B = C # D"]


def test_derive_track_title() -> None:
    assert derive_track_title("01 - Prelude") == "Prelude"
    assert derive_track_title("03_The_End") == "The End"
    assert derive_track_title("01") is None
    assert derive_track_title("Track 07") is None


def test_escape_ffmetadata() -> None:
    assert escape_ffmetadata("a=b;c#d\\e\nf") == "a\\=b\\;c\\#d\\\\e\\\nf"
    assert escape_ffmetadata("plain") == "plain"


def test_chapter_metadata_writers_escape_titles(tmp_path: Path) -> None:
    from bookbot.chapters.models import Chapter

    wrapper = FFmpegWrapper.__new__(FFmpegWrapper)
    path = wrapper._write_chapter_metadata(
        [Chapter(title="A; B = C # D", start_ms=0, end_ms=1000, source="test")]
    )
    try:
        assert "title=A\\; B \\= C \\# D\n" in path.read_text()
    finally:
        path.unlink()


@requires_ffmpeg
def test_title_with_special_characters_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "in.mp3"
    _sine_mp3(source, 2.0)
    m4b = tmp_path / "out.m4b"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-c:a", "aac", str(m4b)],
        check=True,
    )
    wrapper = FFmpegWrapper()
    title = "Part #1; x = y \\ z"
    assert wrapper.add_chapters(m4b, [{"title": title, "start": 0.0, "end": 1.0}])
    assert [c.title for c in wrapper.extract_chapters(m4b)] == [title]


@pytest.mark.parametrize(
    ("chapters", "duration", "message"),
    [
        ([{"start": 0, "end": 5}, {"start": 4, "end": 9}], 10.0, "overlap"),
        ([{"start": 0, "end": 5}, {"start": 10, "end": 12}], 10.0, "starts at"),
        ([{"start": 3, "end": 3}], 10.0, "ends at"),
    ],
)
def test_validate_chapter_timeline_rejects_bad_chapters(
    chapters: list[dict], duration: float, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_chapter_timeline(chapters, duration)


def test_validate_chapter_timeline_clamps_last_end() -> None:
    chapters = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.2}]
    validate_chapter_timeline(chapters, 10.0)
    assert chapters[-1]["end"] == 10.0


def test_cover_embed_failure_is_reported(tmp_path: Path) -> None:
    pipeline = ConversionPipeline(ConfigManager(tmp_path / "cfg"))
    cover = tmp_path / "cover.jpg"

    async def fake_download(url: str, temp_dir: Path) -> Path:
        cover.write_bytes(b"jpg")
        return cover

    class _Wrapper:
        def embed_cover_art(self, file_path: Path, cover_path: Path) -> bool:
            return False

    pipeline._download_cover = fake_download  # type: ignore[method-assign]
    pipeline._ffmpeg = _Wrapper()  # type: ignore[assignment]
    identity = ProviderIdentity(
        provider="x", external_id="1", title="T", cover_urls=["https://e/c.jpg"]
    )

    asyncio.run(pipeline._embed_cover_art(tmp_path / "book.m4b", identity))

    assert any("cover" in warning.lower() for warning in pipeline.warnings)


# ── m4b tag on files that already carry an embedded JPEG (not reproduced) ──


def _jpeg(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64",
            "-frames:v",
            "1",
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


def _m4b_with_ffmpeg_cover(tmp_path: Path) -> Path:
    source = tmp_path / "in.mp3"
    _sine_mp3(source, 2.0)
    jpeg = _jpeg(tmp_path / "embedded.jpg")
    out = tmp_path / "ffmpeg-cover.m4b"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-i",
            str(jpeg),
            "-map",
            "0:a",
            "-map",
            "1",
            "-c:a",
            "aac",
            "-c:v",
            "mjpeg",
            "-disposition:v:0",
            "attached_pic",
            str(out),
        ],
        check=True,
    )
    return out


def _m4b_with_mutagen_cover(tmp_path: Path) -> Path:
    from mutagen.mp4 import MP4, MP4Cover

    source = tmp_path / "in.mp3"
    _sine_mp3(source, 2.0)
    out = tmp_path / "mutagen-cover.m4b"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-c:a",
            "aac",
            "-f",
            "mp4",
            str(out),
        ],
        check=True,
    )
    audio = MP4(out)
    jpeg = _jpeg(tmp_path / "mutagen.jpg").read_bytes()
    audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    return out


@requires_ffmpeg
@pytest.mark.parametrize("builder", [_m4b_with_ffmpeg_cover, _m4b_with_mutagen_cover])
@pytest.mark.parametrize("with_cover", [False, True])
def test_m4b_tag_on_file_with_embedded_jpeg(
    tmp_path: Path,
    builder,
    with_cover: bool,  # type: ignore[no-untyped-def]
) -> None:
    m4b = builder(tmp_path)
    args = ["m4b", "tag", str(m4b), "--title", "New Title"]
    if with_cover:
        args += ["--cover", str(_jpeg(tmp_path / "new.jpg"))]

    result = CliRunner().invoke(cli, ["--config-dir", str(tmp_path / "cfg"), *args])

    assert result.exit_code == 0, result.output
    probe = _probe(m4b)
    assert probe["format"]["tags"]["title"] == "New Title"
    assert any(
        stream.get("disposition", {}).get("attached_pic") == 1
        for stream in probe["streams"]
    )


def test_m4b_tag_error_includes_ffmpeg_version_and_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m4b = tmp_path / "book.m4b"
    m4b.write_bytes(b"not really an m4b")

    class _Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "-version" in cmd:
            return _Result(0, stdout="ffmpeg version 9.9.9-test Copyright\n")
        return _Result(1, stderr="line one\nInvalid data found when processing input")

    monkeypatch.setattr("bookbot.convert.ffmpeg.subprocess.run", fake_run)

    result = CliRunner().invoke(
        cli,
        ["--config-dir", str(tmp_path / "cfg"), "m4b", "tag", str(m4b), "--title", "T"],
    )

    assert result.exit_code == 1
    assert "9.9.9-test" in result.output
    assert "Invalid data found when processing input" in result.output
