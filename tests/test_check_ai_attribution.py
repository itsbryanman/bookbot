"""Tests for scripts/check_ai_attribution.py (CI gate on commit metadata)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_ai_attribution.py"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit(
    repo: Path,
    message: str,
    *,
    author: str = "Bryan C <itsbryanman@users.noreply.github.com>",
    committer: str | None = None,
) -> str:
    import os

    name, email = author[: author.index(" <")], author[author.index("<") + 1 : -1]
    c_name, c_email = name, email
    if committer is not None:
        c_name = committer[: committer.index(" <")]
        c_email = committer[committer.index("<") + 1 : -1]
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": c_name,
        "GIT_COMMITTER_EMAIL": c_email,
    }
    (repo / "f.txt").write_text(message)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "--no-verify", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    _commit(tmp_path, "base")
    return tmp_path


def _check(repo: Path, rev_range: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), rev_range],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_clean_commits_pass(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "Fix\n\nBody that mentions Claude Code and Copilot.")
    _commit(
        repo,
        "Web merge",
        committer="GitHub <noreply@github.com>",
    )
    _commit(repo, "Pair\n\nCo-authored-by: Jane Doe <jane@example.com>")
    result = _check(repo, f"{base}..HEAD")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("message", "author", "committer"),
    [
        ("x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", None, None),
        (
            "x\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
            None,
            None,
        ),
        (
            "x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)",
            None,
            None,
        ),
        ("x\n\nGenerated with Claude Code", None, None),
        (
            "x",
            "copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>",
            None,
        ),
        ("x", "Claude <noreply@anthropic.com>", None),
        ("x", None, "Copilot <198982749+Copilot@users.noreply.github.com>"),
    ],
    ids=[
        "claude-trailer",
        "copilot-trailer",
        "footer-link",
        "footer-plain",
        "copilot-author",
        "claude-author",
        "copilot-committer",
    ],
)
def test_ai_attribution_fails(
    repo: Path, message: str, author: str | None, committer: str | None
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    kwargs = {}
    if author:
        kwargs["author"] = author
    sha = _commit(repo, message, committer=committer, **kwargs)
    result = _check(repo, f"{base}..HEAD")
    assert result.returncode == 1
    assert sha[:12] in result.stdout


def test_only_the_given_range_is_checked(repo: Path) -> None:
    _commit(repo, "old\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "new clean commit")
    assert _check(repo, f"{base}..HEAD").returncode == 0
