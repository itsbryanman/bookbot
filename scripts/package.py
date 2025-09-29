#!/usr/bin/env python3
"""Package BookBot for distribution."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def clean_build_dirs(project_root: Path) -> None:
    """Clean previous build directories."""
    build_dirs = ["build", "dist", "*.egg-info"]

    for pattern in build_dirs:
        for path in project_root.glob(pattern):
            if path.is_dir():
                print(f"Removing {path}")
                shutil.rmtree(path)
            elif path.is_file():
                print(f"Removing {path}")
                path.unlink()


def build_wheel(project_root: Path) -> bool:
    """Build wheel package."""
    print("Building wheel package...")

    try:
        subprocess.run([
            sys.executable, "-m", "build", "--wheel", str(project_root)
        ], check=True, capture_output=True, text=True, cwd=project_root)

        print("✓ Wheel built successfully")
        return True

    except subprocess.CalledProcessError as e:
        print("✗ Wheel build failed")
        print(e.stdout)
        print(e.stderr)
        return False


def build_sdist(project_root: Path) -> bool:
    """Build source distribution."""
    print("Building source distribution...")

    try:
        subprocess.run([
            sys.executable, "-m", "build", "--sdist", str(project_root)
        ], check=True, capture_output=True, text=True, cwd=project_root)

        print("✓ Source distribution built successfully")
        return True

    except subprocess.CalledProcessError as e:
        print("✗ Source distribution build failed")
        print(e.stdout)
        print(e.stderr)
        return False


def check_package(project_root: Path) -> bool:
    """Check packages with twine."""
    print("Checking packages...")

    dist_dir = project_root / "dist"
    if not dist_dir.exists() or not list(dist_dir.glob("*")):
        print("✗ No packages found to check")
        return False

    try:
        subprocess.run([
            sys.executable, "-m", "twine", "check", str(dist_dir / "*")
        ], check=True, capture_output=True, text=True, cwd=project_root)

        print("✓ Package check passed")
        return True

    except subprocess.CalledProcessError as e:
        print("✗ Package check failed")
        print(e.stdout)
        print(e.stderr)
        return False


def test_installation(project_root: Path) -> bool:
    """Test installation in a clean environment."""
    print("Testing installation in clean environment...")

    dist_dir = project_root / "dist"
    wheel_files = list(dist_dir.glob("*.whl"))

    if not wheel_files:
        print("✗ No wheel file found for testing")
        return False

    wheel_file = wheel_files[0]

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        venv_path = temp_path / "test_venv"

        try:
            # Create virtual environment
            subprocess.run([
                sys.executable, "-m", "venv", str(venv_path)
            ], check=True, capture_output=True)

            # Determine python executable in venv
            if sys.platform == "win32":
                python_exe = venv_path / "Scripts" / "python.exe"
                pip_exe = venv_path / "Scripts" / "pip.exe"
            else:
                python_exe = venv_path / "bin" / "python"
                pip_exe = venv_path / "bin" / "pip"

            # Install package
            subprocess.run([
                str(pip_exe), "install", str(wheel_file)
            ], check=True, capture_output=True)

            # Test basic functionality
            result = subprocess.run([
                str(python_exe), "-c", "import bookbot; print('Import successful')"
            ], check=True, capture_output=True, text=True)

            # Test CLI
            subprocess.run([
                str(python_exe), "-m", "bookbot.cli", "--help"
            ], check=True, capture_output=True, text=True)

            print("✓ Installation test passed")
            return True

        except subprocess.CalledProcessError as e:
            print("✗ Installation test failed")
            print(e.stdout if e.stdout else "")
            print(e.stderr if e.stderr else "")
            return False


def create_checksums(project_root: Path) -> None:
    """Create checksums for distribution files."""
    import hashlib

    dist_dir = project_root / "dist"
    checksum_file = dist_dir / "checksums.txt"

    print("Creating checksums...")

    with open(checksum_file, 'w') as f:
        for file_path in sorted(dist_dir.glob("*")):
            if file_path.is_file() and file_path.name != "checksums.txt":
                # Calculate SHA256
                sha256_hash = hashlib.sha256()
                with open(file_path, 'rb') as binary_file:
                    for chunk in iter(lambda: binary_file.read(4096), b""):
                        sha256_hash.update(chunk)

                sha256_hex = sha256_hash.hexdigest()
                f.write(f"{sha256_hex}  {file_path.name}\n")
                print(f"  {file_path.name}: {sha256_hex}")

    print(f"✓ Checksums written to {checksum_file}")


def show_package_info(project_root: Path) -> None:
    """Show information about created packages."""
    dist_dir = project_root / "dist"

    print("\nPackage Information:")
    print("=" * 50)

    total_size = 0
    for file_path in sorted(dist_dir.glob("*")):
        if file_path.is_file():
            size = file_path.stat().st_size
            total_size += size
            size_mb = size / (1024 * 1024)
            print(f"  {file_path.name:<30} {size_mb:>8.2f} MB")

    total_mb = total_size / (1024 * 1024)
    print(f"  {'Total:':<30} {total_mb:>8.2f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="Package BookBot for distribution")
    parser.add_argument("--wheel-only", action="store_true",
                       help="Build wheel only (skip source distribution)")
    parser.add_argument("--sdist-only", action="store_true",
                       help="Build source distribution only (skip wheel)")
    parser.add_argument("--skip-test", action="store_true",
                       help="Skip installation testing")
    parser.add_argument("--skip-check", action="store_true",
                       help="Skip package checking with twine")
    parser.add_argument("--no-clean", action="store_true",
                       help="Don't clean build directories first")

    args = parser.parse_args()

    project_root = Path(__file__).parent.parent

    # Check dependencies
    try:
        import importlib.util

        if importlib.util.find_spec("build") is None:
            raise ImportError("build is not installed")
        if importlib.util.find_spec("twine") is None:
            raise ImportError("twine is not installed")
    except ImportError as e:
        print(f"Missing build dependency: {e}")
        print("Install with: pip install build twine")
        return 1

    # Clean build directories
    if not args.no_clean:
        clean_build_dirs(project_root)

    success = True

    # Build packages
    if not args.sdist_only:
        success &= build_wheel(project_root)

    if not args.wheel_only:
        success &= build_sdist(project_root)

    if not success:
        print("Package build failed")
        return 1

    # Check packages
    if not args.skip_check:
        success &= check_package(project_root)

    # Test installation
    if not args.skip_test and not args.sdist_only:
        success &= test_installation(project_root)

    if not success:
        print("Package validation failed")
        return 1

    # Create checksums
    create_checksums(project_root)

    # Show package info
    show_package_info(project_root)

    print("\n✓ Packaging completed successfully!")
    print("\nTo upload to PyPI:")
    print("  python -m twine upload dist/*")
    print("\nTo upload to TestPyPI:")
    print("  python -m twine upload --repository testpypi dist/*")

    return 0


if __name__ == "__main__":
    sys.exit(main())
