"""Lightweight TOML reader/writer used when the external package is unavailable."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml_reader  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - fallback for older interpreters
    _toml_reader = None  # type: ignore[assignment]


class TomlDecodeError(ValueError):
    """Mirror the exception type from the external toml package."""


def load(fp: Any) -> dict[str, Any]:
    """Load TOML data from a file-like object."""
    data = fp.read()
    return loads(data)


def loads(data: str | bytes) -> dict[str, Any]:
    """Load TOML data from a string."""
    if isinstance(data, bytes):
        text = data.decode("utf-8")
    else:
        text = data

    if _toml_reader is not None:
        try:
            result: dict[str, Any] = _toml_reader.loads(text)
            return result
        except (ValueError, AttributeError) as exc:  # pragma: no cover - delegated errors
            raise TomlDecodeError(str(exc)) from exc

    # As a last resort parse via json for extremely simple configs
    try:
        json_result: dict[str, Any] = json.loads(text)
        return json_result
    except json.JSONDecodeError as exc:  # pragma: no cover - alternate parsing path
        raise TomlDecodeError(str(exc)) from exc


def dump(data: Mapping[str, Any], fp: Any) -> None:
    """Serialize TOML data to a file-like object."""
    fp.write(dumps(data))


def dumps(data: Mapping[str, Any]) -> str:
    """Serialize a mapping into a minimal TOML string."""
    lines: list[str] = []

    def write_section(prefix: list[str], section: Mapping[str, Any]) -> None:
        scalar_items: list[tuple[str, Any]] = []
        nested_items: list[tuple[str, Mapping[str, Any]]] = []

        for key, value in section.items():
            if value is None:
                continue
            if isinstance(value, Mapping):
                nested_items.append((key, value))
            else:
                scalar_items.append((key, value))

        for key, value in scalar_items:
            lines.append(f"{key} = {format_value(value)}")

        for key, value in nested_items:
            lines.append("")
            header = ".".join(prefix + [key]) if prefix else key
            lines.append(f"[{header}]")
            write_section(prefix + [key], value)

    write_section([], data)

    rendered = "\n".join(lines).strip()
    return rendered + ("\n" if rendered else "")


def format_value(value: Any) -> str:
    """Format supported TOML literals."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):  # floats used for durations etc.
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_value(item) for item in value) + "]"
    # Fallback to JSON for unknown types (e.g., enums converted to str)
    return json.dumps(str(value))
