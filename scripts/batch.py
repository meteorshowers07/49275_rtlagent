#!/usr/bin/env python3
"""Run each task/seed pair serially and summarize complete batch observations.

Examples:
  python scripts/batch.py --tasks examples/counter.json --output outputs/batch --replay examples/replay.json
  python scripts/batch.py --task-manifest tasks.json --output outputs/real --seeds 0 1 2 3 4

A task manifest is {"tasks": ["relative/to/manifest/task.json", ...]}.
Each subprocess constructs its own client; replay fixtures are restarted for each
pair and remain mock data. Outputs must be new and are never overwritten. --plan-only validates and saves
a launch plan without invoking the model or EDA; execute it in a new output directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.core import digest, load_config, load_task
from agent.client import LocalClient, ReplayClient
from agent.processes import managed_popen, kill_process_tree, release_process_tree
from scripts.metrics import summarize


class BatchInterrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"batch interrupted by signal {signum}")


def task_paths_from_manifest(path: str | Path) -> list[Path]:
    manifest = Path(path).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("track", "rtl") != "rtl":
        raise ValueError("task manifest track must be rtl")
    values = data.get("tasks") if isinstance(data, dict) else None
    if (not isinstance(values, list) or not values
            or any(not isinstance(item, str) or not item.strip() for item in values)):
        raise ValueError("task manifest must contain a nonempty tasks list of JSON paths")
    return [(manifest.parent / item).resolve() for item in values]


def pair_timeout_s(config: dict[str, Any]) -> float:
    """Two generation budgets, one external baseline score, and startup/flush time."""
    return 2 * config["run_timeout_s"] + config["tool_timeout_s"] + 30.0


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _kill_process_group(process: subprocess.Popen) -> None:
    """Let the pair cancel separate EDA sessions, then force its own group down."""
    kill_process_tree(process)


def _run_pair(entry: dict[str, Any], output: Path, timeout_s: float) -> None:
    started = time.monotonic()
    entry["status"] = "running"
    try:
        with (output / entry["stdout_log"]).open("x", encoding="utf-8") as stdout:
            with (output / entry["stderr_log"]).open("x", encoding="utf-8") as stderr:
                try:
                    process = managed_popen(entry["command"], cwd=ROOT, stdout=stdout,
                                               stderr=stderr, stdin=subprocess.DEVNULL,
                                               shell=False, start_new_session=True)
                except OSError as error:
                    entry.update(status="launch_error", error=f"{type(error).__name__}: {error}")
                    stderr.write(entry["error"] + "\n")
                    return
                try:
                    returncode = process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    _kill_process_group(process)
                    entry.update(status="timed_out", returncode=process.returncode,
                                 error=f"pair exceeded {timeout_s:g} seconds; process group killed")
                    stderr.write(entry["error"] + "\n")
                except BaseException:
                    _kill_process_group(process)
                    entry["returncode"] = process.returncode
                    raise
                else:
                    entry.update(status="completed" if returncode == 0 else "failed", returncode=returncode)
                    if returncode:
                        entry["error"] = f"agent.cli pair exited with status {returncode}"
                finally:
                    release_process_tree(process)
    finally:
        entry["wall_s"] = time.monotonic() - started


def _batch_summary(report: dict[str, Any], output: Path) -> dict[str, Any]:
    """Account for every scheduled mode, including children with no result file.

    Only matching task/config/seed/mode slots enter the estimator. A stale or
    changed result cannot silently replace the observation planned by the batch.
    """
    matching = []
    missing = []
    rejected = []
    observed_kinds = set()
    for entry in report["runs"]:
        for mode in ("baseline", "agent"):
            path = output / entry["output"] / mode / "result.json"
            source = str(path.resolve())
            if not path.is_file():
                missing.append({"source": source, "task_id": entry["task_id"],
                                "seed": entry["seed"], "mode": mode,
                                "execution_status": entry["status"]})
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                expected = {"task_id": entry["task_id"], "task_digest": entry["task_digest"],
                            "config_digest": report["config_digest"], "seed": entry["seed"],
                            "mode": mode}
                if not isinstance(record, dict):
                    raise ValueError("result must be an object")
                if any(type(record.get(key)) is not type(value) or record[key] != value
                       for key, value in expected.items()):
                    raise ValueError("result identity differs from its scheduled batch slot")
                if record.get("track", "rtl") != report["track"]:
                    raise ValueError("result track differs from its scheduled batch slot")
                if "inference_source" in report and record.get("inference_source") != report["inference_source"]:
                    raise ValueError("result inference source differs from its batch snapshot")
                kind = record.get("evaluation_kind")
                if isinstance(kind, str) and kind in ("real", "mock"):
                    observed_kinds.add(kind)
                if report.get("requested_evaluation_kind") == "mock" and kind != "mock":
                    raise ValueError("mock batch produced a result claiming real evaluation")
            except (OSError, ValueError) as error:
                rejected.append({"source": source, "reason": "invalid",
                                 "detail": str(error)})
            else:
                matching.append(path)
    summary = summarize(matching, plan=report, plan_root=output)
    summary["input"]["unique_result_files"] += len(rejected)
    summary["input"]["excluded_counts"]["invalid"] += len(rejected)
    summary["input"]["exclusions"] = sorted(
        summary["input"]["exclusions"] + rejected, key=lambda item: item["source"])
    expected_results = 2 * len(report["runs"])
    eligible = summary["input"]["eligible_runs"]
    pairs = summary["paired_comparisons"]["n_pairs"]
    summary["diagnostics"]["batch_slot_rejections_not_in_diagnostics"] = len(rejected)
    summary["coverage"] = {
        "scope": "scheduled batch slots; missing and excluded outcomes are not verified failures",
        "expected_pairs": len(report["runs"]), "expected_results": expected_results,
        "present_results": expected_results - len(missing),
        "missing_results": len(missing), "rejected_slot_results": len(rejected),
        "eligible_results": eligible, "eligible_pairs": pairs,
        "eligible_result_fraction": eligible / expected_results if expected_results else None,
        "full_real_evaluation": bool(expected_results and eligible == expected_results
                                     and pairs == len(report["runs"])),
        "missing": missing,
    }
    report["observed_evaluation_kinds"] = sorted(observed_kinds)
    report["evaluation_kind"] = ("mixed" if len(observed_kinds) == 2 else
                                 next(iter(observed_kinds)) if observed_kinds else "unobserved")
    return summary


def run_batch(task_paths: Iterable[str | Path], output: str | Path,
              config_path: str | Path = ROOT / "serve/config.json",
              seeds: Iterable[int] | None = None, replay: str | Path | None = None,
              plan_only: bool = False) -> dict[str, Any]:
    """Validate inputs, run serial pairs, persist batch.json and summary.json.

    Invalid inputs raise before output is created. Individual pair errors do not
    stop later pairs. Once execution starts, exit_code in the returned manifest
    is 0 for completed subprocesses, 1 for failures, or 128 + termination signal.
    Conclusive hardware/model success is established by summary.json, not exit 0.
    """
    started = time.monotonic()
    seed_list = list(range(5) if seeds is None else seeds)
    if (not seed_list or any(type(seed) is not int for seed in seed_list)
            or len(set(seed_list)) != len(seed_list)):
        raise ValueError("seeds must be a nonempty list of unique integers")
    output_path = Path(output)
    if output_path.exists() or output_path.is_symlink():
        raise ValueError(f"output already exists; choose a new directory: {output_path}")
    output_path = output_path.resolve()
    config_source = Path(config_path).resolve()
    config = load_config(config_source)
    tasks = []
    seen_ids = set()
    for item in task_paths:
        source = Path(item).resolve()
        try:
            task = load_task(source)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise ValueError(f"invalid task {source}: {error}") from error
        if task["task_id"] in seen_ids:
            raise ValueError(f"task_id must be unique within a batch: {task['task_id']}")
        seen_ids.add(task["task_id"])
        tasks.append((source, task))
    if not tasks:
        raise ValueError("at least one task is required")
    replay_source = Path(replay).resolve() if replay is not None else None
    replay_data = None
    if replay_source:
        replay_data = json.loads(replay_source.read_text(encoding="utf-8"))
        if (not isinstance(replay_data, list) or len(replay_data) < 2
                or any(not isinstance(response, str) or not response.strip() for response in replay_data)):
            raise ValueError("pair replay fixture requires at least two nonempty response strings")

    output_path.mkdir(parents=True, exist_ok=False)
    (output_path / "inputs/tasks").mkdir(parents=True)
    (output_path / "logs").mkdir()
    (output_path / "runs").mkdir()
    saved_config = output_path / "inputs/config.json"
    _write_json(saved_config, config)
    saved_replay = output_path / "inputs/replay.json" if replay_source else None
    if saved_replay:
        _write_json(saved_replay, replay_data)
    timeout_s = pair_timeout_s(config)
    report: dict[str, Any] = {
        "schema_version": 1, "track": "rtl", "status": "running", "exit_code": None,
        "evaluation_kind": "mock" if replay_source else "real",
        "requested_evaluation_kind": "mock" if replay_source else "real",
        "observed_evaluation_kinds": [],
        "inference_source": ReplayClient(saved_replay).provenance if saved_replay else LocalClient(config).provenance,
        "execution": "serial independent agent.cli pair subprocesses",
        "config_source": str(config_source), "config_snapshot": "inputs/config.json",
        "config_digest": digest(config), "seeds": seed_list,
        "replay_source": str(replay_source) if replay_source else None,
        "replay_snapshot": "inputs/replay.json" if replay_source else None,
        "pair_timeout_s": timeout_s,
        "pair_timeout_rule": "2 * run_timeout_s + tool_timeout_s + 30 seconds",
        "tasks": [], "runs": [], "wall_s": 0.0,
        "summary": "summary.json", "summary_error": None,
    }
    for index, (source, task) in enumerate(tasks):
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", task["task_id"]).strip("_")[:64] or "task"
        key = f"{index:04d}_{safe_name}"
        relative_task = f"inputs/tasks/{key}.json"
        _write_json(output_path / relative_task, task)
        report["tasks"].append({"task_id": task["task_id"], "task_digest": digest(task),
                                "source": str(source), "snapshot": relative_task})
        for seed in seed_list:
            run_name = f"{key}_seed_{seed}"
            relative_output = f"runs/{run_name}"
            command = [sys.executable, "-m", "agent.cli", "pair", "--task", str(output_path / relative_task),
                       "--output", str(output_path / relative_output), "--config", str(saved_config),
                       "--seed", str(seed)]
            if saved_replay:
                command.extend(["--replay", str(saved_replay)])
            report["runs"].append({"task_id": task["task_id"], "task_digest": digest(task),
                                   "seed": seed, "output": relative_output,
                                   "stdout_log": f"logs/{run_name}.stdout.log",
                                   "stderr_log": f"logs/{run_name}.stderr.log",
                                   "command": command, "status": "pending", "returncode": None,
                                   "wall_s": None, "error": None})

    report["planning"] = {
        "plan_only": plan_only, "tasks": len(tasks), "pairs": len(report["runs"]),
        "model_calls_upper_bound": len(report["runs"]) * (1 + config["max_attempts"]),
        "scheduled_pair_budget_s": len(report["runs"]) * timeout_s,
        "budget_note": "configured serial pair budgets, not a runtime prediction; cleanup overhead is extra",
    }
    if plan_only:
        report.update(status="planned", exit_code=0, wall_s=time.monotonic() - started,
                      counts={"pending": len(report["runs"])})
        _write_json(output_path / "summary.json", _batch_summary(report, output_path))
        _write_json(output_path / "batch.json", report)
        return report

    old_handlers = {}
    interrupted_signal = None
    signal_received = False

    def handle_signal(signum, _frame):
        nonlocal signal_received
        if not signal_received:
            signal_received = True
            raise BatchInterrupted(signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, handle_signal)
    current = None
    try:
        _write_json(output_path / "batch.json", report)
        for entry in report["runs"]:
            current = entry
            entry["status"] = "running"
            _write_json(output_path / "batch.json", report)
            try:
                _run_pair(entry, output_path, timeout_s)
            except Exception as error:
                entry.update(status="error", error=f"{type(error).__name__}: {error}")
            report["wall_s"] = time.monotonic() - started
            _write_json(output_path / "batch.json", report)
            current = None
    except (BatchInterrupted, KeyboardInterrupt) as error:
        interrupted_signal = error.signum if isinstance(error, BatchInterrupted) else signal.SIGINT
        if current is not None and current["status"] in ("pending", "running"):
            current.update(status="interrupted", error=f"batch interrupted by signal {interrupted_signal}")
        for entry in report["runs"]:
            if entry["status"] == "pending":
                entry.update(status="skipped", error="batch interrupted before this pair started")
    finally:
        try:
            _write_json(output_path / "summary.json", _batch_summary(report, output_path))
        except Exception as error:
            report["summary_error"] = f"{type(error).__name__}: {error}"
        failed = any(entry["status"] != "completed" for entry in report["runs"]) or bool(report["summary_error"])
        report["status"] = "interrupted" if interrupted_signal else ("failed" if failed else "completed")
        report["exit_code"] = 128 + interrupted_signal if interrupted_signal else (1 if failed else 0)
        report["wall_s"] = time.monotonic() - started
        report["counts"] = {status: sum(entry["status"] == status for entry in report["runs"])
                            for status in ("completed", "failed", "launch_error", "timed_out",
                                           "error", "interrupted", "skipped", "pending", "running")}
        try:
            _write_json(output_path / "batch.json", report)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--tasks", nargs="+", type=Path)
    sources.add_argument("--task-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new output directory; never overwritten")
    parser.add_argument("--config", type=Path, default=ROOT / "serve/config.json")
    parser.add_argument("--seeds", nargs="+", type=int, default=list(range(5)))
    parser.add_argument("--replay", type=Path, help="software fixtures only; every result remains mock")
    parser.add_argument("--plan-only", action="store_true",
                        help="validate and snapshot the plan without launching inference or EDA")
    args = parser.parse_args(argv)
    try:
        tasks = task_paths_from_manifest(args.task_manifest) if args.task_manifest else args.tasks
        report = run_batch(tasks, args.output, args.config, args.seeds, args.replay, args.plan_only)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        parser.error(str(error))
    print(json.dumps({"status": report["status"], "exit_code": report["exit_code"],
                      "runs": len(report["runs"]), "wall_s": report["wall_s"],
                      "batch": str(args.output.resolve() / "batch.json"),
                      "summary": str(args.output.resolve() / "summary.json")}, ensure_ascii=False, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
