#!/usr/bin/env python3
"""Fail closed when a public source tree or release artifact contains private material.

This checker is intentionally dependency-free so every release builder can run it
before and after packaging.  It is a boundary guard, not a secret-management
system: commercial implementations and production credentials still belong in a
separate private repository and secret store.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator


FORBIDDEN_MODULE_NAMES = frozenset(
    {"private", "enterprise", "commercial_private", "commercial-private"}
)
# These modules currently contain the commercial decision/evidence/runtime
# implementation.  Until they are moved behind the private service boundary,
# a public build must fail instead of silently publishing them under ordinary
# filenames.
FORBIDDEN_COMMERCIAL_FILES = frozenset(
    {
        "bridge/chengfang_autopilot.py",
        "bridge/chengfang_autopilot_runtime.py",
        "bridge/chengfang_evidence.py",
        "bridge/chengfang_official_contract.py",
        "bridge/chengfang_official_adapter.py",
        "bridge/chengfang_production_controller.py",
        "bridge/chengfang_production_targets.py",
        "bridge/test_chengfang_autopilot.py",
        "bridge/test_chengfang_autopilot_runtime.py",
        "bridge/test_chengfang_evidence.py",
        "bridge/test_chengfang_official_contract.py",
        "bridge/test_chengfang_official_adapter.py",
        "bridge/test_chengfang_production_controller.py",
        "bridge/test_chengfang_production_http.py",
        "bridge/test_chengfang_production_targets.py",
    }
)
FORBIDDEN_KEY_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".license"}
)
FORBIDDEN_KEY_NAMES = frozenset(
    {".license", "id_rsa", "id_ed25519", "credentials.json", "service-account.json"}
)
FORBIDDEN_ARTIFACT_NAMES = frozenset({"internal_commercial_build.json"})
ALLOWED_ENV_EXAMPLES = frozenset({".env.marketplace.example"})
SOURCE_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".idea",
        ".vscode",
        ".venv",
        ".venv-macos",
        "__pycache__",
        "backup",
        "data",
        "dist",
        "logs",
        "node_modules",
        "reports",
        "venv",
    }
)
SOURCE_EXCLUDED_PATHS = frozenset({"bridge/knowledge"})
TEXT_SUFFIXES = frozenset(
    {
        "",
        ".cjs",
        ".command",
        ".css",
        ".html",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vbs",
        ".yaml",
        ".yml",
    }
)
MAX_TEXT_BYTES = 2 * 1024 * 1024

_PRIVATE_IMPORT_PATTERNS = (
    re.compile(
        r"(?m)^\s*(?:from\s+\.*(?:private|enterprise|commercial_private)"
        r"(?:\.[A-Za-z_][\w]*)*\s+import\b|import\s+\.*"
        r"(?:private|enterprise|commercial_private)(?:\.[A-Za-z_][\w]*)*)"
    ),
    re.compile(
        r"(?m)^\s*(?:import\s+[^;\n]+\s+from\s+|(?:const|let|var)\s+[^=\n]+="
        r"\s*require\s*\()\s*['\"](?:\.{0,2}/)*(?:private|enterprise|"
        r"commercial[_-]private)(?:/|['\"])"
    ),
    re.compile(
        r"(?m)^\s*import\s*\(\s*['\"](?:\.{0,2}/)*(?:private|enterprise|"
        r"commercial[_-]private)(?:/|['\"])"
    ),
    re.compile(
        r"(?m)^\s*import\s*['\"](?:\.{0,2}/)*(?:private|enterprise|"
        r"commercial[_-]private)(?:/|['\"])"
    ),
    re.compile(
        r"(?m)^\s*[^\n]*\brequire\s*\(\s*['\"](?:\.{0,2}/)*(?:private|"
        r"enterprise|commercial[_-]private)(?:/|['\"])"
    ),
    re.compile(
        r"(?m)^\s*[^\n]*\bimport_module\s*\(\s*['\"](?:private|enterprise|"
        r"commercial_private)(?:\.|['\"])"
    ),
)
_PRIVATE_KEY_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "-----BEGIN EC " + "PRIVATE KEY-----",
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
)


@dataclass(frozen=True)
class Finding:
    location: str
    reason: str


def _normalized_parts(name: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(name.replace("\\", "/")).parts)


def _path_reason(name: str) -> str | None:
    parts = _normalized_parts(name)
    if not parts:
        return None
    normalized_path = "/".join(parts)
    if normalized_path in FORBIDDEN_COMMERCIAL_FILES:
        return "commercial implementation must move to the private repository"
    for part in parts:
        stem = PurePosixPath(part).stem
        if part in FORBIDDEN_MODULE_NAMES or stem in FORBIDDEN_MODULE_NAMES:
            return f"private capability path segment: {part}"
    basename = parts[-1]
    if basename in FORBIDDEN_ARTIFACT_NAMES:
        return "internal commercial build marker"
    if basename == ".env" or basename.startswith(".env."):
        if basename not in ALLOWED_ENV_EXAMPLES:
            return "environment/secret file"
    suffix = PurePosixPath(basename).suffix
    if suffix in FORBIDDEN_KEY_SUFFIXES or basename in FORBIDDEN_KEY_NAMES:
        return "credential or private-key file"
    return None


def _content_reason(text: str) -> str | None:
    if any(marker in text for marker in _PRIVATE_KEY_MARKERS):
        return "embedded private-key material"
    if any(pattern.search(text) for pattern in _PRIVATE_IMPORT_PATTERNS):
        return "reference to a private capability module"
    return None


def _is_text_candidate(name: str, size: int) -> bool:
    return size <= MAX_TEXT_BYTES and PurePosixPath(name).suffix.casefold() in TEXT_SUFFIXES


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _iter_directory_files(root: Path, *, source: bool) -> Iterator[tuple[Path, str]]:
    def walk(directory: Path) -> Iterator[tuple[Path, str]]:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise RuntimeError(f"cannot read {directory}: {exc}") from exc
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                yield entry, relative
                continue
            if entry.is_dir():
                if source and (
                    entry.name.casefold() in SOURCE_EXCLUDED_DIRS
                    or relative.casefold() in SOURCE_EXCLUDED_PATHS
                ):
                    continue
                yield from walk(entry)
            elif entry.is_file():
                yield entry, relative

    yield from walk(root)


def scan_directory(root: Path, *, source: bool) -> list[Finding]:
    findings: list[Finding] = []
    for path, relative in _iter_directory_files(root, source=source):
        path_problem = _path_reason(relative)
        if path_problem:
            findings.append(Finding(relative, path_problem))
            continue
        if path.is_symlink():
            findings.append(Finding(relative, "symbolic links are not allowed in public artifacts"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            findings.append(Finding(relative, f"cannot inspect file: {exc}"))
            continue
        if not _is_text_candidate(relative, size):
            continue
        try:
            text = _decode_text(path.read_bytes())
        except OSError as exc:
            findings.append(Finding(relative, f"cannot read file: {exc}"))
            continue
        if text is not None:
            content_problem = _content_reason(text)
            if content_problem:
                findings.append(Finding(relative, content_problem))
    return findings


def scan_zip(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename
                path_problem = _path_reason(name)
                if path_problem:
                    findings.append(Finding(name, path_problem))
                    continue
                # Unix symlink entries keep their file type in the high mode bits.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    findings.append(Finding(name, "symbolic links are not allowed in public artifacts"))
                    continue
                if info.is_dir() or not _is_text_candidate(name, info.file_size):
                    continue
                try:
                    text = _decode_text(archive.read(info))
                except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    findings.append(Finding(name, f"cannot inspect ZIP member: {exc}"))
                    continue
                if text is not None:
                    content_problem = _content_reason(text)
                    if content_problem:
                        findings.append(Finding(name, content_problem))
    except (OSError, zipfile.BadZipFile) as exc:
        return [Finding(str(path), f"invalid or unreadable ZIP: {exc}")]
    return findings


def scan_target(path: Path, *, source: bool = False) -> list[Finding]:
    if not path.exists():
        return [Finding(str(path), "scan target does not exist")]
    if path.is_dir():
        return scan_directory(path, source=source)
    path_problem = _path_reason(path.name)
    if path_problem:
        return [Finding(path.name, path_problem)]
    if zipfile.is_zipfile(path):
        return scan_zip(path)
    if source:
        return [Finding(str(path), "source target must be a directory")]
    return []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject private modules, credentials, and secret files from public releases."
    )
    parser.add_argument("--source", type=Path, help="public repository root to scan")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        type=Path,
        help="staging directory or ZIP to scan; may be repeated",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.source is None and not args.artifact:
        _parser().error("at least one --source or --artifact is required")

    findings: list[Finding] = []
    targets: list[tuple[str, Path, bool]] = []
    if args.source is not None:
        targets.append(("source", args.source.resolve(), True))
    targets.extend(("artifact", item.resolve(), False) for item in args.artifact)
    for label, target, source in targets:
        for finding in scan_target(target, source=source):
            findings.append(Finding(f"{label}:{finding.location}", finding.reason))

    if findings:
        print("PUBLIC RELEASE CHECK FAILED", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.location}: {finding.reason}", file=sys.stderr)
        print(
            "Move commercial code to the private repository and credentials to the secret store.",
            file=sys.stderr,
        )
        return 1

    checked = ", ".join(f"{label}={target}" for label, target, _ in targets)
    print(f"Public release boundary check passed: {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
