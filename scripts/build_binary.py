#!/usr/bin/env python3
"""Build single-file binary distributions of BookBot using PyInstaller."""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def get_platform_info():
    """Get platform-specific information."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        ext = ".exe"
        target_name = "windows"
    elif system == "darwin":
        ext = ""
        target_name = "macos"
    else:
        ext = ""
        target_name = "linux"

    # Determine architecture
    if machine in ["x86_64", "amd64"]:
        arch = "x64"
    elif machine in ["aarch64", "arm64"]:
        arch = "arm64"
    else:
        arch = machine

    return target_name, arch, ext


def build_binary(target=None, output_dir=None):
    """Build binary using PyInstaller."""
    project_root = Path(__file__).parent.parent
    dist_dir = output_dir or project_root / "dist"

    # Get platform info
    platform_name, arch, ext = get_platform_info()
    if target:
        platform_name = target

    # Binary name
    binary_name = f"bookbot-{platform_name}-{arch}{ext}"

    print(f"Building BookBot binary for {platform_name}-{arch}")
    print(f"Output: {dist_dir / binary_name}")

    # PyInstaller spec file content
    spec_content = f'''# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

# Get the project root
project_root = Path("{project_root}").resolve()

a = Analysis(
    [str(project_root / "bookbot" / "cli.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "bookbot" / "config" / "*.toml"), "bookbot/config"),
        (str(project_root / "LICENSE"), "."),
        (str(project_root / "README.md"), "."),
    ],
    hiddenimports=[
        "bookbot.drm",
        "bookbot.providers",
        "bookbot.tui",
        "bookbot.convert",
        "textual",
        "click",
        "pydantic",
        "mutagen",
        "requests",
        "aiohttp",
        "rapidfuzz",
        "toml",
        "rich",
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "numpy",
        "pandas",
        "jupyter",
        "notebook",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="{binary_name}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
'''

    # Write spec file
    spec_file = project_root / f"bookbot-{platform_name}.spec"
    with open(spec_file, 'w') as f:
        f.write(spec_content)

    try:
        # Run PyInstaller
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath", str(dist_dir),
            str(spec_file)
        ]

        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("PyInstaller completed successfully")

        # Verify the binary was created
        binary_path = dist_dir / binary_name
        if binary_path.exists():
            size_mb = binary_path.stat().st_size / (1024 * 1024)
            print(f"Binary created: {binary_path} ({size_mb:.1f} MB)")

            # Test the binary
            print("Testing binary...")
            test_result = subprocess.run([str(binary_path), "--version"],
                                       capture_output=True, text=True, timeout=10)
            if test_result.returncode == 0:
                print("✓ Binary test successful")
                print(f"Version output: {test_result.stdout.strip()}")
            else:
                print("✗ Binary test failed")
                print(f"Error: {test_result.stderr}")
        else:
            print(f"✗ Binary not found at {binary_path}")
            return False

    except subprocess.CalledProcessError as e:
        print(f"PyInstaller failed: {e}")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("Binary test timed out")
        return False
    finally:
        # Clean up spec file
        if spec_file.exists():
            spec_file.unlink()

        # Clean up build directory
        build_dir = project_root / "build"
        if build_dir.exists():
            shutil.rmtree(build_dir)

    return True


def create_installer(target=None, output_dir=None):
    """Create platform-specific installer."""
    project_root = Path(__file__).parent.parent
    platform_name, arch, ext = get_platform_info()
    if target:
        platform_name = target

    if platform_name == "windows":
        return create_windows_installer(output_dir)
    elif platform_name == "macos":
        return create_macos_app(output_dir)
    else:
        # For Linux, we'll create a tarball
        return create_linux_package(output_dir)


def create_windows_installer(output_dir=None):
    """Create Windows installer using NSIS (if available)."""
    print("Windows installer creation not implemented yet")
    return True


def create_macos_app(output_dir=None):
    """Create macOS .app bundle."""
    print("macOS app bundle creation not implemented yet")
    return True


def create_linux_package(output_dir=None):
    """Create Linux package (AppImage or tarball)."""
    print("Linux package creation not implemented yet")
    return True


def main():
    parser = argparse.ArgumentParser(description="Build BookBot binary distributions")
    parser.add_argument("--target", choices=["linux", "windows", "macos"],
                       help="Target platform (auto-detected if not specified)")
    parser.add_argument("--output-dir", type=Path,
                       help="Output directory for built files")
    parser.add_argument("--installer", action="store_true",
                       help="Create platform-specific installer")
    parser.add_argument("--all", action="store_true",
                       help="Build for all supported platforms (requires cross-compilation)")

    args = parser.parse_args()

    if args.all:
        print("Cross-platform building not yet implemented")
        return 1

    success = build_binary(args.target, args.output_dir)
    if not success:
        return 1

    if args.installer:
        success = create_installer(args.target, args.output_dir)
        if not success:
            return 1

    print("Build completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())