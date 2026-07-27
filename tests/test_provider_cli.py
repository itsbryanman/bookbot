"""Tests for provider CLI management commands."""

from click.testing import CliRunner

from bookbot.cli import cli
from bookbot.config.manager import ConfigManager


def test_provider_set_marketplace_updates_audible_and_audnexus(tmp_path) -> None:
    runner = CliRunner()
    config_dir = tmp_path / "config"

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "provider",
            "set-marketplace",
            "UK",
        ],
    )

    assert result.exit_code == 0
    assert "audible=UK" in result.output
    assert "audnexus=uk" in result.output

    config = ConfigManager(config_dir).load_config()
    assert config.providers.audible.marketplace == "UK"
    assert config.providers.audnexus.marketplace == "uk"


def test_provider_list_shows_effective_priority_order(tmp_path) -> None:
    runner = CliRunner()
    config_dir = tmp_path / "config"

    result = runner.invoke(
        cli,
        [
            "--config-dir",
            str(config_dir),
            "provider",
            "list",
        ],
    )

    assert result.exit_code == 0
    assert "Effective priority order:" in result.output
    assert "1. audnexus [primary]" in result.output
    assert "2. audible" in result.output
    assert "3. openlibrary" in result.output
