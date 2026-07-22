"""Synthetic smoke test for the round-3 user flow."""

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from mutagen.id3 import TIT2, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.core.discovery import AudioFileScanner
from bookbot.core.models import MatchCandidate, MatchConfidence, ProviderIdentity
from bookbot.core.planning import PlanBuilder, save_plan

_FFMPEG = shutil.which("ffmpeg")


def _write_silent_audio(path: Path, codec_args: list[str]) -> None:
    if _FFMPEG is None:
        pytest.skip("ffmpeg is required for the round-3 smoke test")

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


def _write_tagged_mp3(path: Path, *, title: str, track_text: str = "1/1") -> None:
    _write_silent_audio(path, ["-c:a", "libmp3lame"])

    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TRCK(encoding=3, text=[track_text]))
    audio.save()


def _write_tagged_m4b(path: Path, *, title: str) -> None:
    _write_silent_audio(path, ["-c:a", "aac"])

    audio = MP4(path)
    audio["\xa9nam"] = [title]
    audio.save()


class _SmokeMatchProvider:
    async def find_matches_merged(
        self, audiobook_set, limit: int = 10
    ) -> list[MatchCandidate]:
        title = audiobook_set.raw_title_guess or audiobook_set.source_path.name
        return [
            MatchCandidate(
                identity=ProviderIdentity(
                    provider="openlibrary",
                    external_id=title.lower().replace(" ", "-"),
                    title=title,
                    authors=[audiobook_set.author_guess or "Unknown"],
                    raw_data={"providers": ["openlibrary", "audnexus"]},
                ),
                confidence=0.96,
                confidence_level=MatchConfidence.HIGH,
                match_reasons=[
                    "Corroborated by 2 providers",
                    "ASIN exact match",
                ],
            )
        ][:limit]

    async def close_all(self) -> None:
        return None


@pytest.mark.requires_ffmpeg
def test_round3_synthetic_user_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "library"
    config_dir = tmp_path / "config"
    runner = CliRunner()

    multi_disc = library / "Author - Multi Disc Book"
    multi_disc.mkdir(parents=True)
    for disc_number in range(1, 4):
        disc_dir = multi_disc / f"Multi Disc Book Disc {disc_number}"
        disc_dir.mkdir()
        _write_tagged_mp3(
            disc_dir / "01 - Chapter.mp3",
            title=f"Disc {disc_number}",
        )

    duplicate_one = library / "Loose Duplicate One.m4b"
    duplicate_two = library / "Loose Duplicate Two.m4b"
    _write_tagged_m4b(duplicate_one, title="Loose Duplicate")
    duplicate_two.write_bytes(duplicate_one.read_bytes())

    eb_dir = library / "Donald J. Sobol - Encyclopedia Brown"
    eb_dir.mkdir()
    _write_tagged_m4b(
        eb_dir / "EB 01 - Encyclopedia Brown and the Case.m4b",
        title="Encyclopedia Brown and the Case",
    )

    gap_dir = library / "Gap Book"
    gap_dir.mkdir()
    _write_tagged_mp3(
        gap_dir / "Single Chapter.mp3",
        title="Single Chapter",
        track_text="14/15",
    )

    scanner = AudioFileScanner(recursive=True, max_depth=5)
    audiobook_sets = scanner.scan_directory(library)

    assert len(audiobook_sets) == 5
    by_source = {book.source_path.name: book for book in audiobook_sets}
    assert by_source["Author - Multi Disc Book"].raw_title_guess == "Multi Disc Book"
    assert [track.disc for track in by_source["Author - Multi Disc Book"].tracks] == [
        1,
        2,
        3,
    ]
    assert by_source["Donald J. Sobol - Encyclopedia Brown"].author_guess == (
        "Donald J. Sobol"
    )
    assert not any(
        "gaps in track numbering" in warning
        for warning in by_source["Gap Book"].warnings
    )

    config = ConfigManager(config_dir).load_config()
    plan = PlanBuilder(config).create_plan(
        library_root=library,
        audiobook_sets=audiobook_sets,
        source_roots=[library],
    )
    plan_path = tmp_path / "scan-plan.json"
    save_plan(plan, plan_path)
    assert plan_path.exists()

    monkeypatch.setattr(
        "bookbot.cli._build_matching_provider",
        lambda config_manager, metadata_from_files=False: _SmokeMatchProvider(),
    )
    match_result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "match", str(library), "--limit", "1"],
    )
    assert match_result.exit_code == 0
    assert "Corroborated by 2 providers" in match_result.output
    assert "ASIN exact match" in match_result.output
    assert "Providers: openlibrary, audnexus" in match_result.output

    dry_run = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "dedupe", str(library)],
    )
    assert dry_run.exit_code == 0
    assert "Byte-identical files: 1 group(s)" in dry_run.output
    assert str(duplicate_one) in dry_run.output
    assert str(multi_disc) not in dry_run.output
    assert "Quarantine operations: 1" in dry_run.output

    duplicate_bytes = {
        duplicate_one: duplicate_one.read_bytes(),
        duplicate_two: duplicate_two.read_bytes(),
    }
    apply_result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "dedupe", str(library), "--apply"],
    )
    assert apply_result.exit_code == 0
    transaction_id = apply_result.output.strip().split("bookbot undo ")[-1].strip()
    transaction_log = config_dir / "logs" / f"transaction_{transaction_id}.json"
    assert transaction_log.exists()

    history_result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "history", "--days", "365"],
    )
    assert history_result.exit_code == 0
    assert f"{transaction_id[:8]}..." in history_result.output
    assert "dedupe -" in history_result.output

    undo_result = runner.invoke(
        cli,
        ["--config-dir", str(config_dir), "undo", transaction_id],
    )
    assert undo_result.exit_code == 0
    assert duplicate_one.read_bytes() == duplicate_bytes[duplicate_one]
    assert duplicate_two.read_bytes() == duplicate_bytes[duplicate_two]
    assert not (library / ".bookbot-quarantine").exists()
    assert transaction_log.with_suffix(".undone").exists()
