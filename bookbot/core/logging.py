"""Production structured logging system with rotation."""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.manager import get_runtime_config_dir

_warned_log_paths: set[str] = set()


class StructuredLogger:
    """Production structured logger with rotation."""

    def __init__(
        self, name: str, log_dir: Path | None = None, level: int = logging.INFO
    ):
        self.name = name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.handlers.clear()  # Prevent duplicate handlers
        self._preferred_log_dir = log_dir
        self._active_log_dir: Path | None = None
        self._file_handler: logging.Handler | None = None
        self._stderr_only = False

        # Console for errors only
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.ERROR)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        self.logger.addHandler(console)

    def configure(self, log_dir: Path | None) -> None:
        """Update the preferred log directory for future writes."""
        if log_dir is None:
            return

        preferred = Path(log_dir)
        if self._preferred_log_dir == preferred:
            return

        self._preferred_log_dir = preferred
        self._clear_file_handler()
        self._stderr_only = False

    def _resolve_log_dir(self) -> Path:
        """Resolve the effective log directory for the current process state."""
        if self._preferred_log_dir is not None:
            return Path(self._preferred_log_dir)
        if os.environ.get("BOOKBOT_CONFIG_DIR"):
            return get_runtime_config_dir() / "logs"
        return Path.home() / ".local" / "share" / "bookbot" / "logs"

    def _clear_file_handler(self) -> None:
        """Detach any existing file handler so the logger can be reconfigured."""
        if self._file_handler is not None:
            self.logger.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None
        self._active_log_dir = None

    def _ensure_file_handler(self) -> None:
        """Create a file handler on first use once the effective config is known."""
        log_dir = self._resolve_log_dir()

        if self._file_handler is not None and self._active_log_dir == log_dir:
            return
        if self._stderr_only and self._active_log_dir == log_dir:
            return

        if self._active_log_dir != log_dir:
            self._clear_file_handler()
            self._stderr_only = False

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{self.name}.jsonl"
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=7,
                encoding="utf-8",
            )
            file_handler.setFormatter(self._json_formatter())
            self._file_handler = file_handler
            self._active_log_dir = log_dir
            self._stderr_only = False
            self.logger.addHandler(file_handler)
        except OSError:
            self._active_log_dir = log_dir
            self._stderr_only = True
            warning = (
                f"Warning: log directory {log_dir} is not writable; "
                "falling back to stderr-only logging."
            )
            if warning not in _warned_log_paths:
                _warned_log_paths.add(warning)
                print(warning, file=sys.stderr)

    def _json_formatter(self) -> logging.Formatter:
        class JSONFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                data: dict[str, Any] = {
                    "ts": datetime.utcnow().isoformat(),
                    "level": record.levelname,
                    "msg": record.getMessage(),
                    "module": record.module,
                    "func": record.funcName,
                    "line": record.lineno,
                }
                if hasattr(record, "extra_data"):
                    data.update(record.extra_data)
                if record.exc_info:
                    data["exc"] = self.formatException(record.exc_info)
                return json.dumps(data)

        return JSONFormatter()

    def info(self, msg: str, **kwargs: Any) -> None:
        self._ensure_file_handler()
        self.logger.info(msg, extra={"extra_data": kwargs})

    def error(self, msg: str, exc_info: bool = False, **kwargs: Any) -> None:
        self._ensure_file_handler()
        self.logger.error(msg, exc_info=exc_info, extra={"extra_data": kwargs})

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._ensure_file_handler()
        self.logger.warning(msg, extra={"extra_data": kwargs})

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._ensure_file_handler()
        self.logger.debug(msg, extra={"extra_data": kwargs})


_loggers: dict[str, StructuredLogger] = {}


def get_logger(name: str, log_dir: Path | None = None) -> StructuredLogger:
    """Get or create logger."""
    if name not in _loggers:
        _loggers[name] = StructuredLogger(name, log_dir)
    elif log_dir is not None:
        _loggers[name].configure(log_dir)
    return _loggers[name]
