"""Regression tests for repository-parent imports."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cli_imports_from_repository_parent() -> None:
    """`bookbot.cli` should resolve to the inner package from the repo parent."""
    repo_parent = Path(__file__).resolve().parents[2]

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
    assert result.stdout.strip().endswith("/bookbot/bookbot/cli.py")
