"""Follow-ups to the Audiobookshelf metadata.json style (#6 review)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import toml
from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager
from bookbot.config.models import Config
from bookbot.core.doctor import LibraryDoctor
from bookbot.core.models import AudiobookSet, ProviderIdentity
from bookbot.io.sidecar import SidecarManager
from bookbot.providers.local import LocalMetadataProvider

IDENTITY = ProviderIdentity(
    provider="x",
    external_id="1",
    title="All Systems Red",
    authors=["Martha Wells"],
    narrator="Kevin R. Free",
    series_name="The Murderbot Diaries",
    series_index="1",
    year=2017,
)


# ── Codex: local provider must read ABS-style files ──


def test_local_provider_reads_abs_metadata_json(tmp_path: Path) -> None:
    book = tmp_path / "book"
    book.mkdir()
    SidecarManager().write_metadata_json(
        book / "metadata.json", IDENTITY, style="audiobookshelf"
    )
    provider = LocalMetadataProvider()

    candidates = asyncio.run(
        provider.find_matches(AudiobookSet(source_path=book), limit=1)
    )

    assert candidates
    identity = candidates[0].identity
    assert identity.series_name == "The Murderbot Diaries"
    assert identity.series_index == "1"
    assert identity.narrator == "Kevin R. Free"
    assert identity.year == 2017


# ── Codex: merging must not keep stale aliases of updated fields ──


def test_abs_write_over_booklore_drops_stale_aliases(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    old = {
        "title": "Old",
        "series": "Old Series",
        "seriesIndex": "9",
        "narrator": "Old Narrator",
        "year": 1999,
        "chapters": [],
    }
    manager = SidecarManager()
    manager.write_metadata_json(path, IDENTITY, style="audiobookshelf", existing=old)

    data = json.loads(path.read_text())
    assert "seriesIndex" not in data
    assert "narrator" not in data
    assert "year" not in data
    assert data["chapters"] == []
    read = manager.read_metadata_json(path)
    assert read is not None
    assert (read.series_name, read.series_index) == ("The Murderbot Diaries", "1")
    assert read.narrator == "Kevin R. Free"
    assert read.year == 2017


def test_booklore_write_over_abs_drops_stale_aliases(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    old = {
        "title": "Old",
        "series": ["Old Series #9"],
        "narrators": ["Old Narrator"],
        "publishedYear": "1999",
        "tags": ["kept"],
    }
    manager = SidecarManager()
    manager.write_metadata_json(path, IDENTITY, style="booklore", existing=old)

    data = json.loads(path.read_text())
    assert "narrators" not in data
    assert "publishedYear" not in data
    assert data["tags"] == ["kept"]
    read = manager.read_metadata_json(path)
    assert read is not None
    assert read.narrator == "Kevin R. Free"
    assert read.year == 2017


# ── Saved built-in profiles predating metadata_json_style ──


def _strip_style_from_profile(config_dir: Path, name: str) -> Path:
    path = config_dir / "profiles" / f"{name}.toml"
    data = toml.load(path)
    del data["config"]["output"]["metadata_json_style"]
    path.write_text(toml.dumps(data))
    return path


def test_old_audiobookshelf_profile_gets_builtin_style(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    ConfigManager(config_dir)
    _strip_style_from_profile(config_dir, "audiobookshelf")

    profile = ConfigManager(config_dir).load_profile("audiobookshelf")

    assert profile is not None
    assert profile.config.output.metadata_json_style == "audiobookshelf"


def test_explicit_style_in_profile_file_wins(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    ConfigManager(config_dir)
    path = config_dir / "profiles" / "audiobookshelf.toml"
    data = toml.load(path)
    data["config"]["output"]["metadata_json_style"] = "booklore"
    path.write_text(toml.dumps(data))

    profile = ConfigManager(config_dir).load_profile("audiobookshelf")

    assert profile is not None
    assert profile.config.output.metadata_json_style == "booklore"


def test_custom_profile_is_not_filled_from_builtins(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    ConfigManager(config_dir)
    path = config_dir / "profiles" / "mine.toml"
    path.write_text(toml.dumps({"name": "mine", "description": "", "config": {}}))

    profile = ConfigManager(config_dir).load_profile("mine")

    assert profile is not None
    assert profile.config.output.metadata_json_style == "booklore"


def test_using_old_profile_writes_audiobookshelf_style(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    ConfigManager(config_dir)
    path = _strip_style_from_profile(config_dir, "audiobookshelf")

    manager = ConfigManager(config_dir)
    assert manager.apply_profile("audiobookshelf")

    saved = toml.load(path)
    assert saved["config"]["output"]["metadata_json_style"] == "audiobookshelf"
    assert manager.load_config().output.metadata_json_style == "audiobookshelf"


# ── doctor flags ABS files written while the style is booklore ──


def _library_with_abs_json(root: Path) -> Path:
    book = root / "lib" / "Martha Wells" / "All Systems Red"
    book.mkdir(parents=True)
    SidecarManager().write_metadata_json(
        book / "metadata.json", IDENTITY, style="audiobookshelf"
    )
    return root / "lib"


def test_doctor_warns_when_abs_files_meet_booklore_style(tmp_path: Path) -> None:
    library = _library_with_abs_json(tmp_path)
    report = LibraryDoctor(Config(), tmp_path / "cfg").run(library)

    warnings = [c.message for c in report.checks if c.status == "warn"]
    hint = "bookbot config set output.metadata_json_style audiobookshelf"
    assert any(hint in message for message in warnings), warnings


def test_doctor_quiet_when_style_is_audiobookshelf(tmp_path: Path) -> None:
    library = _library_with_abs_json(tmp_path)
    config = Config()
    config.output.metadata_json_style = "audiobookshelf"
    report = LibraryDoctor(config, tmp_path / "cfg").run(library)

    assert not any("metadata_json_style" in c.message for c in report.checks)


def test_config_set_accepts_the_style(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    result = CliRunner().invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "config",
            "set",
            "output.metadata_json_style",
            "audiobookshelf",
        ],
    )
    assert result.exit_code == 0, result.output
    style = ConfigManager(config_dir).load_config().output.metadata_json_style
    assert style == "audiobookshelf"
