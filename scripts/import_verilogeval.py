#!/usr/bin/env python3
"""Import a pinned, clean VerilogEval checkout without changing its source.

Import: python scripts/import_verilogeval.py --source third_party/verilog-eval \
            --output data/verilogeval --split-seed 20261007 --dev-fraction 0.8
Verify: python scripts/import_verilogeval.py --verify data/verilogeval

Task JSON contains only the original prompt and evaluation metadata. Reference
RTL and testbenches live separately under evaluation/. The verifier checks every
exported file against provenance.json; that file should itself be retained in a
trusted version/freeze manifest. Hash consistency alone is not authentication.
No HDL compilation, simulation, or synthesis is performed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent.task_metadata import detect_clock_port


PINNED_REVISION = "c498220d0a52248f8e3fdffe279075215bde2da6"
REPOSITORY_URL = "https://github.com/NVlabs/verilog-eval"
SUBSET = "dataset_spec-to-rtl"
SUFFIXES = ("_prompt.txt", "_ref.sv", "_test.sv")


def _sha256(content):
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _safe_path(path):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"symlink is not allowed: {component}")
    return path.resolve()


def _read_regular(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"not a regular file: {path}")
        return stream.read()


def _files(root):
    """Collect regular files, rejecting links including links to directories."""
    result = {}

    def fail(error):
        raise error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=fail):
        folder = Path(current)
        for name in directories + filenames:
            if (folder / name).is_symlink():
                raise ValueError(f"symlink is not allowed: {folder / name}")
        directories.sort()
        for name in sorted(filenames):
            path = folder / name
            result[path.relative_to(root).as_posix()] = _read_regular(path)
    return dict(sorted(result.items()))


def _git(source, *arguments):
    result = subprocess.run(["git", "-C", str(source), *arguments], check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30)
    if result.returncode:
        raise ValueError("git check failed: " + result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8")


def _check_git(source, expected_revision):
    root = Path(_git(source, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != source:
        raise ValueError("--source must be the Git repository root")
    revision = _git(source, "rev-parse", "HEAD").strip()
    if revision != expected_revision:
        raise ValueError(f"revision mismatch: expected {expected_revision}, found {revision}")
    # All tracked edits are rejected. Untracked/ignored dataset or LICENSE
    # additions are also rejected, even if Git would normally hide them.
    tracked = _git(source, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    dataset = _git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                   "--ignored=matching", "--", SUBSET, "LICENSE")
    if tracked or dataset:
        raise ValueError("dirty source: tracked changes or untracked/ignored dataset files are not allowed")
    return revision


def _split(task_ids, seed, fraction):
    if type(seed) is not int:
        raise ValueError("split seed must be an integer")
    if not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 < fraction < 1:
        raise ValueError("dev fraction must be finite and strictly between 0 and 1")
    ordered = sorted(task_ids, key=lambda value: (_sha256(f"{seed}\n{value}".encode("utf-8")), value))
    count = int(len(ordered) * fraction)
    count = max(1, min(len(ordered) - 1, count)) if len(ordered) >= 2 else len(ordered)
    return ordered[:count], ordered[count:]


def import_dataset(source, output, split_seed=20261007, dev_fraction=0.8,
                   *, expected_revision=PINNED_REVISION):
    """Import once; expected_revision is injectable only for fixture tests."""
    source, output = _safe_path(source), _safe_path(output)
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    if output == source or source in output.parents:
        raise ValueError("output must be outside the source repository")
    dataset = _safe_path(source / SUBSET)
    license_path = _safe_path(source / "LICENSE")
    if not dataset.is_dir() or not license_path.is_file():
        raise ValueError("source must contain dataset_spec-to-rtl and LICENSE")
    raw = _files(dataset)
    license_bytes = _read_regular(license_path)
    revision = _check_git(source, expected_revision)
    groups = [{name[:-len(suffix)] for name in raw if name.endswith(suffix)} for suffix in SUFFIXES]
    if not groups[0] or not (groups[0] == groups[1] == groups[2]):
        raise ValueError("incomplete dataset: every prompt needs matching _ref.sv and _test.sv files")
    task_ids = sorted(groups[0])
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", task_id) for task_id in task_ids):
        raise ValueError("unsupported task ID or nested task path")
    dev, holdout = _split(task_ids, split_seed, dev_fraction)
    source_hashes = {f"{SUBSET}/{name}": _sha256(content) for name, content in raw.items()}
    source_hashes["LICENSE"] = _sha256(license_bytes)
    source_hashes = dict(sorted(source_hashes.items()))
    exported = {"LICENSE": license_bytes}
    warnings = []
    for task_id in task_ids:
        prompt_bytes = raw[task_id + "_prompt.txt"]
        prompt = prompt_bytes.decode("utf-8")
        # Read the interface bullet, never infer clocks from the reference RTL.
        clock_port = detect_clock_port(prompt)
        if clock_port is None:
            warnings.append({"task_id": task_id, "code": "clock_port_undetermined",
                             "message": "No unique scalar clock input found; clock_port is null. Review the prompt before real synthesis."})
        reference = f"evaluation/{task_id}/{task_id}_ref.sv"
        testbench = f"evaluation/{task_id}/{task_id}_test.sv"
        task = {"task_id": task_id, "prompt": prompt, "top": "TopModule",
                "clock_port": clock_port, "testbench_files": ["../" + reference, "../" + testbench],
                "testbench_top": "tb", "simulation_protocol": "verilogeval_v2",
                "dataset_revision": revision,
                "source_sha256": {f"{SUBSET}/{task_id}{suffix}": source_hashes[f"{SUBSET}/{task_id}{suffix}"]
                                  for suffix in SUFFIXES}}
        exported[f"tasks/{task_id}.json"] = _json_bytes(task)
        exported[f"prompts/{task_id}_prompt.txt"] = prompt_bytes
        exported[reference] = raw[task_id + "_ref.sv"]
        exported[testbench] = raw[task_id + "_test.sv"]
    identity = {"name": "VerilogEval v2", "repository_url": REPOSITORY_URL,
                "revision": revision, "subset": SUBSET}
    for name, ids in (("manifest", task_ids), ("dev", dev), ("holdout", holdout)):
        exported[name + ".json"] = _json_bytes({"schema_version": 1, "dataset": identity,
                                                 "provenance": "provenance.json",
                                                 "split": "all" if name == "manifest" else name,
                                                 "tasks": [f"tasks/{task_id}.json" for task_id in ids]})
    provenance = {"schema_version": 1, "dataset": identity, "source_clean": True,
                  "source_files_sha256": source_hashes,
                  "source_content_sha256": _sha256(_json_bytes(source_hashes)),
                  "source_hash_serialization": "UTF-8 JSON, ensure_ascii=False, sort_keys=True, indent=2, trailing newline",
                  "split": {"algorithm": "ascending sha256(UTF-8(str(seed) + newline + task_id)); floor(n*fraction), clamped to [1,n-1] when n>=2",
                            "seed": split_seed, "dev_fraction": dev_fraction,
                            "counts": {"all": len(task_ids), "dev": len(dev), "holdout": len(holdout)}},
                  "warnings": warnings,
                  "files_sha256": {name: _sha256(content) for name, content in sorted(exported.items())},
                  "verification": "python scripts/import_verilogeval.py --verify <output-directory>; provenance.json is excluded from its own hash map and must be externally frozen",
                  "validation_status": "imported_only_no_EDA_validation"}
    # Catch source changes during the import before creating any destination.
    if _files(dataset) != raw or _read_regular(license_path) != license_bytes:
        raise ValueError("source changed during import")
    _check_git(source, expected_revision)
    output.mkdir(parents=True, exist_ok=False)
    try:
        for name, content in sorted(exported.items()):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (output / "provenance.json").write_bytes(_json_bytes(provenance))
        verify_dataset(output)
    except Exception:
        shutil.rmtree(output)
        raise
    return provenance


def verify_dataset(output):
    """Re-read exported bytes and check inventory, hashes, and split coverage."""
    output = _safe_path(output)
    if not output.is_dir():
        raise ValueError("verification directory does not exist")
    files = _files(output)
    if "provenance.json" not in files:
        raise ValueError("missing provenance.json")
    provenance = json.loads(files.pop("provenance.json"))
    hashes = provenance.get("files_sha256")
    if provenance.get("schema_version") != 1 or not isinstance(hashes, dict):
        raise ValueError("unsupported provenance schema")
    for name, digest in hashes.items():
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                or path.as_posix() != name or not re.fullmatch(r"[0-9a-f]{64}", str(digest))):
            raise ValueError("unsafe path or invalid SHA-256 in provenance")
    if set(files) != set(hashes):
        raise ValueError("exported file inventory mismatch")
    if any(_sha256(content) != hashes[name] for name, content in files.items()):
        raise ValueError("exported file SHA-256 mismatch")
    source_hashes = provenance.get("source_files_sha256")
    if not isinstance(source_hashes, dict) or _sha256(_json_bytes(source_hashes)) != provenance.get("source_content_sha256"):
        raise ValueError("source content SHA-256 mismatch")
    lists = {name: json.loads(files[name + ".json"])["tasks"] for name in ("manifest", "dev", "holdout")}
    if any(len(items) != len(set(items)) for items in lists.values()):
        raise ValueError("duplicate tasks in manifest")
    all_tasks, dev, holdout = (set(lists[name]) for name in ("manifest", "dev", "holdout"))
    if dev & holdout or dev | holdout != all_tasks or any(task not in files for task in all_tasks):
        raise ValueError("split coverage or task path mismatch")
    counts = {"all": len(all_tasks), "dev": len(dev), "holdout": len(holdout)}
    if counts != provenance["split"]["counts"]:
        raise ValueError("split count mismatch")
    return {"ok": True, "counts": counts, "dataset_revision": provenance["dataset"]["revision"],
            "validation_status": "hashes_verified_no_EDA_validation"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--split-seed", type=int, default=20261007)
    parser.add_argument("--dev-fraction", type=float, default=0.8)
    parser.add_argument("--verify", type=Path, metavar="DIRECTORY")
    args = parser.parse_args(argv)
    if args.verify:
        if args.source or args.output:
            parser.error("--verify cannot be combined with --source or --output")
    elif not args.source or not args.output:
        parser.error("--source and --output are required for import")
    try:
        if args.verify:
            result = verify_dataset(args.verify)
        else:
            provenance = import_dataset(args.source, args.output, args.split_seed, args.dev_fraction)
            result = {"ok": True, "output": str(args.output), "counts": provenance["split"]["counts"],
                      "warnings": len(provenance["warnings"]), "dataset_revision": provenance["dataset"]["revision"],
                      "validation_status": provenance["validation_status"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f"import/verify failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
