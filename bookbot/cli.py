"""Command-line interface for BookBot."""

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click

from .config.manager import ConfigManager
from .core.discovery import AudioFileScanner
from .core.operations import TransactionManager
from .providers.openlibrary import OpenLibraryProvider


@click.group()
@click.version_option()
@click.option('--config-dir', type=click.Path(path_type=Path),
              help='Configuration directory path')
@click.pass_context
def cli(ctx: click.Context, config_dir: Optional[Path]) -> None:
    """BookBot - A cross-platform TUI audiobook renamer and organizer."""
    # Ensure that ctx.obj exists and is a dict
    ctx.ensure_object(dict)

    # Initialize configuration manager
    ctx.obj['config_manager'] = ConfigManager(config_dir)


@cli.command()
@click.argument('folder', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--dry-run', is_flag=True, default=True,
              help='Show what would be done without making changes')
@click.option('--profile', type=str, help='Configuration profile to use')
@click.option('--recurse', type=int, default=5, help='Maximum recursion depth')
@click.option('--no-tag', is_flag=True, help='Skip tagging operations')
@click.option('--template', type=str, help='Naming template to use')
@click.option('--lang', type=str, default='en', help='Preferred language')
@click.option('--cache', type=click.Path(path_type=Path), help='Cache directory')
@click.option('--log', type=click.Path(path_type=Path), help='Log file path')
@click.pass_context
def scan(ctx: click.Context, folder: Path, dry_run: bool, profile: Optional[str],
         recurse: int, no_tag: bool, template: Optional[str], lang: str,
         cache: Optional[Path], log: Optional[Path]) -> None:
    """Scan a folder for audiobooks and propose renames."""
    config_manager = ctx.obj['config_manager']

    # Apply profile if specified
    if profile:
        if not config_manager.apply_profile(profile):
            click.echo(f"Error: Profile '{profile}' not found", err=True)
            sys.exit(1)

    config = config_manager.load_config()

    # Override config with command line options
    if no_tag:
        config.tagging.enabled = False
    if template:
        config.active_template = template

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

            if audiobook_set.warnings:
                click.echo("   Warnings:")
                for warning in audiobook_set.warnings:
                    click.echo(f"     - {warning}")

        if dry_run:
            click.echo("\nDry run completed. Use 'bookbot tui' for interactive processing.")

    except Exception as e:
        click.echo(f"Error scanning directory: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('folders', nargs=-1, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--profile', type=str, help='Configuration profile to use')
@click.pass_context
def tui(ctx: click.Context, folders: tuple[Path, ...], profile: Optional[str]) -> None:
    """Launch the interactive TUI for audiobook processing."""
    config_manager = ctx.obj['config_manager']

    # Apply profile if specified
    if profile:
        if not config_manager.apply_profile(profile):
            click.echo(f"Error: Profile '{profile}' not found", err=True)
            sys.exit(1)

    if not folders:
        click.echo("Error: At least one folder must be specified", err=True)
        sys.exit(1)

    try:
        # Import TUI app here to avoid issues if textual is not installed
        from .tui.app import BookBotApp

        app = BookBotApp(config_manager, list(folders))
        app.run()

    except ImportError:
        click.echo("Error: Textual is required for TUI mode. Install with: pip install textual", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error running TUI: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('folder', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('-o', '--output', type=click.Path(path_type=Path), required=True,
              help='Output directory for converted files')
@click.option('--profile', type=str, help='Configuration profile to use')
@click.option('--bitrate', type=str, default='128k', help='AAC bitrate (e.g., 128k)')
@click.option('--vbr', type=int, help='VBR quality (1-6, overrides bitrate)')
@click.option('--normalize', is_flag=True, help='Normalize audio levels')
@click.option('--chapters', type=click.Choice(['auto', 'from-tags']), default='auto',
              help='Chapter creation method')
@click.option('--no-art', is_flag=True, help='Skip cover art embedding')
@click.option('--dry-run', is_flag=True, help='Show conversion plan without executing')
@click.pass_context
def convert(ctx: click.Context, folder: Path, output: Path, profile: Optional[str],
           bitrate: str, vbr: Optional[int], normalize: bool, chapters: str,
           no_art: bool, dry_run: bool) -> None:
    """Convert audiobooks to M4B format."""
    config_manager = ctx.obj['config_manager']

    # Apply profile if specified
    if profile:
        if not config_manager.apply_profile(profile):
            click.echo(f"Error: Profile '{profile}' not found", err=True)
            sys.exit(1)

    config = config_manager.load_config()

    # Check if conversion is enabled in config
    if not config.conversion.enabled:
        click.echo("Error: M4B conversion is not enabled in configuration", err=True)
        click.echo("Enable it with a conversion profile or modify your config")
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
        conv_config.chapter_naming = chapters
        conv_config.write_cover_art = not no_art

        click.echo(f"Converting audiobooks from {folder} to {output}...")

        if dry_run:
            # Show conversion plan
            plan = pipeline.create_conversion_plan(folder, conv_config)
            click.echo(f"Conversion plan created with {len(plan.operations)} operation(s)")
            # TODO: Display plan details
        else:
            # Execute conversion
            success = pipeline.convert_directory(folder, conv_config)
            if success:
                click.echo("Conversion completed successfully!")
            else:
                click.echo("Conversion failed!", err=True)
                sys.exit(1)

    except ImportError as e:
        if 'ffmpeg' in str(e).lower():
            click.echo("Error: FFmpeg is required for conversion. Please install FFmpeg.", err=True)
        else:
            click.echo(f"Error: Missing dependency for conversion: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during conversion: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('transaction_id', type=str)
@click.pass_context
def undo(ctx: click.Context, transaction_id: str) -> None:
    """Undo a previous operation by transaction ID."""
    config_manager = ctx.obj['config_manager']
    transaction_manager = TransactionManager(config_manager)

    try:
        if transaction_manager.undo_transaction(transaction_id):
            click.echo(f"Transaction {transaction_id} undone successfully")
        else:
            click.echo(f"Failed to undo transaction {transaction_id}", err=True)
            sys.exit(1)
    except Exception as e:
        click.echo(f"Error undoing transaction: {e}", err=True)
        sys.exit(1)


@cli.group()
def config() -> None:
    """Configuration management commands."""
    pass


@config.command('list')
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """List configuration profiles."""
    config_manager = ctx.obj['config_manager']
    profiles = config_manager.list_profiles()

    if not profiles:
        click.echo("No profiles found")
        return

    click.echo("Available profiles:")
    for name, description in profiles.items():
        click.echo(f"  {name}: {description}")


@config.command('show')
@click.argument('profile_name', type=str, required=False)
@click.pass_context
def config_show(ctx: click.Context, profile_name: Optional[str]) -> None:
    """Show configuration details."""
    config_manager = ctx.obj['config_manager']

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
    click.echo(f"Tagging enabled: {config_data.tagging.enabled}")
    click.echo(f"Conversion enabled: {config_data.conversion.enabled}")


@config.command('reset')
@click.confirmation_option(prompt='Reset configuration to defaults?')
@click.pass_context
def config_reset(ctx: click.Context) -> None:
    """Reset configuration to defaults."""
    config_manager = ctx.obj['config_manager']
    config_manager.reset_to_defaults()
    click.echo("Configuration reset to defaults")


@cli.group()
def provider() -> None:
    """Metadata provider management commands."""
    pass


@provider.command('list')
@click.pass_context
def provider_list(ctx: click.Context) -> None:
    """List available metadata providers."""
    try:
        from .providers.manager import ProviderManager

        config_manager = ctx.obj['config_manager']
        provider_manager = ProviderManager(config_manager)

        providers_info = provider_manager.list_providers()

        click.echo("Available metadata providers:")
        click.echo("")

        for provider_id, info in providers_info.items():
            status_icon = "✅" if info["status"] == "enabled" else "❌"
            click.echo(f"{status_icon} {info['name']} ({provider_id})")
            click.echo(f"   Description: {info['description']}")
            click.echo(f"   Requires API Key: {info['requires_api_key']}")

            if provider_id == "googlebooks" and info.get("api_key_provided") is False:
                click.echo("   ⚠️  API key not configured - provider disabled")
            elif provider_id == "audible" and "marketplace" in info:
                click.echo(f"   Marketplace: {info['marketplace']}")

            click.echo("")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)


@provider.command('enable')
@click.argument('provider_name', type=str)
@click.pass_context
def provider_enable(ctx: click.Context, provider_name: str) -> None:
    """Enable a metadata provider."""
    config_manager = ctx.obj['config_manager']
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
    elif provider_name == "openlibrary":
        click.echo("OpenLibrary is always enabled as the default provider")
        return
    else:
        click.echo(f"Error: Unknown provider '{provider_name}'")
        click.echo("Available providers: googlebooks, librivox, audible")
        sys.exit(1)

    config_manager.save_config(config)
    click.echo(f"Provider '{provider_name}' enabled")


@provider.command('disable')
@click.argument('provider_name', type=str)
@click.pass_context
def provider_disable(ctx: click.Context, provider_name: str) -> None:
    """Disable a metadata provider."""
    config_manager = ctx.obj['config_manager']
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
    else:
        click.echo(f"Error: Unknown provider '{provider_name}'")
        sys.exit(1)

    config_manager.save_config(config)
    click.echo(f"Provider '{provider_name}' disabled")


@provider.command('set-key')
@click.argument('provider_name', type=str)
@click.argument('api_key', type=str)
@click.pass_context
def provider_set_key(ctx: click.Context, provider_name: str, api_key: str) -> None:
    """Set API key for a provider."""
    config_manager = ctx.obj['config_manager']
    config = config_manager.load_config()

    provider_name = provider_name.lower()

    if provider_name == "googlebooks":
        config.providers.google_books.api_key = api_key
        config.providers.google_books.enabled = True
        config_manager.save_config(config)
        click.echo("Google Books API key set and provider enabled")
    else:
        click.echo(f"Error: Provider '{provider_name}' does not require an API key")
        sys.exit(1)


@provider.command('set-marketplace')
@click.argument('marketplace', type=click.Choice(['US', 'UK', 'CA', 'AU', 'FR', 'DE', 'IT', 'ES', 'JP', 'IN']))
@click.pass_context
def provider_set_marketplace(ctx: click.Context, marketplace: str) -> None:
    """Set Audible marketplace."""
    config_manager = ctx.obj['config_manager']
    config = config_manager.load_config()

    config.providers.audible.marketplace = marketplace.upper()
    config_manager.save_config(config)
    click.echo(f"Audible marketplace set to {marketplace}")


@cli.command()
@click.option('--days', type=int, default=30, help='Show transactions from last N days')
@click.pass_context
def history(ctx: click.Context, days: int) -> None:
    """Show operation history."""
    config_manager = ctx.obj['config_manager']
    transaction_manager = TransactionManager(config_manager)

    transactions = transaction_manager.list_transactions(days)

    if not transactions:
        click.echo("No transactions found")
        return

    click.echo(f"Transactions from last {days} days:")
    for transaction in transactions:
        status = transaction.get('status', 'completed')
        undo_info = "" if transaction['can_undo'] else " (cannot undo)"
        click.echo(f"  {transaction['id'][:8]}... - "
                  f"{transaction['timestamp']} - "
                  f"{transaction['operation_count']} operations - "
                  f"{status}{undo_info}")


@cli.group()
def drm() -> None:
    """DRM detection and removal commands."""
    pass


@drm.command('detect')
@click.argument('files', nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option('--recursive', '-r', is_flag=True, help='Scan directories recursively')
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
                audio_extensions = {'.mp3', '.m4a', '.m4b', '.aax', '.aaxc', '.flac', '.ogg', '.opus', '.aac', '.wav'}
                for file_path in path.rglob('*'):
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

            status_icon = "🔒" if drm_info.is_protected else "✅"
            click.echo(f"{status_icon} {file_path.name}: {drm_info.drm_type.value}")

            if drm_info.is_protected:
                protected_count += 1
                if drm_info.metadata:
                    for key, value in drm_info.metadata.items():
                        click.echo(f"    {key}: {value}")

        click.echo(f"\nSummary: {protected_count}/{total_count} files have DRM protection")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during DRM detection: {e}", err=True)
        sys.exit(1)


@drm.command('remove')
@click.argument('files', nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option('-o', '--output-dir', type=click.Path(path_type=Path),
              help='Output directory for DRM-free files')
@click.option('--activation-bytes', type=str,
              help='Audible activation bytes for AAX files')
@click.option('--dry-run', is_flag=True, help='Show what would be done without removing DRM')
@click.option('--recursive', '-r', is_flag=True, help='Process directories recursively')
@click.pass_context
def drm_remove(ctx: click.Context, files: tuple[Path, ...], output_dir: Optional[Path],
               activation_bytes: Optional[str], dry_run: bool, recursive: bool) -> None:
    """Remove DRM protection from audio files."""
    if not files:
        click.echo("Error: At least one file or directory must be specified", err=True)
        sys.exit(1)

    try:
        from .drm.remover import DRMRemover
        from .drm.detector import DRMDetector

        detector = DRMDetector()
        remover = DRMRemover(activation_bytes=activation_bytes)

        # Check ffmpeg availability
        if not remover.check_ffmpeg_availability():
            click.echo("Warning: FFmpeg with activation_bytes support not found. "
                      "AAX DRM removal will not work.", err=True)

        # Collect all files to process
        files_to_process = []
        for path in files:
            if path.is_file():
                files_to_process.append(path)
            elif path.is_dir() and recursive:
                audio_extensions = {'.mp3', '.m4a', '.m4b', '.aax', '.aaxc', '.flac', '.ogg', '.opus', '.aac', '.wav'}
                for file_path in path.rglob('*'):
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
                click.echo(f"✅ {file_path.name}: No DRM protection")
                success_count += 1
                continue

            if dry_run:
                click.echo(f"🔒 {file_path.name}: Would remove {drm_info.drm_type.value} DRM")
                continue

            # Remove DRM
            output_path = None
            if output_dir:
                output_path = output_dir / f"{file_path.stem}_no_drm.m4a"

            result = remover.remove_drm(file_path, output_path, activation_bytes)

            if result.success:
                click.echo(f"✅ {file_path.name}: DRM removed successfully")
                if result.output_file:
                    click.echo(f"    Output: {result.output_file}")
                success_count += 1
            else:
                click.echo(f"❌ {file_path.name}: Failed - {result.error_message}")
                error_count += 1

        if dry_run:
            click.echo(f"\nDry run completed. Found {len(files_to_process)} files to process.")
        else:
            click.echo(f"\nCompleted: {success_count} successful, {error_count} failed")

    except ImportError as e:
        click.echo(f"Error: Missing dependency: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error during DRM removal: {e}", err=True)
        sys.exit(1)


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


if __name__ == '__main__':
    main()