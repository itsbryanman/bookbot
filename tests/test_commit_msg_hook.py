"""Tests for scripts/hooks/commit-msg."""

import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "commit-msg"


def _run(tmp_path: Path, message: str) -> str:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message, encoding="utf-8")
    subprocess.run([sys.executable, str(HOOK), str(msg)], check=True)
    return msg.read_text(encoding="utf-8")


def test_strips_claude_and_copilot_trailers(tmp_path: Path) -> None:
    message = (
        "Fix the thing\n"
        "\n"
        "Body text mentioning Claude stays.\n"
        "\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
        "\n"
        "Co-Authored-By: Claude Opus 4 <noreply@anthropic.com>\n"
        "co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>\n"
        "Co-authored-by: Jane Doe <jane@example.com>\n"
    )
    out = _run(tmp_path, message)
    assert "Claude Opus" not in out
    assert "Copilot" not in out
    assert "Generated with" not in out
    assert "Co-authored-by: Jane Doe <jane@example.com>" in out
    assert "Body text mentioning Claude stays." in out


def test_clean_message_is_unchanged(tmp_path: Path) -> None:
    message = "Subject\n\nBody\n"
    assert _run(tmp_path, message) == message


def test_trailing_blank_lines_collapsed(tmp_path: Path) -> None:
    out = _run(tmp_path, "Subject\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
    assert out == "Subject\n"


def test_prose_mentioning_claude_code_survives(tmp_path: Path) -> None:
    prose = [
        "- the hook drops the Claude Code footer, i.e. lines generated with",
        "Docs generated with Claude Code were reviewed by hand.",
        "  🤖 Generated with Claude Code is the footer we remove.",
        "Mentions Copilot and Claude in passing.",
    ]
    message = "Subject\n\n" + "\n".join(prose) + "\n"
    assert _run(tmp_path, message) == message


def test_only_marker_lines_are_removed(tmp_path: Path) -> None:
    message = (
        "Subject\n\nBody.\n\n"
        "Generated with Claude Code\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
        "Co-authored-by: GitHub Copilot <copilot@github.com>\n"
    )
    assert _run(tmp_path, message) == "Subject\n\nBody.\n"
