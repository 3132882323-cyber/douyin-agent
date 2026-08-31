#!/usr/bin/env python3
"""Prepare an internal-only source tree with commercial build metadata."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from check_public_release import (
    FORBIDDEN_COMMERCIAL_FILES,
    SOURCE_EXCLUDED_DIRS,
    SOURCE_EXCLUDED_PATHS,
    _PRIVATE_KEY_MARKERS,
    scan_target,
)
from prepare_public_source import find_unsafe_source_entry


PRIVATE_FLAVOR = '''"""Generated internal build-edition metadata.  Do not redistribute."""

BUILD_FLAVOR = "private_commercial"
COMMERCIAL_MODULES_INCLUDED = True
REDISTRIBUTABLE = False


def build_edition_status() -> dict[str, object]:
    return {
        "edition": "commercial",
        "build_flavor": BUILD_FLAVOR,
        "commercial_modules_included": COMMERCIAL_MODULES_INCLUDED,
        "redistributable": REDISTRIBUTABLE,
    }
'''


def prepare_private_source(source: Path, destination: Path) -> None:
    source_input = Path(os.path.abspath(source))
    destination_input = Path(os.path.abspath(destination))
    if not source_input.is_dir():
        raise RuntimeError(f"source directory does not exist: {source_input}")
    if os.path.lexists(destination_input):
        raise RuntimeError(f"destination already exists: {destination_input}")

    unsafe_entry = find_unsafe_source_entry(source_input)
    if unsafe_entry is not None:
        raise RuntimeError(
            f"internal source contains a symbolic link or reparse point: {unsafe_entry}"
        )

    source = source_input.resolve()
    destination = destination_input.resolve()
    if destination == source or source in destination.parents:
        raise RuntimeError("destination must be outside the source checkout")

    findings = scan_target(source, source=True)
    unexpected = [
        item
        for item in findings
        if item.location.replace("\\", "/").casefold() not in FORBIDDEN_COMMERCIAL_FILES
    ]
    if unexpected:
        details = "\n".join(f"- {item.location}: {item.reason}" for item in unexpected)
        raise RuntimeError(f"internal source contains unexpected secret material:\n{details}")

    missing = [name for name in FORBIDDEN_COMMERCIAL_FILES if not (source / name).is_file()]
    # Test modules are useful but not required to compile the runtime.
    runtime_missing = [name for name in missing if "/test_" not in name]
    if runtime_missing:
        raise RuntimeError(
            "required internal modules are missing: " + ", ".join(sorted(runtime_missing))
        )
    for name in FORBIDDEN_COMMERCIAL_FILES:
        path = source / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        if any(marker in text for marker in _PRIVATE_KEY_MARKERS):
            raise RuntimeError(f"embedded private-key material is forbidden: {name}")

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        relative_parent = current.resolve().relative_to(source).as_posix()
        ignored: set[str] = set()
        for name in names:
            path = current / name
            relative = name if relative_parent == "." else f"{relative_parent}/{name}"
            if path.is_dir() and (
                name.casefold() in SOURCE_EXCLUDED_DIRS
                or relative.casefold() in SOURCE_EXCLUDED_PATHS
            ):
                ignored.add(name)
        return ignored

    try:
        # Never follow a link introduced after the preflight.  A preserved link
        # is detected by the post-copy check and the incomplete private stage is
        # removed before the caller can build from it.
        shutil.copytree(
            source,
            destination,
            ignore=ignore,
            copy_function=shutil.copy2,
            symlinks=True,
        )
        staged_unsafe = find_unsafe_source_entry(destination)
        if staged_unsafe is not None:
            raise RuntimeError(
                f"prepared internal source contains a symbolic link or reparse point: {staged_unsafe}"
            )
        (destination / "bridge" / "build_flavor.py").write_text(
            PRIVATE_FLAVOR, encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an internal commercial source tree.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare_private_source(args.source, args.destination)
    except RuntimeError as exc:
        print(f"PRIVATE LOCAL SOURCE PREPARATION FAILED\n{exc}", file=sys.stderr)
        return 1
    print(f"Prepared internal-only source: {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
