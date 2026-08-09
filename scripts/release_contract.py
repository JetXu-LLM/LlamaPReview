"""Shared deterministic release primitives for the three Lambda artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile


RELEASE_SCHEMA = "llamapreview-public-release-v1"
RELEASE_METADATA_FILES = (
    "dependency-licenses.json",
    "release-manifest.json",
    "sbom.cdx.json",
)
CHECKSUM_FILE = "SHA256SUMS"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def checked_zip_entries(
    archive: zipfile.ZipFile, *, required_root: str | None = None
) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    names: set[str] = set()
    for info in entries:
        name = PurePosixPath(info.filename)
        if (
            not name.parts
            or name.is_absolute()
            or ".." in name.parts
            or "" in name.parts
            or info.filename.endswith("/")
        ):
            raise ValueError(f"Unsafe ZIP entry: {info.filename}")
        if required_root and name.parts[0] != required_root:
            raise ValueError(f"ZIP entry is outside {required_root}/: {info.filename}")
        if info.filename in names:
            raise ValueError(f"Duplicate ZIP entry: {info.filename}")
        names.add(info.filename)
        if info.date_time != ZIP_TIMESTAMP:
            raise ValueError(f"Non-deterministic ZIP timestamp: {info.filename}")
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) != stat.S_IFREG or stat.S_IMODE(mode) != 0o644:
            raise ValueError(f"Non-canonical ZIP mode: {info.filename}")
    return entries


def safe_extract(
    archive_path: Path, destination: Path, *, required_root: str | None = None
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        entries = checked_zip_entries(archive, required_root=required_root)
        for info in entries:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
