"""Command-line interface for BookBot."""

import shutil
import sys
from pathlib import Path

import click

from .config.manager import ConfigManager
from .core.discovery import AudioFileScanner
from .core.operations import TransactionManager


@click.group()
@click.version_option()
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
    lang: str,
    cache: Path | None,
    log: Path | None,
) -> None:
    """Scan a folder for audiobooks and propose renames."""
    config_manager = ctx.obj["config_manager"]

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
            click.echo("\n" + "─" * 60)
            click.echo("✓ Scan completed. Next steps:")
            click.echo("")
            click.echo("  📱 Interactive mode:")
            click.echo(f"     bookbot tui {folder}")
            click.echo("")
            click.echo("  🎵 Convert to M4B:")
            if audiobook_sets:
                example = audiobook_sets[0].source_path
                click.echo(f'     bookbot convert "{example}" -o ./output --dry-run')
            click.echo("")
            click.echo("  ⚙️  View config:")
            click.echo("     bookbot config show")
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

    if not folders:
        click.echo("Error: At least one folder must be specified", err=True)
        sys.exit(1)

    try:
        # Import TUI app here to avoid issues if textual is not installed
        from .providers.local import LocalMetadataProvider
        from .tui.app import BookBotApp

        provider = LocalMetadataProvider() if metadata_from_files else None
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
    type=click.Choice(["auto", "from-tags"]),
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
        click.echo("❌ Error: FFmpeg not found in PATH.", err=True)
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
            click.echo(f"❌ Error: Profile '{profile}' not found", err=True)
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
        click.echo(
            "⚠️  M4B conversion is currently disabled in your configuration.", err=True
        )
        click.echo("")
        if click.confirm("Would you like to enable it now?", default=True):
            config.conversion.enabled = True
            config_manager.save_config(config)
            click.echo("✓ Conversion enabled and saved to config.")
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
        conv_config.chapter_naming = chapters
        conv_config.write_cover_art = not no_art

        click.echo(f"Converting audiobooks from {folder} to {output}...")

        if dry_run:
            # Show conversion plan
            plan = pipeline.create_conversion_plan(folder, conv_config)
            click.echo(f"\n📋 Conversion Plan ({len(plan.operations)} operation(s)):")
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
            click.echo("✓ Dry run complete. No files were modified.")
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
    click.echo(f"Tagging enabled: {config_data.tagging.enabled}")
    click.echo(f"Conversion enabled: {config_data.conversion.enabled}")


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
        click.echo("❌ Error: Key must be in format 'section.field'", err=True)
        click.echo("\nExamples:", err=True)
        click.echo("  bookbot config set conversion.enabled true", err=True)
        click.echo("  bookbot config set conversion.bitrate 256k", err=True)
        sys.exit(1)

    section, field = parts

    # Get the section object
    if not hasattr(config, section):
        click.echo(f"❌ Error: Unknown section '{section}'", err=True)
        click.echo("\nAvailable sections: conversion, tagging, providers", err=True)
        sys.exit(1)

    section_obj = getattr(config, section)

    # Check if field exists
    if not hasattr(section_obj, field):
        click.echo(
            f"❌ Error: Unknown field '{field}' in section '{section}'", err=True
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
            click.echo(f"❌ Error: '{value}' is not a valid integer", err=True)
            sys.exit(1)
    elif isinstance(original_value, float):
        try:
            converted_value = float(value)
        except ValueError:
            click.echo(f"❌ Error: '{value}' is not a valid number", err=True)
            sys.exit(1)

    # Set the value
    setattr(section_obj, field, converted_value)
    config_manager.save_config(config)
    click.echo(f"✓ Set {key} = {value}")


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
        click.echo("❌ Error: Key must be in format 'section.field'", err=True)
        sys.exit(1)

    section, field = parts

    # Get the value
    if not hasattr(config, section):
        click.echo(f"❌ Error: Unknown section '{section}'", err=True)
        sys.exit(1)

    section_obj = getattr(config, section)

    if not hasattr(section_obj, field):
        click.echo(
            f"❌ Error: Unknown field '{field}' in section '{section}'", err=True
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
        click.echo("✓ Configuration file closed")
    except subprocess.CalledProcessError:
        click.echo(f"❌ Error opening editor. Edit manually: {config_file}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo(f"❌ Editor '{editor}' not found.", err=True)
        click.echo(f"\nEdit manually: {config_file}", err=True)
        sys.exit(1)


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
    elif provider_name == "openlibrary":
        click.echo("OpenLibrary is always enabled as the default provider")
        return
    else:
        click.echo(f"Error: Unknown provider '{provider_name}'")
        click.echo("Available providers: googlebooks, librivox, audible")
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
    else:
        click.echo(f"Error: Provider '{provider_name}' does not require an API key")
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
        click.echo(
            f"  {transaction['id'][:8]}... - "
            f"{transaction['timestamp']} - "
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
                "✅ Authentication successful! You can now import Audible books."
            )
        else:
            click.echo(
                "❌ Authentication failed. Please try again.",
                err=True,
            )
            sys.exit(1)

    except ImportError as e:
        click.echo(f"❌ Missing dependency: {e}", err=True)
        click.echo("Install the audible package with: pip install audible", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Authentication failed: {e}", err=True)
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
        cache_file = Path.home() / ".config" / "bookbot" / ".audible_library_cache.json"
        if not cache_file.exists():
            click.echo(
                "❌ No library cache found. Run 'bookbot audible list' first.", err=True
            )
            sys.exit(1)

        library = json.loads(cache_file.read_text())

        # Parse numbers (comma or space separated)
        numbers = numbers.replace(" ", ",")
        try:
            book_indices = [int(n.strip()) - 1 for n in numbers.split(",") if n.strip()]
        except ValueError:
            click.echo("❌ Invalid book numbers. Use format: 1,2,3", err=True)
            sys.exit(1)

        # Validate indices
        invalid = [i + 1 for i in book_indices if i < 0 or i >= len(library)]
        if invalid:
            click.echo(
                f"❌ Invalid book numbers: {invalid}. "
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
                "❌ Not authenticated. Run 'bookbot audible auth' first.", err=True
            )
            sys.exit(1)

        # Import each book
        click.echo(f"📚 Importing {len(book_indices)} book(s)...\n")

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
                click.echo(f"  ✅ Downloaded to: {book_path}")

                if remove_drm:
                    click.echo("  🔓 Removing DRM...")

                    # Try to get activation bytes from client if not provided
                    if not activation_bytes:
                        activation_bytes = client.get_activation_bytes()

                    if not activation_bytes:
                        click.echo(
                            "  ⚠️  No activation bytes available. Skipping DRM removal."
                        )
                    else:
                        remover = DRMRemover(activation_bytes=activation_bytes)
                        result = remover.remove_drm(book_path)

                        if result.success:
                            click.echo(f"  ✅ DRM removed: {result.output_file}")
                        else:
                            click.echo(
                                f"  ❌ DRM removal failed: {result.error_message}",
                                err=True,
                            )
            else:
                click.echo("  ❌ Download failed")

            click.echo("")

        click.echo("✨ Import complete!")

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
                "❌ Not authenticated. Run 'bookbot audible auth' first.",
                err=True,
            )
            sys.exit(1)

        library = client.get_library()

        if not library:
            click.echo("No books found in your library")
            return

        # Cache library for import command
        cache_file = Path.home() / ".config" / "bookbot" / ".audible_library_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        import json

        cache_file.write_text(json.dumps(library, indent=2))

        display_count = len(library) if limit is None else min(limit, len(library))

        click.echo(f"📚 Found {len(library)} books in your library\n")

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

        click.echo("\n💡 To import books, use: bookbot audible import 1,2,3")

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

            status_icon = "🔒" if drm_info.is_protected else "✅"
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
                    click.echo("✅ Activation bytes fetched successfully!")
                else:
                    click.echo("⚠️  Could not automatically fetch activation bytes.")
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
                click.echo(f"✅ {file_path.name}: No DRM protection")
                success_count += 1
                continue

            if dry_run:
                click.echo(
                    f"🔒 {file_path.name}: Would remove {drm_info.drm_type.value} DRM"
                )
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
