#!/usr/bin/env python3
"""Create and verify a deterministic SHA-256 submission manifest.

Usage: python scripts/freeze.py create ROOT MANIFEST.json
       python scripts/freeze.py verify ROOT MANIFEST.json

Only directories named outputs, .git, __pycache__, or tmp are omitted.
Model files are always included. The manifest itself is omitted when inside ROOT.
Symlinks and special files are rejected instead of followed or silently skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any


EXCLUDED_DIRECTORIES = frozenset({"outputs", ".git", "__pycache__", "tmp"})


def _root_and_manifest(root: str | Path, manifest: str | Path) -> tuple[Path, Path]:
    root_path, manifest_path = Path(root), Path(manifest)
    # Check each existing component before resolving, including parent links.
    for value in (root_path.absolute(), manifest_path.absolute()):
        for component in (value, *value.parents):
            if component.is_symlink():
                raise ValueError(f"symlink is not allowed: {component}")
    root_path, manifest_path = root_path.resolve(), manifest_path.resolve()
    if not root_path.is_dir():
        raise ValueError(f"root is not a directory: {root_path}")
    if manifest_path == root_path:
        raise ValueError("manifest must be a file, not the root directory")
    return root_path, manifest_path


def _sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"not a regular file: {path}")
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def _snapshot(root: Path, manifest: Path) -> dict[str, str]:
    result: dict[str, str] = {}

    def fail_walk(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=fail_walk):
        folder = Path(current)
        for name in directories + filenames:
            if (folder / name).is_symlink():
                raise ValueError(f"symlink is not allowed: {folder / name}")
        directories[:] = sorted(name for name in directories if name not in EXCLUDED_DIRECTORIES)
        for name in sorted(filenames):
            path = folder / name
            if path == manifest:
                continue
            if not path.is_file():
                raise ValueError(f"not a regular file: {path}")
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return dict(sorted(result.items()))


def create_manifest(root: str | Path, manifest: str | Path) -> dict[str, Any]:
    """Hash root and write stable JSON without timestamps or absolute paths."""
    root_path, manifest_path = _root_and_manifest(root, manifest)
    result = {"schema_version": 1, "algorithm": "sha256",
              "excluded_directories": sorted(EXCLUDED_DIRECTORIES),
              "files": _snapshot(root_path, manifest_path)}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    manifest_path.write_text(content, encoding="utf-8")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in manifest: {key}")
        result[key] = value
    return result


def _read_manifest(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or isinstance(data.get("schema_version"), bool) or data.get("algorithm") != "sha256"
            or data.get("excluded_directories") != sorted(EXCLUDED_DIRECTORIES)):
        raise ValueError("unsupported manifest schema, algorithm, or exclusions")
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError("manifest files must be an object")
    for name, digest in files.items():
        if (not name or "\\" in name or "\x00" in name or PurePosixPath(name).is_absolute()
                or any(part in ("", ".", "..") for part in name.split("/"))
                or ":" in name.split("/")[0]):
            raise ValueError(f"unsafe manifest path: {name!r}")
        if any(part in EXCLUDED_DIRECTORIES for part in name.split("/")[:-1]):
            raise ValueError(f"manifest references excluded directory: {name!r}")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"invalid SHA-256 digest for {name!r}")
    return files


def verify_manifest(root: str | Path, manifest: str | Path) -> dict[str, Any]:
    """Return ok/modified/missing/added; reject malformed paths and symlinks."""
    root_path, manifest_path = _root_and_manifest(root, manifest)
    expected = _read_manifest(manifest_path)
    if manifest_path.is_relative_to(root_path) and manifest_path.relative_to(root_path).as_posix() in expected:
        raise ValueError("manifest must not include its own file")
    actual = _snapshot(root_path, manifest_path)
    missing = sorted(expected.keys() - actual.keys())
    added = sorted(actual.keys() - expected.keys())
    modified = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
    return {"ok": not (missing or added or modified), "checked_files": len(actual),
            "missing": missing, "added": added, "modified": modified}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subcommands.add_parser(command)
        subparser.add_argument("root", type=Path)
        subparser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_manifest(args.root, args.manifest)
            result = {"ok": True, "manifest": str(args.manifest), "files": len(manifest["files"])}
        else:
            result = verify_manifest(args.root, args.manifest)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return 0 if result["ok"] else 1
    except (OSError, ValueError) as error:
        sys.stderr.write(f"freeze: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
