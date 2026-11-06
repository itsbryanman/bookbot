#!/usr/bin/env python3
"""Fail if commits in a range carry Claude or Copilot attribution.

GitHub lists a commit's author, committer and Co-authored-by trailers as
repository contributors. This check runs in CI on every pull request and
push, so commits made outside a clone with the local commit-msg hook (the
Copilot coding agent, web edits, other machines) are caught too.

Usage: check_ai_attribution.py <rev-range>     e.g. origin/main..HEAD
       check_ai_attribution.py --from-ci-env   range from the CI event:
         EVENT=pull_request: PR_BASE..PR_HEAD
         EVENT=push: BEFORE..AFTER; if BEFORE is all zeros or unknown
         (new branch, force-push), origin/DEFAULT_BRANCH..AFTER, or just
         the AFTER commit when it is already on the default branch.
Exit status: 0 clean, 1 offending commits found, 2 usage/git error.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping

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


def _git_ok(*args: str) -> bool:
    return (
        subprocess.run(["git", *args], capture_output=True, check=False).returncode == 0
    )


def range_from_ci_env(env: Mapping[str, str]) -> str:
    """The commit range a CI event introduced."""
    if env.get("EVENT") == "pull_request":
        return f"{env['PR_BASE']}..{env['PR_HEAD']}"
    after = env["AFTER"]
    before = env.get("BEFORE", "")
    if before.strip("0") and _git_ok("cat-file", "-e", f"{before}^{{commit}}"):
        return f"{before}..{after}"
    # New branch, or a force-push whose old tip is not in the checkout.
    default = f"origin/{env.get('DEFAULT_BRANCH') or 'main'}"
    if _git_ok("rev-parse", "--verify", "--quiet", default) and not _git_ok(
        "merge-base", "--is-ancestor", after, default
    ):
        return f"{default}..{after}"
    return f"{after}^!"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        rev_range = (
            range_from_ci_env(os.environ) if argv[1] == "--from-ci-env" else argv[1]
        )
    except KeyError as exc:
        print(f"Missing environment variable {exc}", file=sys.stderr)
        return 2
    argv = [argv[0], rev_range]
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
