#!/usr/bin/env python3
"""Release automation script for BookBot."""

import argparse
import re
import subprocess
import sys
from pathlib import Path


def get_current_version() -> str:
    """Get the current version from pyproject.toml."""
    project_root = Path(__file__).parent.parent
    pyproject_path = project_root / "pyproject.toml"

    with open(pyproject_path) as f:
        content = f.read()

    version_match = re.search(r'version = "([^"]+)"', content)
    if not version_match:
        raise ValueError("Could not find version in pyproject.toml")

    return version_match.group(1)


def bump_version(current_version: str, bump_type: str) -> str:
    """Bump version according to semantic versioning."""
    major, minor, patch = map(int, current_version.split('.'))

    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    else:
        raise ValueError(f"Invalid bump type: {bump_type}")

    return f"{major}.{minor}.{patch}"


def update_version_in_file(file_path: Path, old_version: str, new_version: str) -> None:
    """Update version in a file."""
    with open(file_path) as f:
        content = f.read()

    # Replace version in pyproject.toml
    if file_path.name == "pyproject.toml":
        content = re.sub(
            r'version = "[^"]+"',
            f'version = "{new_version}"',
            content
        )
    # Replace version in __init__.py
    elif file_path.name == "__init__.py":
        content = re.sub(
            r'__version__ = "[^"]+"',
            f'__version__ = "{new_version}"',
            content
        )

    with open(file_path, 'w') as f:
        f.write(content)

    print(f"Updated {file_path}: {old_version} -> {new_version}")


def run_tests() -> bool:
    """Run the test suite."""
    print("Running tests...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v"],
            check=True,
            capture_output=True,
            text=True
        )
        print("✓ Tests passed")
        return True
    except subprocess.CalledProcessError as e:
        print("✗ Tests failed")
        print(e.stdout)
        print(e.stderr)
        return False


def run_linting() -> bool:
    """Run linting checks."""
    print("Running linting checks...")
    try:
        # Run ruff
        subprocess.run([sys.executable, "-m", "ruff", "check", "."], check=True)
        subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", "."], check=True
        )
        print("✓ Linting passed")
        return True
    except subprocess.CalledProcessError:
        print("✗ Linting failed")
        return False


def create_changelog_entry(version: str) -> str:
    """Create a changelog entry for the new version."""
    date = subprocess.check_output(['date', '+%Y-%m-%d']).decode().strip()
    return f"""## [{version}] - {date}

### Added
- New features and enhancements

### Changed
- Improvements and modifications

### Fixed
- Bug fixes and corrections

### Removed
- Deprecated features removed
"""


def update_changelog(version: str) -> None:
    """Update CHANGELOG.md with new version."""
    project_root = Path(__file__).parent.parent
    changelog_path = project_root / "CHANGELOG.md"

    if not changelog_path.exists():
        # Create new changelog
        content = f"""# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

{create_changelog_entry(version)}
"""
    else:
        with open(changelog_path) as f:
            content = f.read()

        # Insert new entry after the header
        lines = content.split('\n')
        header_end = 0
        for i, line in enumerate(lines):
            if line.startswith('## ['):
                header_end = i
                break
        else:
            # No existing entries, find end of header
            for i, line in enumerate(lines):
                if line.strip() == '':
                    header_end = i + 1
                    break

        new_entry = create_changelog_entry(version)
        lines.insert(header_end, new_entry)
        content = '\n'.join(lines)

    with open(changelog_path, 'w') as f:
        f.write(content)

    print(f"Updated CHANGELOG.md with version {version}")


def git_commit_and_tag(version: str) -> None:
    """Commit changes and create git tag."""
    print(f"Creating git commit and tag for version {version}")

    # Add all changes
    subprocess.run(["git", "add", "."], check=True)

    # Commit
    commit_message = f"chore: Release version {version}"
    subprocess.run(["git", "commit", "-m", commit_message], check=True)

    # Create tag
    tag_message = f"Release version {version}"
    subprocess.run(["git", "tag", "-a", f"v{version}", "-m", tag_message], check=True)

    print(f"✓ Created commit and tag v{version}")


def push_release(version: str) -> None:
    """Push release to remote repository."""
    print("Pushing release to remote repository...")

    # Push commits
    subprocess.run(["git", "push", "origin", "main"], check=True)

    # Push tags
    subprocess.run(["git", "push", "origin", f"v{version}"], check=True)

    print("✓ Pushed release to remote repository")


def create_github_release(version: str) -> None:
    """Create GitHub release using gh CLI."""
    print(f"Creating GitHub release for version {version}")

    try:
        # Check if gh CLI is available
        subprocess.run(["gh", "--version"], check=True, capture_output=True)

        # Create release
        release_notes = f"Release BookBot v{version}\n\nSee CHANGELOG.md for details."
        subprocess.run([
            "gh", "release", "create", f"v{version}",
            "--title", f"BookBot v{version}",
            "--notes", release_notes
        ], check=True)

        print(f"✓ Created GitHub release v{version}")

    except subprocess.CalledProcessError:
        print("✗ GitHub CLI not available or failed")
        print("Please create the release manually on GitHub")


def main() -> int:
    parser = argparse.ArgumentParser(description="Release BookBot")
    parser.add_argument("bump_type", choices=["major", "minor", "patch"],
                       help="Type of version bump")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without executing")
    parser.add_argument("--skip-tests", action="store_true",
                       help="Skip running tests")
    parser.add_argument("--skip-push", action="store_true",
                       help="Skip pushing to remote repository")

    args = parser.parse_args()

    project_root = Path(__file__).parent.parent

    try:
        # Get current version
        current_version = get_current_version()
        new_version = bump_version(current_version, args.bump_type)

        print(f"Current version: {current_version}")
        print(f"New version: {new_version}")

        if args.dry_run:
            print("Dry run - no changes will be made")
            return 0

        # Run tests
        if not args.skip_tests:
            if not run_tests():
                print("Tests failed, aborting release")
                return 1

            if not run_linting():
                print("Linting failed, aborting release")
                return 1

        # Update version files
        pyproject_path = project_root / "pyproject.toml"
        init_path = project_root / "bookbot" / "__init__.py"

        update_version_in_file(pyproject_path, current_version, new_version)

        if init_path.exists():
            update_version_in_file(init_path, current_version, new_version)

        # Update changelog
        update_changelog(new_version)

        # Commit and tag
        git_commit_and_tag(new_version)

        # Push to remote
        if not args.skip_push:
            push_release(new_version)
            create_github_release(new_version)

        print(f"✓ Release {new_version} completed successfully!")
        print("\nNext steps:")
        print("1. GitHub Actions will automatically build and publish to PyPI")
        print("2. Binary releases will be attached to the GitHub release")
        print("3. Update the release notes on GitHub if needed")

        return 0

    except Exception as e:
        print(f"Release failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
