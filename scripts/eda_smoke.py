#!/usr/bin/env python3
"""Archive one real EDA smoke or an existing model run in a new portable bundle."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import eda
from agent.core import load_task
from scripts.doctor import collect_checks
from scripts.freeze import create_manifest

TRACK = "rtl"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def portable(value, mappings):
    if isinstance(value, dict):
        return {portable(k, mappings): portable(v, mappings) for k, v in value.items()}
    if isinstance(value, list):
        return [portable(v, mappings) for v in value]
    if isinstance(value, str):
        for old, new in mappings:
            if value == old or value.startswith(old + os.sep) or value.startswith(old + "/"):
                return new + value[len(old):].replace("\\", "/")
            # Windows may report the same directory through an 8.3 alias or
            # different casing/separators. Only map paths inside this root.
            if Path(value).is_absolute() and Path(old).is_absolute():
                try:
                    relative = Path(value).resolve().relative_to(Path(old).resolve())
                except (OSError, ValueError):
                    continue
                return new if not relative.parts else new + "/" + relative.as_posix()
    return value



def missing_references(value, out):
    missing = []
    def walk(item, key=""):
        if isinstance(item, dict):
            for k, v in item.items():
                walk(v, k)
        elif isinstance(item, list):
            for v in item:
                walk(v, key)
        elif isinstance(item, str) and key in ("log", "work_dir", "code_path", "testbench_files", "support_files", "testbench_data_files"):
            if Path(item).is_absolute() or PureWindowsPath(item).is_absolute() or not (out / item).exists():
                missing.append({"field": key, "reference": item})
    walk(value)
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--purpose", choices=("final", "experiment"), required=True)
    parser.add_argument("--related-run", default="", help="Related bundle ID, never an absolute path")
    parser.add_argument("--archive-run", type=Path, help="Copy an existing model run including all attempts and artifacts; no inference is executed")
    parser.add_argument("--task", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if not args.archive_run and (not args.task or not args.source):
        parser.error("EDA smoke requires --task and --source")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    out = args.output.resolve()
    if args.archive_run and (out == args.archive_run.resolve() or args.archive_run.resolve() in out.parents):
        parser.error("output must be outside the imported run")
    out.mkdir(parents=True, exist_ok=False)
    metadata = {"schema_version": 1, "track": TRACK, "purpose": args.purpose,
                "related_run": args.related_run, "created_utc": datetime.now(timezone.utc).isoformat(),
                "kind": "imported_model_run" if args.archive_run else "handwritten_eda_smoke",
                "environment_snapshot_semantics": "doctor/config describe the archive host; imported original run/config.json describes its execution",
                "source_snapshot_semantics": "Code at archive time; imported runs need their original source manifest to prove execution provenance",
                "model_validation": "not_run" if not args.archive_run else "see imported run provenance",
                "implementation_timing_acceptance": "not_run"}
    exit_code = 1
    try:
        for directory in ("agent", "scripts"):
            shutil.copytree(ROOT / directory, out / "source_snapshot" / directory,
                            ignore=shutil.ignore_patterns("__pycache__"))
        save(out / "doctor.json", collect_checks(out))
        save(out / "config.json", {"required_tool_version": eda.required_tool_version(),
             "part": eda.PART, "period_ns": eda.PERIOD_NS, "timeout_s": args.timeout,
             "environment": {k: os.environ.get(k) for k in
                 ("HLS_AGENT_VITIS_VERSION", "RTL_AGENT_VIVADO_VERSION", "XILINX_HLS", "XILINX_VITIS", "HLS_AGENT_WORK_ROOT")}})
        if args.archive_run:
            shutil.copytree(args.archive_run, out / "run")
            # Keep original imported JSON intact and provide a portable result view.
            mapping = [(str(args.archive_run.resolve()), "run")]
            result_path = out / "run" / "result.json"
            if not result_path.is_file():
                result_path = out / "run" / "pair.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            view = portable(result, mapping)
            save(out / "result.json", view)
            save(out / "missing_references.json", missing_references(view, out))
            metadata["result_status"] = ({mode: result[mode].get("validation", {}).get("status", "unknown")
                                          for mode in ("baseline", "agent")} if "baseline" in result and "agent" in result
                                         else result.get("validation", result).get("status", "unknown"))
            exit_code = 0  # Archive completion, not model/EDA success.
        else:
            task = load_task(args.task)
            inputs = out / "inputs"
            inputs.mkdir()
            source = inputs / args.source.name
            shutil.copy2(args.source, source)
            task_snapshot = dict(task)
            for field in ("support_files", "testbench_files", "testbench_data_files"):
                names = []
                for item in task.get(field, []):
                    dest = inputs / Path(item).name
                    if dest.exists():
                        raise ValueError("input basename collision: " + dest.name)
                    shutil.copy2(item, dest)
                    names.append(dest.name)
                if field in task:
                    task_snapshot[field] = names
            task_snapshot.pop("asset_sha256", None)
            save(inputs / "task.json", task_snapshot)
            task = load_task(inputs / "task.json")
            validator = eda.validate_hls if TRACK == "hls" else eda.validate_rtl
            result = validator(source, task, out / "eda", args.timeout)
            mappings = [(str(out), ".")]
            external = result.get("work_dir")
            if external and Path(external).is_dir() and not Path(external).resolve().is_relative_to(out):
                shutil.copytree(external, out / "external_eda")
                mappings.insert(0, (external, "external_eda"))
            save(out / "result.json", portable(result, mappings))
            metadata["result_status"] = result["status"]
            exit_code = 0 if result["status"] == "passed" else 1
    except Exception:
        (out / "exception.log").write_text(traceback.format_exc(), encoding="utf-8")
        metadata["result_status"] = "archive_or_execution_error"
    finally:
        save(out / "bundle.json", metadata)
        links = ["# Evidence bundle", "", "Status: " + str(metadata.get("result_status", "unknown")), "",
                 "Purpose: " + args.purpose, "", "Related run: " + args.related_run, ""]
        links += ["- [" + p.relative_to(out).as_posix() + "](" + p.relative_to(out).as_posix() + ")"
                  for p in sorted(out.rglob("*")) if p.is_file() and p.suffix in (".json", ".log", ".tcl", ".rpt", ".dcp")]
        (out / "README.md").write_text("\n".join(links) + "\n", encoding="utf-8")
        create_manifest(out, out / "manifest.json")
    print(str(out))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
