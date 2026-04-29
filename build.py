from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path

from app_metadata import (
    APP_BUNDLE_ID,
    APP_EXECUTABLE_NAME,
    APP_VERSION,
    DEFAULT_ICON_PATHS,
)

ENTRY_FILE = "main.py"
COLLECT_ALL_PACKAGES = (
    "customtkinter",
    "git",
    "gitdb",
    "smmap",
)
HIDDEN_IMPORTS = (
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
)


def detect_platform() -> str:
    system_name = platform.system()
    if system_name == "Darwin":
        return "macos"
    if system_name == "Windows":
        return "windows"
    if system_name == "Linux":
        return "linux"
    raise RuntimeError(f"Unsupported host platform: {system_name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Codex Memory Sync executable with PyInstaller.",
    )
    parser.add_argument(
        "--name",
        default=APP_EXECUTABLE_NAME,
        help=f"Application name. Default: {APP_EXECUTABLE_NAME}",
    )
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Build a one-file executable. Default is onedir/app bundle.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove previous build/dist folders for the current platform before building.",
    )
    parser.add_argument(
        "--icon",
        default="",
        help="Optional icon file path (.icns on macOS, .ico on Windows).",
    )
    parser.add_argument(
        "--bundle-id",
        default=APP_BUNDLE_ID,
        help="Bundle identifier used on macOS.",
    )
    parser.add_argument(
        "--dist-root",
        default="dist",
        help="Root output directory. Default: dist",
    )
    parser.add_argument(
        "--work-root",
        default="build",
        help="Root temporary build directory. Default: build",
    )
    parser.add_argument(
        "--spec-root",
        default="build/spec",
        help="Directory used by PyInstaller for generated spec files. Default: build/spec",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the PyInstaller arguments without running the build.",
    )
    return parser


def resolve_root() -> Path:
    return Path(__file__).resolve().parent


def ensure_entry_file(root_dir: Path) -> Path:
    entry_path = root_dir / ENTRY_FILE
    if not entry_path.exists():
        raise FileNotFoundError(f"Entry file not found: {entry_path}")
    return entry_path


def normalize_optional_path(root_dir: Path, path_value: str) -> str:
    if not path_value.strip():
        return ""

    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = root_dir / candidate
    candidate = candidate.resolve()

    if not candidate.exists():
        raise FileNotFoundError(f"Optional file not found: {candidate}")
    return str(candidate)


def resolve_icon_path(args: argparse.Namespace, root_dir: Path, host_platform: str) -> str:
    if args.icon.strip():
        return normalize_optional_path(root_dir, args.icon)

    default_icon = DEFAULT_ICON_PATHS.get(host_platform, "")
    if not default_icon:
        return ""

    candidate = root_dir / default_icon
    if candidate.exists():
        return str(candidate.resolve())
    return ""


def build_pyinstaller_args(args: argparse.Namespace, root_dir: Path, host_platform: str) -> list[str]:
    entry_path = ensure_entry_file(root_dir)
    dist_root = (root_dir / args.dist_root).resolve()
    work_root = (root_dir / args.work_root).resolve()
    spec_root = (root_dir / args.spec_root).resolve()

    dist_path = dist_root / host_platform
    work_path = work_root / host_platform
    spec_path = spec_root / host_platform

    pyinstaller_args = [
        str(entry_path),
        "--noconfirm",
        "--windowed",
        "--name",
        args.name,
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
        "--specpath",
        str(spec_path),
    ]

    if args.onefile:
        pyinstaller_args.append("--onefile")

    icon_path = resolve_icon_path(args, root_dir, host_platform)
    if icon_path:
        pyinstaller_args.extend(["--icon", icon_path])

    if host_platform == "macos":
        pyinstaller_args.extend(["--osx-bundle-identifier", args.bundle_id])

    for package_name in COLLECT_ALL_PACKAGES:
        pyinstaller_args.extend(["--collect-all", package_name])

    for import_name in HIDDEN_IMPORTS:
        pyinstaller_args.extend(["--hidden-import", import_name])

    return pyinstaller_args


def clean_previous_outputs(root_dir: Path, args: argparse.Namespace, host_platform: str) -> None:
    dist_path = (root_dir / args.dist_root / host_platform).resolve()
    work_path = (root_dir / args.work_root / host_platform).resolve()
    spec_path = (root_dir / args.spec_root / host_platform).resolve()

    for path in (dist_path, work_path, spec_path):
        if path.exists():
            shutil.rmtree(path)


def run_build(pyinstaller_args: list[str]) -> None:
    try:
        import PyInstaller.__main__
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyInstaller is not installed. Run `pip install -r requirements-build.txt` first."
        ) from exc

    PyInstaller.__main__.run(pyinstaller_args)


def print_build_summary(args: argparse.Namespace, host_platform: str, pyinstaller_args: list[str]) -> None:
    print(f"Host platform : {host_platform}")
    print(f"App name      : {args.name}")
    print(f"App version   : {APP_VERSION}")
    print(f"Build mode    : {'onefile' if args.onefile else 'onedir'}")
    print("PyInstaller args:")
    for item in pyinstaller_args:
        print(f"  {item}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root_dir = resolve_root()
    host_platform = detect_platform()

    if args.clean:
        clean_previous_outputs(root_dir, args, host_platform)

    pyinstaller_args = build_pyinstaller_args(args, root_dir, host_platform)
    print_build_summary(args, host_platform, pyinstaller_args)

    if args.dry_run:
        return 0

    run_build(pyinstaller_args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
