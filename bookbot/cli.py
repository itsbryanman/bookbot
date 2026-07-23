"""Command-line interface for BookBot."""

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from .abs.client import AudiobookshelfClient
    from .core.models import AudiobookSet

from . import __version__
from .config.manager import ConfigManager
from .config.models import Config, Profile
from .core.discovery import AudioFileScanner
from .core.doctor import LibraryDoctor
from .core.operations import TransactionManager
from .core.planning import (
    PlanBuilder,
    format_plan_diff,
    format_plan_summary,
    load_plan,
    save_plan,
)


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--config-dir", type=click.Path(path_type=Path), help="Configuration directory path"
)
@click.pass_context
def cli(ctx: click.Context, config_dir: Path | None) -> None:
    """BookBot - A cross-platform TUI audiobook renamer and organizer."""
    # Ensure that ctx.obj exists and is a dict
    ctx.ensure_object(dict)

    # Initialize configuration manager
    ctx.obj["config_manager"] = ConfigManager(config_dir)


def _require_profile(config_manager: ConfigManager, profile_name: str) -> Profile:
    """Load a profile or exit with a helpful error."""
    profile = config_manager.load_profile(profile_name)
    if profile is None:
        click.echo(f"Error: Profile '{profile_name}' not found", err=True)
        sys.exit(1)
    return profile


def _resolve_config(
    config_manager: ConfigManager, profile_name: str | None
) -> tuple[Config, Profile | None]:
    """Return effective config without mutating the user's saved default config."""
    if profile_name:
        profile = _require_profile(config_manager, profile_name)
        return profile.config.model_copy(deep=True), profile
    return config_manager.load_config().model_copy(deep=True), None


def _build_matching_provider(
    config_manager: ConfigManager,
    *,
    metadata_from_files: bool = False,
):
    """Build the configured metadata provider stack for matching flows."""
    if metadata_from_files:
        from .providers.local import LocalMetadataProvider

        return LocalMetadataProvider()

    from .providers.manager import ProviderManager

    return ProviderManager(config_manager)


def _audible_library_cache_file(config_manager: ConfigManager) -> Path:
    """Return the cache file used to hand off Audible list results."""
    return config_manager.config_dir / ".audible_library_cache.json"


def _create_and_save_plan(
    config: Config,
    folder: Path,
    audiobook_sets: list,
    output_path: Path,
    profile_name: str | None,
) -> None:
    """Create a plan JSON file and print the next-step workflow."""
    plan = PlanBuilder(config).create_plan(
        library_root=folder,
        audiobook_sets=audiobook_sets,
        profile_name=profile_name,
        source_roots=[folder],
    )
    save_plan(plan, output_path)
    click.echo("")
    click.echo(f"Plan saved to {output_path}")
    click.echo(f"Review: bookbot review {output_path}")
    click.echo(f"Apply:  bookbot apply {output_path}")
    if plan.conflicts:
        click.echo("")
        click.echo("Plan has conflicts and must be fixed before apply.", err=True)


def _show_plan(path: Path, include_operations: bool = True) -> None:
    """Print a stored plan in review format."""
    plan = load_plan(path)
    click.echo(format_plan_summary(plan, include_operations=include_operations))


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    default=True,
    help="Show what would be done without making changes",
)
@click.option("--profile", type=str, help="Configuration profile to use")
@click.option("--recurse", type=int, default=5, help="Maximum recursion depth")
@click.option("--no-tag", is_flag=True, help="Skip tagging operations")
@click.option("--template", type=str, help="Naming template to use")
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(path_type=Path),
    help="Write a reviewable rename plan to JSON",
)
@click.option("--lang", type=str, default="en", help="Preferred language")
@click.option("--cache", type=click.Path(path_type=Path), help="Cache directory")
@click.option("--log", type=click.Path(path_type=Path), help="Log file path")
@click.pass_context
def scan(
    ctx: click.Context,
    folder: Path,
    dry_run: bool,
    profile: str | None,
    recurse: int,
    no_tag: bool,
    template: str | None,
    plan_path: Path | None,
    lang: str,
    cache: Path | None,
    log: Path | None,
) -> None:
    """Scan a folder for audiobooks and propose renames."""
    config_manager = ctx.obj["config_manager"]
    config, loaded_profile = _resolve_config(config_manager, profile)

    # Override config with command line options
    if no_tag:
        config.tagging.enabled = False
    if template:
        named_template = config.templates.get(template)
        if named_template is not None:
            config.output.folder_template = named_template.folder_template
            config.output.file_template = named_template.file_template
        else:
            config.output.folder_template = template
    if cache:
        config.cache_directory = cache
    if log:
        config.log_directory = log

    # Initialize scanner
    scanner = AudioFileScanner(recursive=True, max_depth=recurse)

    try:
        click.echo(f"Scanning {folder}...")
        audiobook_sets = scanner.scan_directory(folder)

        if not audiobook_sets:
            click.echo("No audiobooks found in the specified directory.")
            return

        click.echo(f"Found {len(audiobook_sets)} audiobook set(s):")

        for i, audiobook_set in enumerate(audiobook_sets, 1):
            click.echo(f"\n{i}. {audiobook_set.source_path.name}")
            click.echo(f"   Tracks: {audiobook_set.total_tracks}")
            click.echo(f"   Discs: {audiobook_set.disc_count}")

            if audiobook_set.raw_title_guess:
                click.echo(f"   Title: {audiobook_set.raw_title_guess}")
            if audiobook_set.author_guess:
                click.echo(f"   Author: {audiobook_set.author_guess}")
            if audiobook_set.asin_guess:
                click.echo(f"   ASIN: {audiobook_set.asin_guess}")

            if audiobook_set.warnings:
                click.echo("   Warnings:")
                for warning in audiobook_set.warnings:
                    click.echo(f"     - {warning}")

        if plan_path:
            _create_and_save_plan(
                config=config,
                folder=folder,
                audiobook_sets=audiobook_sets,
                output_path=plan_path,
                profile_name=loaded_profile.name if loaded_profile else None,
            )

        if dry_run:
            click.echo("\n" + "─" * 60)
            click.echo("✓ Scan completed. Next steps:")
            click.echo("")
            click.echo("  📋 Safe plan:")
            if plan_path:
                click.echo(f"     bookbot review {plan_path}")
                click.echo(f"     bookbot apply {plan_path}")
            else:
                click.echo(f"     bookbot scan {folder} --plan ./bookbot-plan.json")
                click.echo(f"     bookbot plan create {folder}")
            click.echo("")
            click.echo("  📱 Interactive mode:")
            click.echo(f"     bookbot tui {folder}")
            click.echo("")
            click.echo("  🎵 Convert to M4B:")
            if audiobook_sets:
                example = audiobook_sets[0].source_path
                click.echo(f'     bookbot convert "{example}" -o ./output --dry-run')
            click.echo("")
            click.echo("  🩺 Diagnose library:")
            click.echo(f"     bookbot doctor {folder}")
            click.echo("")
            click.echo("For more help, run: bookbot --help")

    except Exception as e:
        click.echo(f"Error scanning directory: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument(
    "folders", nargs=-1, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--profile", type=str, help="Configuration profile to use")
@click.option(
    "--metadata-from-files",
    is_flag=True,
    help="Use local metadata sidecar files instead of online providers",
)
@click.pass_context
def tui(
    ctx: click.Context,
    folders: tuple[Path, ...],
    profile: str | None,
    metadata_from_files: bool,
) -> None:
    """Launch the interactive TUI for audiobook processing."""
    config_manager = ctx.obj["config_manager"]

    # Apply profile if specified
    if profile:
        if not config_manager.apply_profile(profile):
            click.echo(f"Error: Profile '{profile}' not found", err=True)
            sys.exit(1)

    try:
        # Import TUI app here to avoid issues if textual is not installed
        from .tui.app import BookBotApp

        provider = _build_matching_provider(
            config_manager,
            metadata_from_files=metadata_from_files,
        )
        app = BookBotApp(config_manager, list(folders), provider=provider)
        app.run()

    except ImportError:
        click.echo(
            "Error: Textual is required for TUI mode. "
            "Install with: pip install textual",
            err=True,
        )
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error running TUI: {e}", err=True)
        sys.exit(1)


@cli.command("match")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--limit", type=int, default=5, show_default=True)
@click.pass_context
def match(ctx: click.Context, folder: Path, limit: int) -> None:
    """Inspect merged metadata matches and the reasons behind them."""
    import asyncio

    from .core.exceptions import MetadataError

    config_manager = ctx.obj["config_manager"]
    request_timeout = max(
        1.0,
        float(config_manager.load_config().providers.request_timeout or 15),
    )
    scanner = AudioFileScanner(recursive=True, max_depth=5)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found in the specified directory.")
        return

    provider = _build_matching_provider(config_manager)

    def format_failure(error: BaseException) -> str:
        if isinstance(error, MetadataError):
            provider_errors = error.details.get("provider_errors")
            if isinstance(provider_errors, list) and provider_errors:
                detail_text = "; ".join(
                    (
                        f"{item.get('provider', 'provider')}: "
                        f"{item.get('error', 'unavailable')}"
                    )
                    for item in provider_errors
                    if isinstance(item, dict)
                )
                return (
                    "Provider unavailable - no matches "
                    f"(network error: {detail_text})"
                )

            if error.details.get("kind") == "timeout":
                timeout = error.details.get("timeout", request_timeout)
                return (
                    "Provider unavailable - no matches "
                    f"(network error: request timed out after {timeout:.0f}s)"
                )

        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return (
                "Provider unavailable - no matches "
                f"(network error: request timed out after {request_timeout:.0f}s)"
            )

        return f"Provider unavailable - no matches (network error: {error})"

    async def inspect_matches() -> list[tuple]:
        try:
            results = []
            for audiobook_set in audiobook_sets:
                try:
                    if hasattr(provider, "find_matches_merged"):
                        candidates = await provider.find_matches_merged(
                            audiobook_set,
                            limit=limit,
                        )
                    else:
                        candidates = await asyncio.wait_for(
                            provider.find_matches(
                                audiobook_set,
                                limit=limit,
                            ),
                            timeout=request_timeout,
                        )
                except BaseException as exc:
                    results.append((audiobook_set, [], exc))
                else:
                    results.append((audiobook_set, candidates, None))
            return results
        finally:
            if hasattr(provider, "close_all"):
                await provider.close_all()
            elif hasattr(provider, "close"):
                await provider.close()

    had_failures = False
    for audiobook_set, candidates, error in asyncio.run(inspect_matches()):
        click.echo("")
        click.echo(f"Set: {audiobook_set.source_path}")
        click.echo(
            f"Query: {audiobook_set.raw_title_guess or audiobook_set.source_path.name}"
        )

        if error is not None:
            had_failures = True
            click.echo(f"  {format_failure(error)}")
            continue

        if not candidates:
            click.echo("  No matches found.")
            continue

        for index, candidate in enumerate(candidates, 1):
            authors = ", ".join(candidate.identity.authors) or "Unknown author"
            providers = candidate.identity.raw_data.get(
                "providers",
                [candidate.identity.provider],
            )
            provider_text = ", ".join(str(name) for name in providers)
            reasons = "; ".join(candidate.match_reasons) or "No reasons provided"

            click.echo(
                f"  {index}. {candidate.confidence:.2f} - "
                f"{candidate.identity.title} - {authors}"
            )
            click.echo(f"     Providers: {provider_text}")
            click.echo(f"     Reasons: {reasons}")

    if had_failures:
        sys.exit(1)


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for converted files",
)
@click.option("--profile", type=str, help="Configuration profile to use")
@click.option("--bitrate", type=str, default="128k", help="AAC bitrate (e.g., 128k)")
@click.option("--vbr", type=int, help="VBR quality (1-6, overrides bitrate)")
@click.option("--normalize", is_flag=True, help="Normalize audio levels")
@click.option(
    "--chapters",
    type=click.Choice(["auto", "from-tags", "track"]),
    default="auto",
    help="Chapter creation method",
)
@click.option("--no-art", is_flag=True, help="Skip cover art embedding")
@click.option("--dry-run", is_flag=True, help="Show conversion plan without executing")
@click.pass_context
def convert(
    ctx: click.Context,
    folder: Path,
    output: Path,
    profile: str | None,
    bitrate: str,
    vbr: int | None,
    normalize: bool,
    chapters: str,
    no_art: bool,
    dry_run: bool,
) -> None:
    """Convert audiobooks to M4B format."""

    # Pre-flight check: Ensure FFmpeg is installed
    if not shutil.which("ffmpeg"):
        click.echo("Error:Error: FFmpeg not found in PATH.", err=True)
        click.echo("\nConversion requires FFmpeg. Please install it first:", err=True)
        click.echo("  • Debian/Ubuntu: sudo apt install ffmpeg", err=True)
        click.echo("  • macOS: brew install ffmpeg", err=True)
        click.echo("  • Windows: winget install ffmpeg", err=True)
        sys.exit(1)

    config_manager = ctx.obj["config_manager"]

    # Apply profile if specified
    if profile:
        if not config_manager.apply_profile(profile):
            profiles = config_manager.list_profiles()
            click.echo(f"Error:Error: Profile '{profile}' not found", err=True)
            if profiles:
                click.echo("\nAvailable profiles:", err=True)
                for name, prof in profiles.items():
                    click.echo(f"  • {name}: {prof.description}", err=True)
            else:
                click.echo(
                    "\nNo profiles found. Profiles will be created automatically.",
                    err=True,
                )
            sys.exit(1)

    config = config_manager.load_config()

    # Check if conversion is enabled in config
    if not config.conversion.enabled:
        if dry_run:
            # A dry run never converts anything, so it must not gate on the
            # conversion toggle or prompt interactively (script-unfriendly).
            click.echo(
                "Note: M4B conversion is disabled in your configuration; "
                "showing the plan anyway (dry run).",
                err=True,
            )
        else:
            click.echo(
                "Warning:M4B conversion is currently disabled in your configuration.", err=True
            )
            click.echo("")
            if click.confirm("Would you like to enable it now?", default=True):
                config.conversion.enabled = True
                config_manager.save_config(config)
                click.echo("Done:Conversion enabled and saved to config.")
            else:
                click.echo("\nTo enable conversion manually, edit:", err=True)
                click.echo(f"  {config_manager.config_file}", err=True)
                click.echo("\nSet: [conversion] enabled = true", err=True)
                sys.exit(1)

    try:
        # Import conversion module
        from .convert.pipeline import ConversionPipeline

        pipeline = ConversionPipeline(config_manager)

        # Override config with command line options
        conv_config = config.conversion.model_copy()
        conv_config.output_directory = output
        if vbr:
            conv_config.use_vbr = True
            conv_config.vbr_quality = vbr
        else:
            conv_config.bitrate = bitrate
        conv_config.normalize_audio = normalize
        conv_config.chapter_naming = (
            "track_number" if chapters == "track" else chapters.replace("-", "_")
        )
        conv_config.write_cover_art = not no_art

        click.echo(f"Converting audiobooks from {folder} to {output}...")

        if dry_run:
            # Show conversion plan
            plan = pipeline.create_conversion_plan(folder, conv_config)
            click.echo(f"\nConversion Plan ({len(plan.operations)} operation(s)):")
            click.echo("─" * 60)
            for i, op in enumerate(plan.operations, 1):
                click.echo(f"\n{i}. {op.audiobook_set.source_path.name}")
                click.echo(f"   Source: {op.audiobook_set.source_path}")
                click.echo(f"   Output: {op.output_path}")
                if op.audiobook_set.chosen_identity:
                    identity = op.audiobook_set.chosen_identity
                    click.echo(f"   Title: {identity.title}")
                    if identity.authors:
                        click.echo(f"   Author: {', '.join(identity.authors)}")
            click.echo("\n" + "─" * 60)
            click.echo("Done:Dry run complete. No files were modified.")
            click.echo("\nTo execute, run the same command without --dry-run")
        else:
            # Execute conversion
            success = pipeline.convert_directory(folder, conv_config)
            if success:
                click.echo("Conversion completed successfully!")
            else:
                click.echo("Conversion failed!", err=True)
                sys.exit(1)

    except ImportError as e:
        if "ffmpeg" in str(e).lower():
            click.echo(
                "Error: FFmpeg is required for conversion. Please install FFmpeg.",
                err=True,
            )
        else:
            click.echo(f"Error: Missing dependency for conversion: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during conversion: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("transaction_id", type=str)
@click.pass_context
def undo(ctx: click.Context, transaction_id: str) -> None:
    """Undo a previous operation by transaction ID."""
    config_manager = ctx.obj["config_manager"]
    transaction_manager = TransactionManager(config_manager)

    try:
        if transaction_manager.undo_transaction(transaction_id):
            click.echo(f"Transaction {transaction_id} undone successfully")
        else:
            click.echo(f"Failed to undo transaction {transaction_id}", err=True)
            click.echo(
                "Run 'bookbot history' to list recent transactions and their "
                "IDs, or 'bookbot history --days 30' for older ones.",
                err=True,
            )
            sys.exit(1)
    except Exception as e:
        click.echo(f"Error undoing transaction: {e}", err=True)
        sys.exit(1)


@cli.group()
def config() -> None:
    """Configuration management commands."""
    pass


@config.command("list")
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """List configuration profiles."""
    config_manager = ctx.obj["config_manager"]
    profiles = config_manager.list_profiles()

    if not profiles:
        click.echo("No profiles found")
        return

    click.echo("Available profiles:")
    for name, description in profiles.items():
        click.echo(f"  {name}: {description}")


@config.command("show")
@click.argument("profile_name", type=str, required=False)
@click.pass_context
def config_show(ctx: click.Context, profile_name: str | None) -> None:
    """Show configuration details."""
    config_manager = ctx.obj["config_manager"]

    if profile_name:
        profile = config_manager.load_profile(profile_name)
        if not profile:
            click.echo(f"Profile '{profile_name}' not found", err=True)
            sys.exit(1)
        config_data = profile.config
        click.echo(f"Profile: {profile.name}")
        click.echo(f"Description: {profile.description}")
    else:
        config_data = config_manager.load_config()
        click.echo("Current configuration:")

    # Display key configuration settings
    click.echo(f"Safe mode: {config_data.safe_mode}")
    click.echo(f"Active template: {config_data.active_template}")
    click.echo(f"Folder template: {config_data.output.folder_template}")
    click.echo(f"File template: {config_data.output.file_template}")
    click.echo(f"Tagging enabled: {config_data.tagging.enabled}")
    click.echo(f"Conversion enabled: {config_data.conversion.enabled}")
    click.echo(f"Prefer M4B: {config_data.output.prefer_m4b}")
    click.echo(f"Write metadata.json: {config_data.output.write_metadata_json}")
    click.echo(f"Write metadata.opf: {config_data.output.write_metadata_opf}")
    click.echo(f"Write NFO: {config_data.output.write_nfo}")


@config.command("reset")
@click.confirmation_option(prompt="Reset configuration to defaults?")
@click.pass_context
def config_reset(ctx: click.Context) -> None:
    """Reset configuration to defaults."""
    config_manager = ctx.obj["config_manager"]
    config_manager.reset_to_defaults()
    click.echo("Configuration reset to defaults")


@config.command("set")
@click.argument("key", type=str)
@click.argument("value", type=str)
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set a configuration value.

    Examples:
        bookbot config set conversion.enabled true
        bookbot config set conversion.bitrate 256k
        bookbot config set tagging.enabled false
    """
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    # Parse the key to get section and field
    parts = key.split(".")
    if len(parts) != 2:
        click.echo("Error:Error: Key must be in format 'section.field'", err=True)
        click.echo("\nExamples:", err=True)
        click.echo("  bookbot config set conversion.enabled true", err=True)
        click.echo("  bookbot config set conversion.bitrate 256k", err=True)
        sys.exit(1)

    section, field = parts

    # Get the section object
    if not hasattr(config, section):
        click.echo(f"Error:Error: Unknown section '{section}'", err=True)
        click.echo("\nAvailable sections: conversion, tagging, providers", err=True)
        sys.exit(1)

    section_obj = getattr(config, section)

    # Check if field exists
    if not hasattr(section_obj, field):
        click.echo(
            f"Error:Error: Unknown field '{field}' in section '{section}'", err=True
        )
        sys.exit(1)

    # Convert value to appropriate type
    original_value = getattr(section_obj, field)
    converted_value: object = value
    if isinstance(original_value, bool):
        converted_value = value.lower() in ("true", "yes", "1", "on")
    elif isinstance(original_value, int):
        try:
            converted_value = int(value)
        except ValueError:
            click.echo(f"Error:Error: '{value}' is not a valid integer", err=True)
            sys.exit(1)
    elif isinstance(original_value, float):
        try:
            converted_value = float(value)
        except ValueError:
            click.echo(f"Error:Error: '{value}' is not a valid number", err=True)
            sys.exit(1)

    # Set the value
    setattr(section_obj, field, converted_value)
    config_manager.save_config(config)
    click.echo(f"Done:Set {key} = {value}")


@config.command("get")
@click.argument("key", type=str)
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Get a configuration value.

    Example:
        bookbot config get conversion.enabled
    """
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    # Parse the key
    parts = key.split(".")
    if len(parts) != 2:
        click.echo("Error:Error: Key must be in format 'section.field'", err=True)
        sys.exit(1)

    section, field = parts

    # Get the value
    if not hasattr(config, section):
        click.echo(f"Error:Error: Unknown section '{section}'", err=True)
        sys.exit(1)

    section_obj = getattr(config, section)

    if not hasattr(section_obj, field):
        click.echo(
            f"Error:Error: Unknown field '{field}' in section '{section}'", err=True
        )
        sys.exit(1)

    value = getattr(section_obj, field)
    click.echo(f"{key} = {value}")


@config.command("where")
@click.pass_context
def config_where(ctx: click.Context) -> None:
    """Show the location of the configuration file."""
    config_manager = ctx.obj["config_manager"]
    click.echo(f"Configuration file: {config_manager.config_file}")
    click.echo(f"Profiles directory: {config_manager.profiles_dir}")


@config.command("edit")
@click.pass_context
def config_edit(ctx: click.Context) -> None:
    """Open the configuration file in your default editor."""
    import os
    import subprocess

    config_manager = ctx.obj["config_manager"]
    config_file = config_manager.config_file

    # Get editor from environment or use default
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_file)], check=True)
        click.echo("Done:Configuration file closed")
    except subprocess.CalledProcessError:
        click.echo(f"Error:Error opening editor. Edit manually: {config_file}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"Error:Editor '{editor}' not found.", err=True)
        click.echo(f"\nEdit manually: {config_file}", err=True)
        sys.exit(1)


@cli.group()
def profile() -> None:
    """Built-in collector profiles."""
    pass


@profile.command("list")
@click.pass_context
def profile_list(ctx: click.Context) -> None:
    """List built-in and saved profiles."""
    config_manager = ctx.obj["config_manager"]
    profiles = config_manager.list_profiles()

    if not profiles:
        click.echo("No profiles found")
        return

    click.echo("Available profiles:")
    for name, description in profiles.items():
        click.echo(f"  {name}: {description}")


@profile.command("show")
@click.argument("profile_name", type=str)
@click.pass_context
def profile_show(ctx: click.Context, profile_name: str) -> None:
    """Show a profile's layout and output preferences."""
    config_manager = ctx.obj["config_manager"]
    loaded_profile = _require_profile(config_manager, profile_name)
    config = loaded_profile.config

    click.echo(f"Profile: {loaded_profile.name}")
    click.echo(f"Description: {loaded_profile.description}")
    click.echo(f"Folder template: {config.output.folder_template}")
    click.echo(f"File template: {config.output.file_template}")
    click.echo(f"Prefer M4B: {config.output.prefer_m4b}")
    click.echo(f"Write cover: {config.output.write_cover}")
    click.echo(f"Write metadata.json: {config.output.write_metadata_json}")
    click.echo(f"Write metadata.opf: {config.output.write_metadata_opf}")
    click.echo(f"Write NFO: {config.output.write_nfo}")
    click.echo(f"Chapter style: {config.output.chapter_style}")


@profile.command("use")
@click.argument("profile_name", type=str)
@click.pass_context
def profile_use(ctx: click.Context, profile_name: str) -> None:
    """Apply a profile as the saved default."""
    config_manager = ctx.obj["config_manager"]
    if not config_manager.apply_profile(profile_name):
        click.echo(f"Error: Profile '{profile_name}' not found", err=True)
        sys.exit(1)
    click.echo(f"Profile '{profile_name}' applied")


@cli.group()
def provider() -> None:
    """Metadata provider management commands."""
    pass


@provider.command("list")
@click.pass_context
def provider_list(ctx: click.Context) -> None:
    """List available metadata providers."""
    try:
        from .providers.manager import ProviderManager

        config_manager = ctx.obj["config_manager"]
        provider_manager = ProviderManager(config_manager)

        providers_info = provider_manager.list_providers()

        click.echo("Available metadata providers:")
        click.echo("")

        for provider_id, info in providers_info.items():
            status_icon = "[OK]" if info["status"] == "enabled" else "[X]"
            click.echo(f"{status_icon} {info['name']} ({provider_id})")
            click.echo(f"   Description: {info['description']}")
            click.echo(f"   Requires API Key: {info['requires_api_key']}")

            if provider_id == "googlebooks" and info.get("api_key_provided") is False:
                click.echo("   Warning:API key not configured - provider disabled")
            elif provider_id == "audible" and "marketplace" in info:
                click.echo(f"   Marketplace: {info['marketplace']}")

            click.echo("")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)


@provider.command("enable")
@click.argument("provider_name", type=str)
@click.pass_context
def provider_enable(ctx: click.Context, provider_name: str) -> None:
    """Enable a metadata provider."""
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    provider_name = provider_name.lower()

    if provider_name == "googlebooks":
        if not config.providers.google_books.api_key:
            click.echo("Error: Google Books requires an API key. Set it first with:")
            click.echo("  bookbot provider set-key googlebooks YOUR_API_KEY")
            sys.exit(1)
        config.providers.google_books.enabled = True
    elif provider_name == "librivox":
        config.providers.librivox.enabled = True
    elif provider_name == "audible":
        config.providers.audible.enabled = True
    elif provider_name == "audnexus":
        config.providers.audnexus.enabled = True
    elif provider_name == "hardcover":
        if not config.providers.hardcover.api_key:
            click.echo("Error: Hardcover requires an API key. Set it first with:")
            click.echo("  bookbot provider set-key hardcover YOUR_TOKEN")
            sys.exit(1)
        config.providers.hardcover.enabled = True
    elif provider_name == "openlibrary":
        click.echo("OpenLibrary is always enabled as the default provider")
        return
    else:
        click.echo(f"Error: Unknown provider '{provider_name}'")
        click.echo(
            "Available providers: googlebooks, librivox, audible, audnexus, hardcover"
        )
        sys.exit(1)

    config_manager.save_config(config)
    click.echo(f"Provider '{provider_name}' enabled")


@provider.command("disable")
@click.argument("provider_name", type=str)
@click.pass_context
def provider_disable(ctx: click.Context, provider_name: str) -> None:
    """Disable a metadata provider."""
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    provider_name = provider_name.lower()

    if provider_name == "openlibrary":
        click.echo("Error: OpenLibrary cannot be disabled (it's the default provider)")
        sys.exit(1)
    elif provider_name == "googlebooks":
        config.providers.google_books.enabled = False
    elif provider_name == "librivox":
        config.providers.librivox.enabled = False
    elif provider_name == "audible":
        config.providers.audible.enabled = False
    elif provider_name == "audnexus":
        config.providers.audnexus.enabled = False
    elif provider_name == "hardcover":
        config.providers.hardcover.enabled = False
    else:
        click.echo(f"Error: Unknown provider '{provider_name}'")
        sys.exit(1)

    config_manager.save_config(config)
    click.echo(f"Provider '{provider_name}' disabled")


@provider.command("set-key")
@click.argument("provider_name", type=str)
@click.argument("api_key", type=str)
@click.pass_context
def provider_set_key(ctx: click.Context, provider_name: str, api_key: str) -> None:
    """Set API key for a provider."""
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    provider_name = provider_name.lower()

    if provider_name == "googlebooks":
        config.providers.google_books.api_key = api_key
        config.providers.google_books.enabled = True
        config_manager.save_config(config)
        click.echo("Google Books API key set and provider enabled")
    elif provider_name == "hardcover":
        config.providers.hardcover.api_key = api_key
        config.providers.hardcover.enabled = True
        config_manager.save_config(config)
        click.echo("Hardcover API key set and provider enabled")
    else:
        click.echo(f"Error: Provider '{provider_name}' does not use an API key")
        click.echo("Providers with API keys: googlebooks, hardcover")
        sys.exit(1)


@provider.command("set-marketplace")
@click.argument(
    "marketplace",
    type=click.Choice(["US", "UK", "CA", "AU", "FR", "DE", "IT", "ES", "JP", "IN"]),
)
@click.pass_context
def provider_set_marketplace(ctx: click.Context, marketplace: str) -> None:
    """Set Audible marketplace."""
    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    config.providers.audible.marketplace = marketplace.upper()
    config_manager.save_config(config)
    click.echo(f"Audible marketplace set to {marketplace}")


@cli.command("review")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def review(plan_file: Path) -> None:
    """Review a saved rename plan."""
    _show_plan(plan_file, include_operations=True)


@cli.command("apply")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--yes", is_flag=True, help="Apply without confirmation")
@click.pass_context
def apply_plan_file(ctx: click.Context, plan_file: Path, yes: bool) -> None:
    """Apply a saved rename plan."""
    config_manager = ctx.obj["config_manager"]
    plan = load_plan(plan_file)

    if plan.conflicts:
        click.echo("Plan has conflicts and cannot be applied:", err=True)
        for conflict in plan.conflicts:
            click.echo(f"  - {conflict}", err=True)
        sys.exit(1)

    if not yes and not click.confirm(
        f"Apply {len(plan.operations)} operation(s) from {plan_file}?", default=False
    ):
        click.echo("Cancelled")
        return

    transaction_manager = TransactionManager(config_manager)
    try:
        transaction_manager.execute_plan(plan, dry_run=False)
    except Exception as exc:
        click.echo(f"Error applying plan: {exc}", err=True)
        sys.exit(1)

    save_plan(plan, plan_file)
    transaction_id = plan.applied_transaction_id or "unknown"
    click.echo(f"Plan applied successfully. Transaction ID: {transaction_id}")


@cli.group()
def plan() -> None:
    """Create, inspect, validate, and apply rename plans."""
    pass


@plan.command("create")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=Path("bookbot-plan.json"),
    show_default=True,
    help="Output JSON file for the generated plan",
)
@click.option("--profile", type=str, help="Profile to use for plan generation")
@click.option("--recurse", type=int, default=5, help="Maximum recursion depth")
@click.pass_context
def plan_create(
    ctx: click.Context, folder: Path, output: Path, profile: str | None, recurse: int
) -> None:
    """Create a reviewable plan JSON from a library directory."""
    config_manager = ctx.obj["config_manager"]
    config, loaded_profile = _resolve_config(config_manager, profile)
    scanner = AudioFileScanner(recursive=True, max_depth=recurse)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found in the specified directory.")
        return

    _create_and_save_plan(
        config=config,
        folder=folder,
        audiobook_sets=audiobook_sets,
        output_path=output,
        profile_name=loaded_profile.name if loaded_profile else None,
    )


@plan.command("show")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def plan_show(plan_file: Path) -> None:
    """Show the saved plan with operations and warnings."""
    _show_plan(plan_file, include_operations=True)


@plan.command("apply")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--yes", is_flag=True, help="Apply without confirmation")
@click.pass_context
def plan_apply(ctx: click.Context, plan_file: Path, yes: bool) -> None:
    """Apply a plan file."""
    ctx.invoke(apply_plan_file, plan_file=plan_file, yes=yes)


@plan.command("diff")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def plan_diff(plan_file: Path) -> None:
    """Show a compact old->new diff for a plan."""
    click.echo(format_plan_diff(load_plan(plan_file)))


@plan.command("validate")
@click.argument(
    "plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
def plan_validate(plan_file: Path) -> None:
    """Validate a plan file for conflicts and filesystem safety."""
    loaded_plan = load_plan(plan_file)
    if loaded_plan.conflicts:
        click.echo(f"Validation failed for {plan_file}:", err=True)
        for conflict in loaded_plan.conflicts:
            click.echo(f"  - {conflict}", err=True)
        sys.exit(1)

    click.echo(f"Plan {plan_file} is valid")
    if loaded_plan.warnings:
        for warning in loaded_plan.warnings:
            click.echo(f"  - {warning}")


@cli.command()
@click.argument(
    "library_path",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--profile", type=str, help="Profile to evaluate against")
@click.pass_context
def doctor(ctx: click.Context, library_path: Path | None, profile: str | None) -> None:
    """Inspect the environment and an optional library path."""
    from .core.logging import get_logger

    config_manager = ctx.obj["config_manager"]
    config, loaded_profile = _resolve_config(config_manager, profile)
    logger = get_logger("doctor", config_manager.get_log_dir())
    logger.info(
        "Running doctor",
        library_path=str(library_path) if library_path else None,
        profile=loaded_profile.name if loaded_profile else None,
    )
    report = LibraryDoctor(config, config_manager.config_dir).run(
        library_path=library_path,
        profile_name=loaded_profile.name if loaded_profile else None,
    )

    for check in report.checks:
        click.echo(f"{check.icon} {check.message}")

    if report.has_failures:
        sys.exit(1)


@cli.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish", "all"]))
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(path_type=Path),
    help="Output directory for completion files",
)
@click.pass_context
def completions(ctx: click.Context, shell: str, output_dir: Path | None) -> None:
    """Generate shell completion scripts."""
    try:
        import subprocess
        from pathlib import Path as PathlibPath

        # Get the script path
        script_path = PathlibPath(__file__).parent.parent / "scripts" / "completions.py"
        if not script_path.exists():
            script_path = PathlibPath("/opt/bookbot/scripts/completions.py")

        if not script_path.exists():
            click.echo("Error: Completion generator script not found", err=True)
            sys.exit(1)

        # Build command
        cmd = [sys.executable, str(script_path), shell]
        if output_dir:
            cmd.extend(["--output-dir", str(output_dir)])

        # Run the completion generator
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            click.echo(result.stdout)
        else:
            click.echo(f"Error generating completions: {result.stderr}", err=True)
            sys.exit(1)

    except Exception as e:
        click.echo(f"Error generating completions: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--days", type=int, default=30, help="Show transactions from last N days")
@click.pass_context
def history(ctx: click.Context, days: int) -> None:
    """Show operation history."""
    config_manager = ctx.obj["config_manager"]
    transaction_manager = TransactionManager(config_manager)

    transactions = transaction_manager.list_transactions(days)

    if not transactions:
        click.echo("No transactions found")
        return

    click.echo(f"Transactions from last {days} days:")
    for transaction in transactions:
        status = transaction.get("status", "completed")
        undo_info = "" if transaction["can_undo"] else " (cannot undo)"
        transaction_type = transaction.get("transaction_type", "rename")
        click.echo(
            f"  {transaction['id'][:8]}... - "
            f"{transaction['timestamp']} - "
            f"{transaction_type} - "
            f"{transaction['operation_count']} operations - "
            f"{status}{undo_info}"
        )


@cli.group()
def audible() -> None:
    """Audible-specific commands for import and DRM removal."""
    pass


@audible.command("auth")
@click.option(
    "--country",
    type=str,
    default="US",
    help="Country code (US, UK, CA, AU, etc.)",
)
@click.pass_context
def audible_auth(ctx: click.Context, country: str) -> None:
    """Authenticate with Audible for importing books."""
    try:
        from .drm.audible_client import AudibleAuthClient

        client = AudibleAuthClient(country_code=country)

        if client.authenticate():
            click.echo(
                "[OK]Authentication successful! You can now import Audible books."
            )
        else:
            click.echo(
                "Error:Authentication failed. Please try again.",
                err=True,
            )
            sys.exit(1)

    except ImportError as e:
        click.echo(f"Error:Missing dependency: {e}", err=True)
        click.echo("Install the audible package with: pip install audible", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error:Authentication failed: {e}", err=True)
        sys.exit(1)


@audible.command("import")
@click.argument("numbers", type=str)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path),
    help="Output directory for downloaded books",
)
@click.option("--remove-drm", is_flag=True, help="Remove DRM after download")
@click.option("--activation-bytes", type=str, help="Activation bytes for DRM removal")
@click.pass_context
def audible_import(
    ctx: click.Context,
    numbers: str,
    output_dir: Path | None,
    remove_drm: bool,
    activation_bytes: str | None,
) -> None:
    """Import Audible books by number from 'audible list' (e.g., '1,2,3' or '1')."""
    import json

    try:
        from .drm.audible_client import AudibleAuthClient
        from .drm.remover import DRMRemover

        # Load cached library
        cache_file = _audible_library_cache_file(ctx.obj["config_manager"])
        if not cache_file.exists():
            click.echo(
                "Error:No library cache found. Run 'bookbot audible list' first.", err=True
            )
            sys.exit(1)

        library = json.loads(cache_file.read_text())

        # Parse numbers (comma or space separated)
        numbers = numbers.replace(" ", ",")
        try:
            book_indices = [int(n.strip()) - 1 for n in numbers.split(",") if n.strip()]
        except ValueError:
            click.echo("Error:Invalid book numbers. Use format: 1,2,3", err=True)
            sys.exit(1)

        # Validate indices
        invalid = [i + 1 for i in book_indices if i < 0 or i >= len(library)]
        if invalid:
            click.echo(
                f"Error:Invalid book numbers: {invalid}. "
                f"Library has {len(library)} books.",
                err=True,
            )
            sys.exit(1)

        if not output_dir:
            output_dir = Path.cwd() / "audible_books"

        output_dir.mkdir(parents=True, exist_ok=True)

        client = AudibleAuthClient()

        # Check if authenticated
        if not client._load_stored_auth():
            click.echo(
                "Error:Not authenticated. Run 'bookbot audible auth' first.", err=True
            )
            sys.exit(1)

        # Import each book
        click.echo(f"Importing {len(book_indices)} book(s)...\n")

        for idx in book_indices:
            book = library[idx]
            asin = book.get("asin", "")
            title = book.get("title", "Unknown")

            click.echo(
                f"[{book_indices.index(idx) + 1}/{len(book_indices)}] {title} [{asin}]"
            )

            # Download the book
            book_path = output_dir / f"{asin}.aax"
            success = client.download_book(asin, str(book_path))

            if success:
                click.echo(f"  [OK]Downloaded to: {book_path}")

                if remove_drm:
                    click.echo("  [DRM]Removing DRM...")

                    # Try to get activation bytes from client if not provided
                    if not activation_bytes:
                        activation_bytes = client.get_activation_bytes()

                    if not activation_bytes:
                        click.echo(
                            "  Warning:No activation bytes available. Skipping DRM removal."
                        )
                    else:
                        remover = DRMRemover(activation_bytes=activation_bytes)
                        result = remover.remove_drm(book_path)

                        if result.success:
                            click.echo(f"  [OK]DRM removed: {result.output_file}")
                        else:
                            click.echo(
                                f"  Error:DRM removal failed: {result.error_message}",
                                err=True,
                            )
            else:
                click.echo("  Error:Download failed")

            click.echo("")

        click.echo("Import complete!")

    except Exception as e:
        click.echo(f"Import failed: {e}", err=True)
        sys.exit(1)


@audible.command("get-activation-bytes")
@click.option("--username", prompt=True)
@click.option("--password", prompt=True, hide_input=True)
@click.option(
    "--lang",
    default="us",
    help="us (default) / au / in / de / fr / jp / uk (untested)",
)
def get_activation_bytes(username: str, password: str, lang: str) -> None:
    """Get activation bytes from Audible using your username and password."""
    try:
        import base64
        import binascii
        import hashlib
        from urllib.parse import parse_qsl, urlencode, urlparse

        import requests  # type: ignore[import-untyped]
        from selenium import webdriver  # type: ignore[import-not-found]

        def extract_activation_bytes(data: bytes):  # type: ignore[no-untyped-def]
            if (b"BAD_LOGIN" in data or b"Whoops" in data) or b"group_id" not in data:
                raise Exception("Activation failed! Please check your credentials.")
            k = data.rfind(b"group_id")
            end_paren = data[k:].find(b")")
            keys = data[k + end_paren + 1 + 1 :]
            output_keys = []
            for i in range(0, 8):
                key = keys[i * 70 + i : (i + 1) * 70 + i]
                h = binascii.hexlify(bytes(key))
                h = b",".join(h[i : i + 2] for i in range(0, len(h), 2))
                output_keys.append(h)

            activation_bytes = output_keys[0].replace(b",", b"")[0:8]
            activation_bytes = b"".join(
                reversed(
                    [
                        activation_bytes[i : i + 2]
                        for i in range(0, len(activation_bytes), 2)
                    ]
                )
            )
            return activation_bytes.decode("ascii")

        base_url = "https://www.audible.com/"
        base_url_license = "https://www.audible.com/"

        if lang == "uk":
            base_url = base_url.replace(".com", ".co.uk")
        elif lang == "jp":
            base_url = base_url.replace(".com", ".co.jp")
        elif lang == "au":
            base_url = base_url.replace(".com", ".com.au")
        elif lang == "in":
            base_url = base_url.replace(".com", ".in")
        elif lang != "us":
            base_url = base_url.replace(".com", "." + lang)

        player_id = (
            base64.encodebytes(hashlib.sha1(b"").digest()).rstrip().decode("ascii")
        )

        opts = webdriver.ChromeOptions()
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 6.1; "
            "WOW64; Trident/7.0; AS; rv:11.0) like Gecko"
        )
        opts.add_argument("--headless")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        if sys.platform == "win32":
            chromedriver_path = "chromedriver.exe"
        elif Path("/usr/bin/chromedriver").is_file():
            chromedriver_path = "/usr/bin/chromedriver"
        elif Path("/usr/lib/chromium-browser/chromedriver").is_file():
            chromedriver_path = "/usr/lib/chromium-browser/chromedriver"
        elif Path("/usr/local/bin/chromedriver").is_file():
            chromedriver_path = "/usr/local/bin/chromedriver"
        else:
            chromedriver_path = "./chromedriver"

        with webdriver.Chrome(
            options=opts, executable_path=chromedriver_path
        ) as driver:
            payload = {
                "openid.ns": "http://specs.openid.net/auth/2.0",
                "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
                "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
                "openid.mode": "logout",
                "openid.assoc_handle": "amzn_audible_" + lang,
                "openid.return_to": (
                    base_url
                    + "player-auth-token?playerType=software"
                    + f"&playerId={player_id}="
                    + "&bp_ua=y&playerModel=Desktop"
                    + "&playerManufacturer=Audible"
                ),
            }
            if "@" in username:
                login_url = "https://www.amazon.com/ap/signin?"
            else:
                login_url = (
                    "https://www.audible.com/sign-in/"
                    "ref=ap_to_private?"
                    "forcePrivateSignIn=true"
                    "&rdPath=https%3A%2F%2Fwww.audible.com"
                    "%2F%3F"
                )

            query_string = urlencode(payload)
            url = login_url + query_string
            driver.get(base_url + "?ipRedirectOverride=true")
            driver.get(url)

            search_box = driver.find_element_by_id("ap_email")
            search_box.send_keys(username)
            search_box = driver.find_element_by_id("ap_password")
            search_box.send_keys(password)
            search_box.submit()
            import time

            time.sleep(2)

            click.echo(
                "ATTENTION: Now you may have to enter a "
                "one-time password manually. Once you are "
                "done, press enter to continue..."
            )
            input()

            driver.get(
                base_url
                + "player-auth-token?playerType=software"
                + "&bp_ua=y&playerModel=Desktop"
                + f"&playerId={player_id}"
                + "&playerManufacturer=Audible&serial="
            )
            current_url = driver.current_url
            o = urlparse(current_url)
            data = dict(parse_qsl(o.query))

            headers = {"User-Agent": "Audible Download Manager"}
            cookies = driver.get_cookies()
            s = requests.Session()
            for cookie in cookies:
                s.cookies.set(cookie["name"], cookie["value"])

            durl = (
                base_url_license
                + "license/licenseForCustomerToken?"
                + "customer_token="
                + data["playerToken"]
                + "&action=de-register"
            )
            s.get(durl, headers=headers)

            url = (
                base_url_license
                + "license/licenseForCustomerToken?"
                + "customer_token="
                + data["playerToken"]
            )
            response = s.get(url, headers=headers)

            activation_bytes = extract_activation_bytes(response.content)
            click.echo(f"Activation bytes: {activation_bytes}")

            from .drm.secure_storage import save_activation_bytes

            save_activation_bytes(activation_bytes)
            click.echo("Activation bytes saved securely.")

            s.get(durl, headers=headers)

    except Exception as e:
        click.echo(f"An error occurred: {e}", err=True)
        sys.exit(1)


@audible.command("list")
@click.option("--limit", type=int, help="Maximum number of books to show")
@click.pass_context
def audible_list(ctx: click.Context, limit: int | None) -> None:
    """List user's Audible library with numbers for easy importing."""
    try:
        from .drm.audible_client import AudibleAuthClient

        client = AudibleAuthClient()

        # Check if authenticated
        if not client._load_stored_auth():
            click.echo(
                "Error:Not authenticated. Run 'bookbot audible auth' first.",
                err=True,
            )
            sys.exit(1)

        library = client.get_library()

        if not library:
            click.echo("No books found in your library")
            return

        # Cache library for import command
        cache_file = _audible_library_cache_file(ctx.obj["config_manager"])
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        import json

        cache_file.write_text(json.dumps(library, indent=2))

        display_count = len(library) if limit is None else min(limit, len(library))

        click.echo(f"Found {len(library)} books in your library\n")

        for i, book in enumerate(library[:display_count], 1):
            title = book.get("title", "Unknown")
            authors = book.get("authors", [])
            author_names = [author.get("name", "") for author in authors]
            author_str = ", ".join(author_names) if author_names else "Unknown"
            asin = book.get("asin", "")

            click.echo(f"{i:3}. {title} - {author_str} [{asin}]")

        if limit and len(library) > limit:
            click.echo(
                f"\n... and {len(library) - limit} more (use --limit to show all)"
            )

        click.echo("\nTip:To import books, use: bookbot audible import 1,2,3")

    except Exception as e:
        click.echo(f"Failed to list library: {e}", err=True)
        sys.exit(1)


@cli.group()
def drm() -> None:
    """DRM detection and removal commands."""
    pass


@drm.command("detect")
@click.argument("files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--recursive", "-r", is_flag=True, help="Scan directories recursively")
@click.pass_context
def drm_detect(ctx: click.Context, files: tuple[Path, ...], recursive: bool) -> None:
    """Detect DRM protection on audio files."""
    if not files:
        click.echo("Error: At least one file or directory must be specified", err=True)
        sys.exit(1)

    try:
        from .drm.detector import DRMDetector

        detector = DRMDetector()

        # Collect all files to scan
        files_to_scan = []
        for path in files:
            if path.is_file():
                files_to_scan.append(path)
            elif path.is_dir() and recursive:
                # Scan directory for audio files
                audio_extensions = {
                    ".mp3",
                    ".m4a",
                    ".m4b",
                    ".aax",
                    ".aaxc",
                    ".flac",
                    ".ogg",
                    ".opus",
                    ".aac",
                    ".wav",
                }
                for file_path in path.rglob("*"):
                    if file_path.suffix.lower() in audio_extensions:
                        files_to_scan.append(file_path)

        if not files_to_scan:
            click.echo("No audio files found to scan")
            return

        protected_count = 0
        total_count = len(files_to_scan)

        click.echo(f"Scanning {total_count} file(s) for DRM protection...")

        for file_path in files_to_scan:
            drm_info = detector.detect_drm(file_path)

            status_icon = "[LOCKED]" if drm_info.is_protected else "[OK]"
            click.echo(f"{status_icon} {file_path.name}: {drm_info.drm_type.value}")

            if drm_info.is_protected:
                protected_count += 1
                if drm_info.metadata:
                    for key, value in drm_info.metadata.items():
                        click.echo(f"    {key}: {value}")

        click.echo(
            f"\nSummary: {protected_count}/{total_count} files have DRM protection"
        )

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during DRM detection: {e}", err=True)
        sys.exit(1)


@drm.command("set-activation-bytes")
@click.argument("activation_bytes", type=str)
@click.pass_context
def drm_set_activation_bytes(ctx: click.Context, activation_bytes: str) -> None:
    """Store activation bytes for AAX DRM removal."""
    try:
        from .drm.secure_storage import save_activation_bytes

        save_activation_bytes(activation_bytes)
        click.echo("Activation bytes saved securely")

    except Exception as e:
        click.echo(f"Failed to save activation bytes: {e}", err=True)
        sys.exit(1)


@drm.command("remove")
@click.argument("files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path),
    help="Output directory for DRM-free files",
)
@click.option(
    "--activation-bytes", type=str, help="Audible activation bytes for AAX files"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without removing DRM"
)
@click.option("--recursive", "-r", is_flag=True, help="Process directories recursively")
@click.pass_context
def drm_remove(
    ctx: click.Context,
    files: tuple[Path, ...],
    output_dir: Path | None,
    activation_bytes: str | None,
    dry_run: bool,
    recursive: bool,
) -> None:
    """Remove DRM protection from audio files."""
    if not files:
        click.echo("Error: At least one file or directory must be specified", err=True)
        sys.exit(1)

    if not activation_bytes:
        try:
            from .drm.audible_client import AudibleAuthClient

            client = AudibleAuthClient()
            if client.is_authenticated():
                click.echo("Attempting to automatically fetch activation bytes...")
                activation_bytes = client.get_activation_bytes()
                if activation_bytes:
                    click.echo("[OK]Activation bytes fetched successfully!")
                else:
                    click.echo("Warning:Could not automatically fetch activation bytes.")
        except ImportError:
            pass

    try:
        from .drm.detector import DRMDetector
        from .drm.remover import DRMRemover

        detector = DRMDetector()
        remover = DRMRemover(activation_bytes=activation_bytes)

        # Check ffmpeg availability
        if not remover.check_ffmpeg_availability():
            click.echo(
                "Warning: FFmpeg with activation_bytes support not found. "
                "AAX DRM removal will not work.",
                err=True,
            )

        # Collect all files to process
        files_to_process = []
        for path in files:
            if path.is_file():
                files_to_process.append(path)
            elif path.is_dir() and recursive:
                audio_extensions = {
                    ".mp3",
                    ".m4a",
                    ".m4b",
                    ".aax",
                    ".aaxc",
                    ".flac",
                    ".ogg",
                    ".opus",
                    ".aac",
                    ".wav",
                }
                for file_path in path.rglob("*"):
                    if file_path.suffix.lower() in audio_extensions:
                        files_to_process.append(file_path)

        if not files_to_process:
            click.echo("No audio files found to process")
            return

        # Create output directory if specified
        if output_dir and not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)

        success_count = 0
        error_count = 0

        click.echo(f"Processing {len(files_to_process)} file(s)...")

        for file_path in files_to_process:
            # First detect DRM
            drm_info = detector.detect_drm(file_path)

            if not drm_info.is_protected:
                click.echo(f"[OK]{file_path.name}: No DRM protection")
                success_count += 1
                continue

            if dry_run:
                click.echo(
                    f"[LOCKED] {file_path.name}: Would remove {drm_info.drm_type.value} DRM"
                )
                continue

            # Remove DRM
            output_path = None
            if output_dir:
                output_path = output_dir / f"{file_path.stem}_no_drm.m4a"

            result = remover.remove_drm(file_path, output_path, activation_bytes)

            if result.success:
                click.echo(f"[OK]{file_path.name}: DRM removed successfully")
                if result.output_file:
                    click.echo(f"    Output: {result.output_file}")
                success_count += 1
            else:
                click.echo(f"Error:{file_path.name}: Failed - {result.error_message}")
                error_count += 1

        if dry_run:
            click.echo(
                f"\nDry run completed. Found {len(files_to_process)} files to process."
            )
        else:
            click.echo(f"\nCompleted: {success_count} successful, {error_count} failed")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during DRM removal: {e}", err=True)
        sys.exit(1)


# --- Health Check Commands (Feature 1D) ---


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--json-output", "json_out", is_flag=True, help="Output machine-readable JSON")
@click.option("--verbose", is_flag=True, help="Show file paths for each issue")
@click.pass_context
def health(
    ctx: click.Context, folder: Path, json_out: bool, verbose: bool
) -> None:
    """Audit an audiobook library for common issues."""
    from .core.discovery import AudioFileScanner
    from .core.health import LibraryHealthChecker

    scanner = AudioFileScanner(recursive=True)
    click.echo(f"Scanning {folder}...")
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found.")
        return

    checker = LibraryHealthChecker()
    report = checker.run_all_checks(folder, audiobook_sets)

    if json_out:
        import json

        click.echo(json.dumps(report.model_dump(), indent=2, default=str))
        return

    summary = report.to_summary()
    click.echo(f"\nHealth Report: {summary['total']} issue(s) found")
    click.echo("-" * 50)

    if report.missing_covers:
        click.echo(f"\nMissing Covers ({len(report.missing_covers)}):")
        for item in report.missing_covers:
            click.echo(f"  - {item['title']}")
            if verbose:
                click.echo(f"    Path: {item['path']}")

    if report.inconsistent_tags:
        click.echo(f"\nInconsistent Tags ({len(report.inconsistent_tags)}):")
        for item in report.inconsistent_tags:
            click.echo(f"  - {item['title']}: {', '.join(item['mismatches'])}")

    if report.orphaned_files:
        click.echo(f"\nOrphaned Files ({len(report.orphaned_files)}):")
        for filepath in report.orphaned_files[:20]:
            click.echo(f"  - {filepath}")
        if len(report.orphaned_files) > 20:
            click.echo(f"  ... and {len(report.orphaned_files) - 20} more")

    if report.duplicate_editions:
        click.echo(f"\nPossible Duplicates ({len(report.duplicate_editions)} groups):")
        for group in report.duplicate_editions:
            titles = [item["title"] for item in group]
            click.echo(f"  - {' / '.join(titles)}")

    if report.series_gaps:
        click.echo(f"\nSeries Gaps ({len(report.series_gaps)}):")
        for item in report.series_gaps:
            click.echo(
                f"  - {item['series']}: missing volumes {item['missing_volumes']}"
            )

    if report.format_inconsistencies:
        click.echo(f"\nMixed Formats ({len(report.format_inconsistencies)}):")
        for item in report.format_inconsistencies:
            click.echo(f"  - {item['title']}: {item['formats']}")

    if report.bitrate_anomalies:
        click.echo(f"\nBitrate Anomalies ({len(report.bitrate_anomalies)}):")
        for item in report.bitrate_anomalies:
            click.echo(
                f"  - {item['title']} (avg {item['average_bitrate']}kbps): "
                f"{len(item['anomalies'])} outlier(s)"
            )


# --- Organize Commands (Feature 1E) ---


@cli.command()
@click.argument("source", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--target",
    type=click.Path(path_type=Path),
    help="Target directory for reorganized files",
)
@click.option(
    "--template",
    type=click.Choice(["default", "abs", "plex"]),
    default="default",
    help="Naming template preset",
)
@click.option("--dry-run", is_flag=True, default=True, help="Show proposed moves only")
@click.option("--confirm", is_flag=True, help="Execute the reorganization")
@click.pass_context
def organize(
    ctx: click.Context,
    source: Path,
    target: Path | None,
    template: str,
    dry_run: bool,
    confirm: bool,
) -> None:
    """Reorganize an audiobook library into a clean directory structure."""
    from .core.discovery import AudioFileScanner
    from .core.organizer import SmartOrganizer

    scanner = AudioFileScanner(recursive=True)
    click.echo(f"Scanning {source}...")
    audiobook_sets = scanner.scan_directory(source)

    if not audiobook_sets:
        click.echo("No audiobooks found.")
        return

    organizer = SmartOrganizer()
    plan = organizer.propose_reorganization(source, target, template, audiobook_sets)

    click.echo(f"\nReorganization Plan ({plan.total_moves} file moves):")
    click.echo("-" * 50)

    if plan.conflicts:
        click.echo("\nConflicts (must be resolved before executing):")
        for conflict in plan.conflicts:
            click.echo(f"  [!] {conflict}")

    if plan.warnings:
        click.echo("\nWarnings:")
        for warning in plan.warnings:
            click.echo(f"  [?] {warning}")

    # Show sample of moves
    shown = 0
    for op in plan.operations[:20]:
        click.echo(f"  {op.source} -> {op.destination}")
        shown += 1
    if plan.total_moves > 20:
        click.echo(f"  ... and {plan.total_moves - 20} more moves")

    if confirm and plan.is_valid:
        click.echo("\nExecuting reorganization...")
        success = organizer.execute_plan(plan, dry_run=False)
        if success:
            click.echo("Reorganization completed successfully.")
        else:
            click.echo("Reorganization failed. Changes have been rolled back.", err=True)
            sys.exit(1)
    elif not plan.is_valid:
        click.echo("\nPlan has conflicts. Resolve them before using --confirm.")
    else:
        click.echo("\nDry run complete. Use --confirm to execute.")


# --- Chapter Commands (Feature 2A) ---


@cli.group()
def chapters() -> None:
    """Chapter detection and management commands."""
    pass


@chapters.command("detect")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--method",
    type=click.Choice(["auto", "silence", "tracks", "audnexus"]),
    default="auto",
    help="Detection method",
)
@click.option("--noise-db", type=float, default=-50.0, help="Noise threshold in dB")
@click.option("--min-silence", type=float, default=2.0, help="Minimum silence duration")
@click.pass_context
def chapters_detect(
    ctx: click.Context,
    folder: Path,
    method: str,
    noise_db: float,
    min_silence: float,
) -> None:
    """Detect chapters in audiobook files."""
    import asyncio

    from .chapters.detector import ChapterDetector
    from .core.discovery import AudioFileScanner

    scanner = AudioFileScanner(recursive=True)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found.")
        return

    detector = ChapterDetector()

    for ab_set in audiobook_sets:
        click.echo(f"\n{ab_set.raw_title_guess or ab_set.source_path.name}:")

        if method == "silence":
            audio_files = sorted(
                [t.src_path for t in ab_set.tracks], key=lambda p: p.name
            )
            detected = detector.detect_from_silence(
                audio_files, noise_db=noise_db, min_silence_sec=min_silence
            )
        elif method == "tracks":
            detected = detector.detect_from_tracks(ab_set)
        elif method == "audnexus":
            detected = asyncio.run(_detect_audnexus(ab_set, ctx))
        else:
            detected = asyncio.run(detector.auto_detect(ab_set))

        if not detected:
            click.echo("  No chapters detected.")
            continue

        click.echo(f"  Found {len(detected)} chapter(s) (source: {detected[0].source}):")
        for ch in detected:
            start = _format_ms(ch.start_ms)
            end = _format_ms(ch.end_ms) if ch.end_ms else "?"
            click.echo(f"    {start} - {end}  {ch.title}")


async def _detect_audnexus(
    ab_set: "AudiobookSet", ctx: click.Context
) -> list:
    """Helper to detect chapters via Audnexus."""
    from .chapters.detector import ChapterDetector
    from .providers.audnexus import AudnexusProvider

    provider = AudnexusProvider()
    detector = ChapterDetector()
    try:
        return await detector.auto_detect(ab_set, audnexus=provider)
    finally:
        await provider.close()


@chapters.command("apply")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["ffmetadata", "cue"]),
    default="ffmetadata",
    help="Output format",
)
@click.option("--dry-run", is_flag=True, help="Show what would be done")
@click.pass_context
def chapters_apply(
    ctx: click.Context, folder: Path, out_format: str, dry_run: bool
) -> None:
    """Write detected chapters to files."""
    import asyncio

    from .chapters.detector import ChapterDetector
    from .chapters.writer import ChapterWriter
    from .core.discovery import AudioFileScanner

    scanner = AudioFileScanner(recursive=True)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found.")
        return

    detector = ChapterDetector()
    writer = ChapterWriter()

    for ab_set in audiobook_sets:
        detected = asyncio.run(detector.auto_detect(ab_set))
        if not detected:
            click.echo(
                f"No chapters detected for "
                f"{ab_set.raw_title_guess or ab_set.source_path.name}"
            )
            continue

        if dry_run:
            click.echo(
                f"Would write {len(detected)} chapters for "
                f"{ab_set.raw_title_guess or ab_set.source_path.name}"
            )
            continue

        if out_format == "ffmetadata":
            out_path = ab_set.source_path / "chapters.txt"
            success = writer.write_to_ffmetadata(out_path, detected)
        else:
            out_path = ab_set.source_path / "chapters.cue"
            success = writer.write_to_cue(out_path, detected)

        if success:
            click.echo(f"Wrote {len(detected)} chapters to {out_path}")
        else:
            click.echo(f"Failed to write chapters for {ab_set.source_path.name}", err=True)


def _format_ms(ms: int) -> str:
    """Format milliseconds as HH:MM:SS."""
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# --- M4B Commands (Feature 2B) ---


@cli.group()
def m4b() -> None:
    """M4B audiobook file operations."""
    pass


@m4b.command("merge")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), required=True)
@click.option("--chapters-mode", type=click.Choice(["auto", "none"]), default="auto")
@click.option("--cover", type=click.Path(exists=True, path_type=Path))
@click.option("--normalize", is_flag=True)
def m4b_merge(
    folder: Path,
    output: Path,
    chapters_mode: str,
    cover: Path | None,
    normalize: bool,
) -> None:
    """Merge audio files in a directory into a single M4B."""
    import asyncio

    from .convert.ffmpeg import FFmpegWrapper
    from .core.discovery import AudioFileScanner

    scanner = AudioFileScanner(recursive=False)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audio files found.")
        return

    ab_set = audiobook_sets[0]
    input_files = sorted(
        [t.src_path for t in ab_set.tracks], key=lambda p: p.name
    )

    click.echo(f"Merging {len(input_files)} files into {output}...")

    ffmpeg = FFmpegWrapper()

    chapter_list = None
    if chapters_mode == "auto":
        from .chapters.detector import ChapterDetector

        detector = ChapterDetector()
        chapter_list = asyncio.run(detector.auto_detect(ab_set))

    try:
        result = ffmpeg.merge_to_m4b(
            input_files, output, chapters=chapter_list, cover=cover
        )
        click.echo(f"Merge complete: {result}")
    except RuntimeError as e:
        click.echo(f"Merge failed: {e}", err=True)
        sys.exit(1)


@m4b.command("split")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output-dir", type=click.Path(path_type=Path), required=True)
@click.option("--format", "out_format", type=click.Choice(["m4a", "mp3"]), default="m4a")
def m4b_split(input_file: Path, output_dir: Path, out_format: str) -> None:
    """Split an M4B file by chapters."""
    from .convert.ffmpeg import FFmpegWrapper

    ffmpeg = FFmpegWrapper()
    click.echo(f"Splitting {input_file.name} by chapters...")

    output_files = ffmpeg.split_m4b(input_file, output_dir, output_format=out_format)

    if output_files:
        click.echo(f"Created {len(output_files)} file(s) in {output_dir}")
    else:
        click.echo("No chapters found or split failed.", err=True)
        sys.exit(1)


@m4b.command("chapters")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def m4b_chapters(input_file: Path) -> None:
    """Display embedded chapters in an M4B file."""
    from .convert.ffmpeg import FFmpegWrapper

    ffmpeg = FFmpegWrapper()
    extracted = ffmpeg.extract_chapters(input_file)

    if not extracted:
        click.echo("No chapters found.")
        return

    click.echo(f"Chapters in {input_file.name}:")
    for i, ch in enumerate(extracted, 1):
        start = _format_ms(ch.start_ms)
        end = _format_ms(ch.end_ms) if ch.end_ms else "?"
        click.echo(f"  {i:3d}. {start} - {end}  {ch.title}")


@m4b.command("tag")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--title", type=str)
@click.option("--author", type=str)
@click.option("--cover", type=click.Path(exists=True, path_type=Path))
def m4b_tag(
    input_file: Path,
    title: str | None,
    author: str | None,
    cover: Path | None,
) -> None:
    """Update metadata tags on an M4B file."""
    from .convert.ffmpeg import FFmpegWrapper

    ffmpeg = FFmpegWrapper()
    metadata: dict[str, str] = {}
    if title:
        metadata["title"] = title
        metadata["album"] = title
    if author:
        metadata["artist"] = author
        metadata["albumartist"] = author

    if not metadata and not cover:
        click.echo("No metadata specified. Use --title, --author, or --cover.")
        return

    try:
        ffmpeg.embed_metadata(input_file, metadata, cover=cover)
        click.echo(f"Updated metadata on {input_file.name}")
    except RuntimeError as e:
        click.echo(f"Failed to update metadata: {e}", err=True)
        sys.exit(1)


# --- Sidecar Commands (Feature 2C) ---


@cli.group()
def sidecar() -> None:
    """Sidecar metadata file operations."""
    pass


@sidecar.command("read")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
def sidecar_read(folder: Path) -> None:
    """Display detected sidecar metadata."""
    from .io.sidecar import SidecarManager

    manager = SidecarManager()
    identity = manager.auto_detect_sidecar(folder)

    if not identity:
        click.echo("No sidecar metadata files found.")
        return

    click.echo(f"Source: {identity.provider}")
    click.echo(f"Title: {identity.title}")
    if identity.authors:
        click.echo(f"Authors: {', '.join(identity.authors)}")
    if identity.series_name:
        series_str = identity.series_name
        if identity.series_index:
            series_str += f" #{identity.series_index}"
        click.echo(f"Series: {series_str}")
    if identity.narrator:
        click.echo(f"Narrator: {identity.narrator}")
    if identity.year:
        click.echo(f"Year: {identity.year}")
    if identity.isbn_13 or identity.isbn_10:
        click.echo(f"ISBN: {identity.isbn_13 or identity.isbn_10}")
    if identity.asin:
        click.echo(f"ASIN: {identity.asin}")
    if identity.publisher:
        click.echo(f"Publisher: {identity.publisher}")
    if identity.language:
        click.echo(f"Language: {identity.language}")


@sidecar.command("write")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["opf", "json"]),
    default="opf",
    help="Output format",
)
@click.option(
    "--from-tags",
    is_flag=True,
    help="Generate from embedded audio tags",
)
@click.pass_context
def sidecar_write(
    ctx: click.Context, folder: Path, out_format: str, from_tags: bool
) -> None:
    """Generate sidecar metadata from tags or matched identity."""
    from .core.discovery import AudioFileScanner
    from .core.models import ProviderIdentity
    from .io.sidecar import SidecarManager

    scanner = AudioFileScanner(recursive=False)
    audiobook_sets = scanner.scan_directory(folder)

    if not audiobook_sets:
        click.echo("No audiobooks found in directory.")
        return

    ab_set = audiobook_sets[0]
    manager = SidecarManager()

    if ab_set.chosen_identity and not from_tags:
        identity = ab_set.chosen_identity
    else:
        # Build identity from tags
        identity = ProviderIdentity(
            provider="local_tags",
            external_id=str(folder),
            title=ab_set.raw_title_guess or folder.name,
            authors=[ab_set.author_guess] if ab_set.author_guess else [],
            series_name=ab_set.series_guess,
            year=ab_set.year_guess,
            narrator=ab_set.narrator_guess,
            language=ab_set.language_guess,
        )

    if out_format == "opf":
        out_path = folder / "metadata.opf"
        manager.write_opf(out_path, identity)
    else:
        out_path = folder / "metadata.json"
        manager.write_metadata_json(out_path, identity)

    click.echo(f"Wrote sidecar metadata to {out_path}")


@sidecar.command("sync")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
def sidecar_sync(folder: Path) -> None:
    """Read sidecar metadata and display it for review."""
    from .io.sidecar import SidecarManager

    manager = SidecarManager()
    identity = manager.auto_detect_sidecar(folder)

    if identity:
        click.echo(f"Found sidecar metadata: {identity.title}")
        click.echo("Use 'bookbot sidecar read' for full details.")
    else:
        click.echo("No sidecar metadata found. Use 'bookbot sidecar write' to create one.")


# --- ABS (Audiobookshelf) Commands (Feature 1C) ---


@cli.group()
def abs() -> None:
    """Audiobookshelf server commands."""
    pass


@abs.command("login")
@click.option("--server", required=True, help="Audiobookshelf server URL")
@click.option("--username", required=True, help="Username")
@click.pass_context
def abs_login(ctx: click.Context, server: str, username: str) -> None:
    """Authenticate with an Audiobookshelf server."""
    import asyncio

    from .abs.client import AudiobookshelfClient

    password = click.prompt("Password", hide_input=True)
    config_manager = ctx.obj["config_manager"]

    token = asyncio.run(AudiobookshelfClient.login(server, username, password))

    if token:
        config = config_manager.load_config()
        config.abs.server_url = server
        config.abs.api_token = token
        config.abs.username = username
        config_manager.save_config(config)
        click.echo("Login successful. Token saved to config.")
    else:
        click.echo("Login failed. Check your server URL and credentials.", err=True)
        sys.exit(1)


def _get_abs_client(ctx: click.Context) -> "AudiobookshelfClient":
    """Get an ABS client from config."""
    from .abs.client import AudiobookshelfClient

    config_manager = ctx.obj["config_manager"]
    config = config_manager.load_config()

    if not config.abs.server_url or not config.abs.api_token:
        click.echo(
            "Not configured. Run 'bookbot abs login --server URL --username USER' first.",
            err=True,
        )
        sys.exit(1)

    return AudiobookshelfClient(config.abs.server_url, config.abs.api_token)


@abs.command("libraries")
@click.pass_context
def abs_libraries(ctx: click.Context) -> None:
    """List all libraries on the server."""
    import asyncio

    client = _get_abs_client(ctx)
    libraries = asyncio.run(client.get_libraries())

    if not libraries:
        click.echo("No libraries found.")
        return

    click.echo("Libraries:")
    for lib in libraries:
        media_type = lib.get("mediaType", "unknown")
        click.echo(f"  {lib.get('id', '?')}  {lib.get('name', '?')}  ({media_type})")


@abs.command("search")
@click.argument("library_id", type=str)
@click.argument("query", type=str)
@click.pass_context
def abs_search(ctx: click.Context, library_id: str, query: str) -> None:
    """Search a library for audiobooks."""
    import asyncio

    client = _get_abs_client(ctx)
    results = asyncio.run(client.search_library(library_id, query))

    book_results = results.get("book", results.get("books", []))
    if not book_results:
        click.echo("No results found.")
        return

    click.echo(f"Search results for '{query}':")
    for item in book_results:
        if isinstance(item, dict):
            lib_item = item.get("libraryItem", item)
            media = lib_item.get("media", {})
            metadata = media.get("metadata", {})
            title = metadata.get("title", lib_item.get("title", "?"))
            author = metadata.get("authorName", "")
            item_id = lib_item.get("id", "?")
            click.echo(f"  [{item_id}] {title} - {author}")


@abs.command("list")
@click.argument("library_id", type=str)
@click.option("--limit", type=int, default=20)
@click.option("--page", type=int, default=0)
@click.pass_context
def abs_list(ctx: click.Context, library_id: str, limit: int, page: int) -> None:
    """List items in a library."""
    import asyncio

    client = _get_abs_client(ctx)
    data = asyncio.run(client.get_library_items(library_id, limit=limit, page=page))

    results = data.get("results", [])
    total = data.get("total", len(results))

    if not results:
        click.echo("No items found.")
        return

    click.echo(f"Items (page {page}, {len(results)} of {total}):")
    for item in results:
        media = item.get("media", {})
        metadata = media.get("metadata", {})
        title = metadata.get("title", item.get("title", "?"))
        author = metadata.get("authorName", "")
        item_id = item.get("id", "?")
        click.echo(f"  [{item_id}] {title} - {author}")


@abs.command("show")
@click.argument("item_id", type=str)
@click.pass_context
def abs_show(ctx: click.Context, item_id: str) -> None:
    """Show full item details."""
    import asyncio

    client = _get_abs_client(ctx)
    item = asyncio.run(client.get_item(item_id))

    if not item:
        click.echo("Item not found.")
        return

    media = item.get("media", {})
    metadata = media.get("metadata", {})

    click.echo(f"Title: {metadata.get('title', '?')}")
    click.echo(f"Author: {metadata.get('authorName', '?')}")
    click.echo(f"Narrator: {metadata.get('narratorName', '?')}")

    if metadata.get("series"):
        click.echo(f"Series: {metadata['series']}")

    duration = media.get("duration", 0)
    if duration:
        hours = int(duration // 3600)
        minutes = int((duration % 3600) // 60)
        click.echo(f"Duration: {hours}h {minutes}m")

    audio_files = media.get("audioFiles", [])
    if audio_files:
        click.echo(f"Audio Files: {len(audio_files)}")

    chapters = media.get("chapters", [])
    if chapters:
        click.echo(f"Chapters: {len(chapters)}")
        for ch in chapters[:10]:
            click.echo(f"  - {ch.get('title', '?')}")
        if len(chapters) > 10:
            click.echo(f"  ... and {len(chapters) - 10} more")

    # Progress
    progress = asyncio.run(client.get_progress(item_id))
    if progress:
        pct = progress.get("progress", 0) * 100
        finished = progress.get("isFinished", False)
        status = "Finished" if finished else f"{pct:.1f}%"
        click.echo(f"Progress: {status}")


@abs.command("match")
@click.argument("item_id", type=str)
@click.option("--provider", default="audnexus", help="Metadata provider to use")
@click.pass_context
def abs_match(ctx: click.Context, item_id: str, provider: str) -> None:
    """Trigger metadata match for an item."""
    import asyncio

    client = _get_abs_client(ctx)
    result = asyncio.run(client.match_item(item_id, provider=provider))

    if result:
        click.echo(f"Match triggered for item {item_id} using {provider}")
    else:
        click.echo("Match request failed.", err=True)
        sys.exit(1)


@abs.command("match-all")
@click.argument("library_id", type=str)
@click.pass_context
def abs_match_all(ctx: click.Context, library_id: str) -> None:
    """Batch match all items in a library."""
    import asyncio

    client = _get_abs_client(ctx)
    result = asyncio.run(client.batch_match(library_id))

    if result:
        click.echo(f"Batch match started for library {library_id}")
    else:
        click.echo("Batch match request failed.", err=True)
        sys.exit(1)


@abs.command("progress")
@click.argument("item_id", type=str)
@click.option("--set", "set_val", type=float, help="Set progress (0.0 - 1.0)")
@click.pass_context
def abs_progress(ctx: click.Context, item_id: str, set_val: float | None) -> None:
    """Get or set playback progress for an item."""
    import asyncio

    client = _get_abs_client(ctx)

    if set_val is not None:
        result = asyncio.run(
            client.update_progress(item_id, set_val, set_val * 3600)
        )
        if result is not None:
            click.echo(f"Progress set to {set_val:.1%}")
        else:
            click.echo("Failed to update progress.", err=True)
            sys.exit(1)
    else:
        progress = asyncio.run(client.get_progress(item_id))
        if progress:
            pct = progress.get("progress", 0)
            current = progress.get("currentTime", 0)
            finished = progress.get("isFinished", False)
            click.echo(f"Progress: {pct:.1%}")
            click.echo(f"Current Time: {current:.0f}s")
            click.echo(f"Finished: {finished}")
        else:
            click.echo("No progress data found.")


@abs.command("stats")
@click.pass_context
def abs_stats(ctx: click.Context) -> None:
    """Show listening statistics."""
    import asyncio

    client = _get_abs_client(ctx)
    stats = asyncio.run(client.get_stats())

    if not stats:
        click.echo("No statistics available.")
        return

    total_time = stats.get("totalTime", 0)
    hours = int(total_time // 3600)
    days_listened = stats.get("days", {})
    recent_sessions = stats.get("recentSessions", [])

    click.echo("Listening Statistics:")
    click.echo(f"  Total listening time: {hours} hours")
    click.echo(f"  Days with listening: {len(days_listened)}")
    click.echo(f"  Recent sessions: {len(recent_sessions)}")


@abs.command("collections")
@click.argument("library_id", type=str)
@click.pass_context
def abs_collections(ctx: click.Context, library_id: str) -> None:
    """List collections in a library."""
    import asyncio

    client = _get_abs_client(ctx)
    collections = asyncio.run(client.get_collections(library_id))

    if not collections:
        click.echo("No collections found.")
        return

    click.echo("Collections:")
    for col in collections:
        name = col.get("name", "?")
        book_count = len(col.get("books", []))
        click.echo(f"  {col.get('id', '?')}  {name} ({book_count} books)")


@abs.command("sync")
@click.option(
    "--direction",
    type=click.Choice(["pull", "push", "both"]),
    default="both",
    help="Sync direction",
)
@click.option("--watch", is_flag=True, help="Continuous sync mode")
@click.option("--interval", type=int, default=60, help="Sync interval in seconds")
@click.pass_context
def abs_sync(
    ctx: click.Context, direction: str, watch: bool, interval: int
) -> None:
    """Synchronize playback progress with ABS server."""
    import asyncio

    from .abs.sync import ProgressSyncDaemon

    client = _get_abs_client(ctx)
    daemon = ProgressSyncDaemon(client)

    async def do_sync() -> None:
        if direction == "pull":
            pulled = await daemon.sync_from_server()
            click.echo(f"Pulled {len(pulled)} progress entries from server")
        elif direction == "push":
            report = await daemon.sync_all()
            click.echo(f"Pushed {report.pushed} entries to server")
        else:
            report = await daemon.sync_all()
            click.echo(
                f"Sync complete: pulled {report.pulled}, pushed {report.pushed}"
            )
            if report.errors:
                for err in report.errors:
                    click.echo(f"  Error: {err}", err=True)

    if watch:
        click.echo(f"Starting continuous sync (every {interval}s)...")
        click.echo("Press Ctrl+C to stop.")

        async def watch_loop() -> None:
            while True:
                await do_sync()
                await asyncio.sleep(interval)

        try:
            asyncio.run(watch_loop())
        except KeyboardInterrupt:
            click.echo("\nSync stopped.")
    else:
        asyncio.run(do_sync())


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Execute the plan (default is dry-run)",
)
@click.option(
    "--files-only",
    is_flag=True,
    default=False,
    help="Only deduplicate files, skip editions",
)
@click.option(
    "--editions-only",
    is_flag=True,
    default=False,
    help="Only deduplicate editions, skip files",
)
@click.option(
    "--audio-hash",
    is_flag=True,
    default=False,
    help=(
        "Reserved for future decoded-audio hashing; "
        "currently only checks ffmpeg availability"
    ),
)
@click.option(
    "--json",
    "json_path",
    type=click.Path(path_type=Path),
    help="Write plan to JSON file",
)
@click.pass_context
def dedupe(
    ctx: click.Context,
    folder: Path,
    apply: bool,
    files_only: bool,
    editions_only: bool,
    audio_hash: bool,
    json_path: Path | None,
) -> None:
    """Find and quarantine duplicate audiobooks and files."""
    from .core.dedupe import DedupeEngine
    from .core.discovery import AudioFileScanner

    if audio_hash:
        click.echo(
            "Error: --audio-hash is not implemented yet. Refusing to continue "
            "with byte-hash-only dedupe.",
            err=True,
        )
        sys.exit(1)

    scanner = AudioFileScanner(recursive=True, max_depth=5)
    audiobook_sets = scanner.scan_directory(folder)

    engine = DedupeEngine(folder)

    edition_groups = None
    file_groups = None

    if not files_only:
        edition_groups = engine.analyze_editions(audiobook_sets)
        for warning in engine.analysis_warnings:
            click.echo(f"Warning: {warning}", err=True)

    keeper_paths: set[Path] = set()
    if edition_groups:
        for g in edition_groups:
            if g.keeper:
                keeper_paths.add(g.keeper.audiobook_set.source_path)

    if not editions_only:
        file_groups = engine.analyze_files()

    plan = engine.build_plan(edition_groups, file_groups, keeper_paths)

    if json_path:
        import json as json_mod

        json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json_mod.dump(plan.to_dict(), f, indent=2, default=str)
        click.echo(f"Plan written to {json_path}")

    if not plan.operations:
        click.echo("No duplicates found.")
        return

    # Dry-run output
    if edition_groups:
        click.echo(f"\n--- Edition duplicates: {len(edition_groups)} group(s) ---")
        for i, g in enumerate(edition_groups, 1):
            click.echo(f"\nGroup {i}:")
            for c in g.members:
                marker = "  [KEEP]" if c.is_keeper else "  [QUARANTINE]"
                click.echo(f"  {marker} {c.audiobook_set.source_path}")
                if not c.is_keeper:
                    click.echo(f"         Reason: {c.quarantine_reason}")

    if file_groups:
        click.echo(f"\n--- Byte-identical files: {len(file_groups)} group(s) ---")
        for fg_info in plan.file_groups:
            click.echo(f"\n  Keeper: {fg_info['keeper']}")
            for q in fg_info["quarantined"]:
                click.echo(f"  [QUARANTINE] {q}")

    reclaimable = plan.total_reclaimable_bytes
    if reclaimable > 0:
        mb = reclaimable / (1024 * 1024)
        click.echo(f"\nTotal reclaimable: {mb:.1f} MB")

    click.echo(f"Quarantine operations: {len(plan.operations)}")

    if apply:
        if plan.has_conflicts():
            click.echo(
                "Error: plan has conflicts (destination files exist). "
                "Aborting.",
                err=True,
            )
            sys.exit(1)
        config_manager = ctx.obj["config_manager"]
        engine.execute_plan(plan, config_manager)
        click.echo(
            f"Done. {len(plan.operations)} files quarantined to "
            f"{plan.quarantine_root}"
        )
        click.echo(f"To undo: bookbot undo {plan.plan_id}")
    else:
        click.echo("\nDry run — no files moved. Use --apply to execute.")


def main() -> None:
    """Main entry point for the CLI."""
    try:
        cli()
    except KeyboardInterrupt:
        click.echo("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
