"""Configuration management."""

import os
from pathlib import Path

import toml
from pydantic import ValidationError

from .models import Config, Profile


class ConfigManager:
    """Manages configuration files and profiles."""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = config_dir or self._get_default_config_dir()
        self.config_file = self.config_dir / "config.toml"
        self.profiles_dir = self.config_dir / "profiles"

        # Ensure directories exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        # Create default profiles if they don't exist
        self.create_default_profiles()

        self._config: Config | None = None

    @staticmethod
    def _get_default_config_dir() -> Path:
        """Get the default configuration directory for the current platform."""
        if os.name == "nt":  # Windows
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif os.environ.get("XDG_CONFIG_HOME"):  # Linux/Unix with XDG
            base = Path(os.environ["XDG_CONFIG_HOME"])
        else:  # macOS and other Unix
            base = Path.home() / ".config"

        return base / "bookbot"

    def load_config(self) -> Config:
        """Load configuration from file or create default."""
        if self._config is not None:
            return self._config

        if self.config_file.exists():
            try:
                with open(self.config_file, encoding="utf-8") as f:
                    config_data = toml.load(f)
                self._config = Config(**config_data)
            except (OSError, ValidationError, toml.TomlDecodeError) as e:
                # Fall back to default config on error
                print(f"Warning: Error loading config file: {e}")
                self._config = Config()
                self.save_config()  # Save default config
        else:
            self._config = Config()
            self.save_config()  # Create default config file

        return self._config

    def save_config(self, config: Config | None = None) -> None:
        """Save configuration to file."""
        if config is not None:
            self._config = config
        elif self._config is None:
            raise ValueError("No config to save")

        config_dict = self._config.model_dump(exclude_none=True, mode="json")

        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                toml.dump(config_dict, f)
        except OSError as e:
            raise RuntimeError(f"Failed to save config: {e}") from e

    def list_profiles(self) -> dict[str, str]:
        """List available profiles with their descriptions."""
        profiles = {}

        for profile_file in self.profiles_dir.glob("*.toml"):
            try:
                with open(profile_file, encoding="utf-8") as f:
                    profile_data = toml.load(f)
                    profile = Profile(**profile_data)
                    profiles[profile.name] = profile.description
            except (OSError, ValidationError, toml.TomlDecodeError):
                # Skip invalid profiles
                continue

        return profiles

    def load_profile(self, name: str) -> Profile | None:
        """Load a specific profile."""
        profile_file = self.profiles_dir / f"{name}.toml"

        if not profile_file.exists():
            return None

        try:
            with open(profile_file, encoding="utf-8") as f:
                profile_data = toml.load(f)
                return Profile(**profile_data)
        except (OSError, ValidationError, toml.TomlDecodeError):
            return None

    def save_profile(self, profile: Profile) -> None:
        """Save a profile."""
        profile_file = self.profiles_dir / f"{profile.name}.toml"
        profile_dict = profile.model_dump(exclude_none=True)

        try:
            with open(profile_file, "w", encoding="utf-8") as f:
                toml.dump(profile_dict, f)
        except OSError as e:
            raise RuntimeError(f"Failed to save profile: {e}") from e

    def delete_profile(self, name: str) -> bool:
        """Delete a profile."""
        profile_file = self.profiles_dir / f"{name}.toml"

        if not profile_file.exists():
            return False

        try:
            profile_file.unlink()
            return True
        except OSError:
            return False

    def apply_profile(self, name: str) -> bool:
        """Apply a profile to the current configuration."""
        profile = self.load_profile(name)
        if profile is None:
            return False

        # Update use count and last used
        profile.use_count += 1
        from datetime import datetime

        profile.last_used = datetime.now().isoformat()

        # Apply the profile's config
        self._config = profile.config
        self.save_config()
        self.save_profile(profile)

        return True

    def create_default_profiles(self) -> None:
        """Create default configuration profiles."""
        default_profiles = [
            Profile(
                name="safe",
                description="Safe mode - rename only, no tagging",
                config=Config(
                    safe_mode=True,
                    tagging=Config().tagging.model_copy(update={"enabled": False}),
                    conversion=Config().conversion.model_copy(
                        update={"enabled": False}
                    ),
                ),
            ),
            Profile(
                name="full",
                description="Full processing - rename, retag, and artwork",
                config=Config(
                    safe_mode=False,
                    tagging=Config().tagging.model_copy(
                        update={
                            "enabled": True,
                            "write_cover_art": True,
                            "overwrite_policy": "fill_missing",
                        }
                    ),
                ),
            ),
            Profile(
                name="plex",
                description="Plex Media Server optimized",
                config=Config(
                    active_template="plex",
                    safe_mode=False,
                    tagging=Config().tagging.model_copy(
                        update={
                            "enabled": True,
                            "write_cover_art": True,
                            "write_series": True,
                        }
                    ),
                ),
            ),
            Profile(
                name="conversion",
                description="Enable M4B conversion",
                config=Config(
                    safe_mode=False,
                    conversion=Config().conversion.model_copy(
                        update={
                            "enabled": True,
                            "bitrate": "128k",
                            "create_chapters": True,
                            "normalize_audio": False,
                        }
                    ),
                ),
            ),
        ]

        for profile in default_profiles:
            if not (self.profiles_dir / f"{profile.name}.toml").exists():
                self.save_profile(profile)

    def reset_to_defaults(self) -> None:
        """Reset configuration to defaults."""
        self._config = Config()
        self.save_config()

    def get_cache_dir(self) -> Path:
        """Get the cache directory, creating it if necessary."""
        cache_dir = self.load_config().cache_directory
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def get_log_dir(self) -> Path:
        """Get the log directory, creating it if necessary."""
        log_dir = self.load_config().log_directory
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
