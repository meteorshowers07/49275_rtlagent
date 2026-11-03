#!/usr/bin/env python3
"""Create a reproducible local-development ZIP from an existing freeze manifest.

Example:
  python scripts/package.py --root . --manifest local_manifest.json \
      --output ../output/rtl_agent_local_v0.1.zip

This verifies the existing manifest; it never creates or updates that manifest.
The output and its .sha256 sidecar must be new paths outside the project root.
ZIP entries use a fixed rtl_agent/ prefix, 1980 timestamps, stored compression,
sorted paths, and Unix permissions. On Windows, .sh files use 0755 and other
files use 0644 because the filesystem cannot express Unix executable bits. The manifest itself
is included. This is a development delivery, not a final competition submission.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile


_SPEC = importlib.util.spec_from_file_location("package_freeze", Path(__file__).with_name("freeze.py"))
freeze = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(freeze)

TOP_DIRECTORY = "rtl_agent"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
CHUNK_SIZE = 1024 * 1024


def _safe_path(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"symlink is not allowed: {component}")
    return path.resolve()


def _entry_name(relative):
    if (not relative or "\\" in relative or "\x00" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or ":" in relative.split("/")[0]
            or any(part in freeze.EXCLUDED_DIRECTORIES for part in relative.split("/"))):
        raise ValueError(f"unsafe or excluded archive path: {relative!r}")
    return TOP_DIRECTORY + "/" + relative


def _open_regular(path):
    _safe_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError(f"not a regular file: {path}")
    return stream


def _hash_stream(stream):
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
        digest.update(block)
    return digest.hexdigest()


def _file_hash(path):
    with _open_regular(path) as stream:
        return _hash_stream(stream)


def verify_archive(archive, expected_hashes, expected_modes=None):
    """Check exact entry inventory, SHA-256 values, and deterministic metadata.

    expected_hashes uses full ZIP entry names, including rtl_agent/. Mode values,
    when supplied, are permission bits as returned by stat.S_IMODE.
    """
    for name, digest in expected_hashes.items():
        if not name.startswith(TOP_DIRECTORY + "/"):
            raise ValueError("archive entry is outside the fixed top-level directory")
        _entry_name(name[len(TOP_DIRECTORY) + 1:])
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid expected SHA-256")
    if expected_modes is not None and set(expected_modes) != set(expected_hashes):
        raise ValueError("expected mode inventory mismatch")
    with zipfile.ZipFile(_safe_path(archive), "r") as zipped:
        entries = zipped.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != set(expected_hashes):
            raise ValueError("archive inventory mismatch")
        if names != sorted(names) or zipped.comment:
            raise ValueError("archive order or comment is not deterministic")
        for entry in entries:
            mode = entry.external_attr >> 16
            if (entry.date_time != FIXED_TIME or entry.create_system != 3
                    or entry.compress_type != zipfile.ZIP_STORED
                    or stat.S_IFMT(mode) != stat.S_IFREG or entry.is_dir()):
                raise ValueError(f"archive metadata mismatch: {entry.filename}")
            if expected_modes is not None and stat.S_IMODE(mode) != expected_modes[entry.filename]:
                raise ValueError(f"archive mode mismatch: {entry.filename}")
            with zipped.open(entry, "r") as stream:
                if _hash_stream(stream) != expected_hashes[entry.filename]:
                    raise ValueError(f"archive SHA-256 mismatch: {entry.filename}")
    return {"ok": True, "files": len(expected_hashes)}


def create_package(root, manifest, output):
    root, manifest, output = (_safe_path(path) for path in (root, manifest, output))
    checksum_path = _safe_path(str(output) + ".sha256")
    if output == root or root in output.parents:
        raise ValueError("output must be outside the project root")
    for target in (output, checksum_path):
        if target.exists():
            raise ValueError(f"output already exists: {target}")
    with _open_regular(manifest) as stream:
        manifest_bytes = stream.read()
    verification = freeze.verify_manifest(root, manifest)
    if not verification["ok"]:
        raise ValueError("source does not match freeze manifest: " + json.dumps(verification, sort_keys=True))
    # freeze validated the schema and safe paths. Compare the bytes again so a
    # manifest changed while verifying cannot silently change the packaged set.
    with _open_regular(manifest) as stream:
        if stream.read() != manifest_bytes:
            raise ValueError("manifest changed during verification")
    manifest_data = json.loads(manifest_bytes)
    expected = {_entry_name(name): digest for name, digest in manifest_data["files"].items()}
    sources = {_entry_name(name): root / name for name in manifest_data["files"]}
    manifest_relative = manifest.relative_to(root).as_posix() if manifest.is_relative_to(root) else manifest.name
    manifest_entry = _entry_name(manifest_relative)
    if manifest_entry in expected:
        raise ValueError("manifest archive path conflicts with a source file")
    expected[manifest_entry] = hashlib.sha256(manifest_bytes).hexdigest()
    sources[manifest_entry] = manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_paths = []
    owned_destinations = []
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temporary_archive = Path(temporary_name)
        temporary_paths.append(temporary_archive)
        modes = {}
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
            for name in sorted(sources):
                with _open_regular(sources[name]) as stream:
                    if os.name == "nt":
                        permission = 0o755 if sources[name].suffix == ".sh" else 0o644
                    else:
                        permission = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
                    modes[name] = permission
                    info = zipfile.ZipInfo(filename=name, date_time=FIXED_TIME)
                    info.create_system = 3
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = (stat.S_IFREG | permission) << 16
                    digest = hashlib.sha256()
                    with zipped.open(info, "w", force_zip64=True) as destination:
                        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
                            digest.update(block)
                            destination.write(block)
                    if digest.hexdigest() != expected[name]:
                        raise ValueError(f"source changed while packaging: {name}")
        verify_archive(temporary_archive, expected, modes)
        # A new or modified source during ZIP creation must not be hidden by
        # the earlier successful freeze verification.
        final_verification = freeze.verify_manifest(root, manifest)
        if not final_verification["ok"] or _file_hash(manifest) != expected[manifest_entry]:
            raise ValueError("source or manifest changed during packaging")
        archive_hash = _file_hash(temporary_archive)
        descriptor, temporary_name = tempfile.mkstemp(prefix="." + checksum_path.name + ".", suffix=".tmp", dir=output.parent)
        temporary_checksum = Path(temporary_name)
        temporary_paths.append(temporary_checksum)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(archive_hash + "  " + output.name + "\n")
        # Reserve both final paths exclusively. os.replace then only replaces
        # this process's placeholders, never a pre-existing delivery.
        for target in (output, checksum_path):
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.close(descriptor)
            owned_destinations.append(target)
        os.replace(temporary_archive, output)
        os.replace(temporary_checksum, checksum_path)
        return {"ok": True, "archive": str(output), "sha256_file": str(checksum_path),
                "archive_sha256": archive_hash, "files": len(expected),
                "source_files": len(expected) - 1, "delivery_kind": "local_development",
                "not_final_submission": True}
    except Exception:
        for path in reversed(owned_destinations):
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = create_package(args.root, args.manifest, args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"package: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
