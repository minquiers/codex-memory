from __future__ import annotations

import argparse
import platform
import zipfile
from pathlib import Path

from app_metadata import APP_EXECUTABLE_NAME, APP_VERSION


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
        description="Package built dist output into a zip archive.",
    )
    parser.add_argument(
        "--name",
        default=APP_EXECUTABLE_NAME,
        help=f"Application executable name. Default: {APP_EXECUTABLE_NAME}",
    )
    parser.add_argument(
        "--version",
        default=APP_VERSION,
        help=f"Application version. Default: {APP_VERSION}",
    )
    parser.add_argument(
        "--dist-root",
        default="dist",
        help="Dist root directory. Default: dist",
    )
    parser.add_argument(
        "--release-root",
        default="release",
        help="Release output root directory. Default: release",
    )
    parser.add_argument(
        "--platform",
        default="",
        help="Optional target platform override. Default is current host platform.",
    )
    return parser


def resolve_root() -> Path:
    return Path(__file__).resolve().parent


def resolve_platform(value: str) -> str:
    if value.strip():
        return value.strip().lower()
    return detect_platform()


def locate_dist_target(dist_dir: Path, app_name: str) -> Path:
    preferred_targets = [
        dist_dir / f"{app_name}.app",
        dist_dir / f"{app_name}.exe",
        dist_dir / app_name,
    ]
    for candidate in preferred_targets:
        if candidate.exists():
            return candidate

    children = sorted(path for path in dist_dir.iterdir() if path.name != ".DS_Store")
    if not children:
        raise FileNotFoundError(f"No build output found in: {dist_dir}")
    return children[0]


def create_zip_archive(source_path: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zip_handle:
        if source_path.is_file():
            zip_handle.write(source_path, arcname=source_path.name)
            return

        base_parent = source_path.parent
        for child in source_path.rglob("*"):
            if child.is_dir():
                continue
            zip_handle.write(child, arcname=str(child.relative_to(base_parent)))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root_dir = resolve_root()
    target_platform = resolve_platform(args.platform)

    dist_dir = (root_dir / args.dist_root / target_platform).resolve()
    if not dist_dir.exists():
        raise FileNotFoundError(f"Dist directory does not exist: {dist_dir}")

    source_path = locate_dist_target(dist_dir, args.name)
    release_dir = (root_dir / args.release_root / target_platform).resolve()
    archive_name = f"{args.name}-{args.version}-{target_platform}.zip"
    archive_path = release_dir / archive_name
    create_zip_archive(source_path, archive_path)

    print(f"Packaged: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
