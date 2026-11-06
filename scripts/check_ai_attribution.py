#!/usr/bin/env python3
"""Fail if commits in a range carry Claude or Copilot attribution.

GitHub lists a commit's author, committer and Co-authored-by trailers as
repository contributors. This check runs in CI on every pull request and
push, so commits made outside a clone with the local commit-msg hook (the
Copilot coding agent, web edits, other machines) are caught too.

Usage: check_ai_attribution.py <rev-range>     e.g. origin/main..HEAD
Exit status: 0 clean, 1 offending commits found, 2 usage/git error.
"""

from __future__ import annotations

import re
import subprocess
import sys

AI_IDENTITY = re.compile(r"(?i)(anthropic|claude|copilot)")
AI_TRAILER = re.compile(r"(?im)^co-authored-by:.*(anthropic|claude|copilot)")
AI_FOOTER = re.compile(
    r"(?im)^\s*(?:\N{ROBOT FACE}\s*)?generated with "
    r"(?:claude code|\[claude code\]\([^)]*\))\s*$"
)

_FIELD = "\x1f"
_RECORD = "\x1e"


def offending_commits(rev_range: str) -> list[str]:
    output = subprocess.run(
        [
            "git",
            "log",
            f"--format=%H{_FIELD}%an <%ae>{_FIELD}%cn <%ce>{_FIELD}%B{_RECORD}",
            rev_range,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    problems: list[str] = []
    for record in output.split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, committer, message = record.split(_FIELD, 3)
        reasons = []
        if AI_IDENTITY.search(author):
            reasons.append(f"author {author}")
        if AI_IDENTITY.search(committer):
            reasons.append(f"committer {committer}")
        reasons += [
            f"trailer '{m.group(0).strip()}'" for m in AI_TRAILER.finditer(message)
        ]
        reasons += [
            f"footer '{m.group(0).strip()}'" for m in AI_FOOTER.finditer(message)
        ]
        if reasons:
            problems.append(f"{sha[:12]}: " + "; ".join(reasons))
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        problems = offending_commits(argv[1])
    except subprocess.CalledProcessError as exc:
        print(f"git log failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    if not problems:
        print(f"No Claude/Copilot attribution in {argv[1]}.")
        return 0
    print("Commits with Claude/Copilot attribution (GitHub would list these")
    print("accounts as contributors). Rewrite them before merging:")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
