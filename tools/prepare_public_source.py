#!/usr/bin/env python3
"""Create a public build tree without weakening the release boundary.

The developer checkout may contain a fixed set of ignored, local-only Chengfang
modules.  Public builders must never compile from that checkout directly.  This
tool first scans the complete checkout, rejects every unexpected finding, then
copies it while omitting only the explicitly registered commercial files.  The
result is scanned again before it can be used as build input.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

from check_public_release import (
    FORBIDDEN_COMMERCIAL_FILES,
    SOURCE_EXCLUDED_DIRS,
    SOURCE_EXCLUDED_PATHS,
    Finding,
    scan_target,
)


class PublicSourceError(RuntimeError):
    """Raised when a checkout cannot be safely reduced to public source."""


def _normalized(relative: str) -> str:
    return relative.replace("\\", "/").strip("/").casefold()


def _allowed_local_finding(finding: Finding) -> bool:
    return (
        _normalized(finding.location) in FORBIDDEN_COMMERCIAL_FILES
        and finding.reason == "commercial implementation must move to the private repository"
    )


def find_unsafe_source_entry(source: Path) -> str | None:
    """Return the first included symlink/reparse point without following it.

    ``check_public_release`` rejects ordinary symlinks, but a Windows junction
    can otherwise be traversed while the source tree is being scanned or
    copied.  Walking with ``follow_symlinks=False`` keeps the public/private
    build boundary inside the selected checkout even when the checkout is
    damaged or was prepared by an untrusted tool.
    """

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    source = Path(os.path.abspath(source))
    try:
        root_metadata = os.lstat(source)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect source directory {source}: {exc}") from exc
    root_attributes = int(getattr(root_metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(root_metadata.st_mode) or bool(root_attributes & reparse_flag):
        return "."
    source = source.resolve()

    def walk(directory: Path, relative_parent: str) -> str | None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            raise RuntimeError(f"cannot inspect source directory {directory}: {exc}") from exc
        for entry in entries:
            relative = entry.name if not relative_parent else f"{relative_parent}/{entry.name}"
            normalized = _normalized(relative)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"cannot inspect source entry {relative}: {exc}") from exc
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            is_reparse = bool(attributes & reparse_flag)
            is_link = stat.S_ISLNK(metadata.st_mode) or is_reparse
            is_directory = stat.S_ISDIR(metadata.st_mode)
            if (is_directory or is_reparse) and (
                entry.name.casefold() in SOURCE_EXCLUDED_DIRS
                or normalized in SOURCE_EXCLUDED_PATHS
            ):
                continue
            if is_link:
                return relative
            if is_directory:
                unsafe = walk(Path(entry.path), relative)
                if unsafe is not None:
                    return unsafe
        return None

    return walk(source, "")


def prepare_public_source(source: Path, destination: Path) -> list[str]:
    source_input = Path(os.path.abspath(source))
    destination_input = Path(os.path.abspath(destination))
    if not source_input.is_dir():
        raise PublicSourceError(f"source directory does not exist: {source_input}")
    if os.path.lexists(destination_input):
        raise PublicSourceError(f"destination already exists: {destination_input}")

    try:
        unsafe_entry = find_unsafe_source_entry(source_input)
    except RuntimeError as exc:
        raise PublicSourceError(str(exc)) from exc
    if unsafe_entry is not None:
        raise PublicSourceError(
            f"source contains an included symbolic link or reparse point: {unsafe_entry}"
        )

    source = source_input.resolve()
    destination = destination_input.resolve()
    if destination == source or source in destination.parents:
        raise PublicSourceError("destination must be outside the source checkout")

    findings = scan_target(source, source=True)
    unexpected = [item for item in findings if not _allowed_local_finding(item)]
    if unexpected:
        details = "\n".join(f"- {item.location}: {item.reason}" for item in unexpected)
        raise PublicSourceError(f"source contains unexpected private material:\n{details}")

    excluded: list[str] = []

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        relative_parent = current.resolve().relative_to(source).as_posix()
        ignored: set[str] = set()
        for name in names:
            relative = name if relative_parent == "." else f"{relative_parent}/{name}"
            normalized = _normalized(relative)
            path = current / name
            if path.is_dir() and name.casefold() in SOURCE_EXCLUDED_DIRS:
                ignored.add(name)
                continue
            if path.is_dir() and normalized in SOURCE_EXCLUDED_PATHS:
                ignored.add(name)
                continue
            if normalized in FORBIDDEN_COMMERCIAL_FILES:
                ignored.add(name)
                excluded.append(normalized)
        return ignored

    # Preserve rather than follow a link introduced after the preflight.  The
    # staged scan below will then reject it instead of copying outside content.
    try:
        shutil.copytree(
            source,
            destination,
            ignore=ignore,
            copy_function=shutil.copy2,
            symlinks=True,
        )
        staged_findings = scan_target(destination, source=True)
        if staged_findings:
            details = "\n".join(
                f"- {item.location}: {item.reason}" for item in staged_findings
            )
            raise PublicSourceError(
                f"prepared public source failed verification:\n{details}"
            )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return sorted(set(excluded))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare a verified public-only source tree.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        excluded = prepare_public_source(args.source, args.destination)
    except PublicSourceError as exc:
        print(f"PUBLIC SOURCE PREPARATION FAILED\n{exc}", file=sys.stderr)
        return 1
    print(
        f"Prepared verified public source: {args.destination.resolve()} "
        f"(excluded {len(excluded)} registered local commercial files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
