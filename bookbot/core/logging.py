"""Production structured logging system with rotation."""

import json
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class StructuredLogger:
    """Production structured logger with rotation."""

    def __init__(self, name: str, log_dir: Path, level: int = logging.INFO):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.handlers.clear()  # Prevent duplicate handlers

        # Ensure log directory exists
        log_dir.mkdir(parents=True, exist_ok=True)

        # Rotating file handler (10MB, 7 backups = ~7 days)
        log_file = log_dir / f"{name}.jsonl"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(self._json_formatter())
        self.logger.addHandler(file_handler)

        # Console for errors only
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.ERROR)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        self.logger.addHandler(console)

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
        self.logger.info(msg, extra={"extra_data": kwargs})

    def error(self, msg: str, exc_info: bool = False, **kwargs: Any) -> None:
        self.logger.error(msg, exc_info=exc_info, extra={"extra_data": kwargs})

    def warning(self, msg: str, **kwargs: Any) -> None:
        self.logger.warning(msg, extra={"extra_data": kwargs})

    def debug(self, msg: str, **kwargs: Any) -> None:
        self.logger.debug(msg, extra={"extra_data": kwargs})


_loggers: dict[str, StructuredLogger] = {}


def get_logger(name: str, log_dir: Path | None = None) -> StructuredLogger:
    """Get or create logger."""
    if name not in _loggers:
        if log_dir is None:
            log_dir = Path.home() / ".local" / "share" / "bookbot" / "logs"
        _loggers[name] = StructuredLogger(name, log_dir)
    return _loggers[name]
