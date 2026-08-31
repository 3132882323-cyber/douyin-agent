"""Create a deterministic ZIP with executable bits preserved for macOS."""

from __future__ import annotations

import stat
import sys
import zipfile
from pathlib import Path


def create_zip(source: Path, destination: Path) -> None:
    executable_suffixes = {".command", ".sh"}
    files = sorted(path for path in source.rglob("*") if path.is_file())
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = Path(source.name) / path.relative_to(source)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.suffix in executable_suffixes else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: create_unix_zip.py SOURCE DESTINATION")
    create_zip(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
