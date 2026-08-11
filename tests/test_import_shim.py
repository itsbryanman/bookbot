"""Regression tests for repository-parent imports."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


def test_toml_resolves_to_pypi_package() -> None:
    """The `toml` import must resolve to the installed PyPI package."""
    spec = importlib.util.find_spec("toml")
    assert spec is not None
    assert spec.origin is not None
    # The installed package lives inside site-packages, never in the repo root
    assert "site-packages" in spec.origin, (
        f"toml resolved to {spec.origin!r} instead of the installed package"
    )


def test_cli_imports_from_repository_parent() -> None:
    """`bookbot.cli` should resolve to the inner package from the repo parent."""
    repo_root = Path(__file__).resolve().parents[1]
    repo_parent = Path(__file__).resolve().parents[2]
    expected_paths = {
        (repo_root / "cli.py").resolve(),
        (repo_root / "bookbot" / "cli.py").resolve(),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util;"
                " spec = importlib.util.find_spec('bookbot.cli');"
                " print(spec.origin if spec else 'missing')"
            ),
        ],
        capture_output=True,
        check=False,
        cwd=repo_parent,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() in expected_paths
