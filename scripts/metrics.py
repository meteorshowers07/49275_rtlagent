#!/usr/bin/env python3
"""Summarize complete real RTL-agent runs without treating unknowns as failures.

Usage: python scripts/metrics.py outputs/run_a outputs/run_b --output metrics/report.json

The pass@k estimator describes independent, complete runs. It is not an official
competition scoring rule, and a run's internal repair attempts are never samples.
Stage durations, when present, describe the final validation attempt only; wall_s
always describes the entire run, including generation and repairs.
Real results require a task_digest so different task contents cannot be pooled.
Standalone runs need no paired_run_id. Repeated seeds within a task/config/mode
are excluded from performance summaries, even with different paired_run_id values.
Diagnostics separately describe all parsed input records, including excluded runs.
"""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable


STAGES = {1: "compile", 2: "simulate", 3: "synthesize"}


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """Unbiased sampling estimate, or None when fewer than k runs exist."""
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (n, c, k)):
        raise ValueError("n, c, and k must be integers")
    if n < 0 or not 0 <= c <= n or k < 1:
        raise ValueError("require n >= 0, 0 <= c <= n, and k >= 1")
    if n < k:
        return None
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def percentile(values: list[float], quantile: float) -> float | None:
    """Linearly interpolate the sorted empirical observations."""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _wall_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    values = [run["wall_s"] for run in runs]
    return {"mean": statistics.mean(values) if values else None,
            "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}


def _stage_durations(run: dict[str, Any], name: str) -> list[float]:
    stages = run["validation"].get("stages", [])
    if not isinstance(stages, list):
        return []
    # Multiple matching entries are ambiguous; do not count repair attempts as
    # independent stage observations or guess which entry represents the final one.
    matching = [item for item in stages if isinstance(item, dict)
                and item.get("name") == name]
    if len(matching) != 1:
        return []
    item = matching[0]
    if item.get("status") not in ("passed", "failed") or not _number(item.get("wall_s")):
        return []
    return [float(item["wall_s"])]


def _summary(runs: list[dict[str, Any]], with_estimates: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"n": len(runs), "wall_s": _wall_summary(runs),
                            "stages": {}}
    scores = [run["validation"]["highest_stage"] for run in runs]
    result["highest_stage_mean"] = statistics.mean(scores) if scores else None
    for stage, name in STAGES.items():
        reached = [run for run in runs if run["validation"]["highest_stage"] >= stage]
        durations = [duration for run in runs for duration in _stage_durations(run, name)]
        data: dict[str, Any] = {
            "name": name, "n": len(runs), "c": len(reached),
            "reached_fraction": len(reached) / len(runs) if runs else None,
            "full_run_wall_s_mean_among_reached": (
                statistics.mean(run["wall_s"] for run in reached) if reached else None),
            "final_validation_wall_s_samples": len(durations),
            "final_validation_wall_s_mean": statistics.mean(durations) if durations else None,
        }
        if with_estimates:
            data.update({"pass_at_1": pass_at_k(len(runs), len(reached), 1),
                         "pass_at_5": pass_at_k(len(runs), len(reached), 5)})
        result["stages"][str(stage)] = data
    return result


_MISSING = object()


def _diagnostic_label(value: Any) -> str:
    """Keep malformed or absent metadata out of the ordinary category buckets."""
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, str) and value.strip():
        return value
    return "<invalid>"


def _diagnostics(records: list[Any], unreadable_files: int) -> dict[str, Any]:
    """Describe all input records independently of performance eligibility."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    non_objects = 0
    for record in records:
        if not isinstance(record, dict):
            non_objects += 1
            continue
        key = (_diagnostic_label(record.get("mode", _MISSING)),
               _diagnostic_label(record.get("evaluation_kind", _MISSING)))
        grouped[key].append(record)
    groups = []
    for (mode, kind), group in sorted(grouped.items()):
        stop_reasons: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        attempts: Counter[str] = Counter()
        selected: Counter[str] = Counter()
        attempt_counts = []
        errors = invalid_errors = invalid_attempt_entries = 0
        for record in group:
            stop_reasons[_diagnostic_label(record.get("stop_reason", _MISSING))] += 1
            validation = record.get("validation", _MISSING)
            status = (validation.get("status", _MISSING) if isinstance(validation, dict)
                      else _MISSING if validation is _MISSING else None)
            statuses[_diagnostic_label(status)] += 1
            error = record.get("error")
            if isinstance(error, str):
                errors += bool(error.strip())
            elif error is not None:
                invalid_errors += 1
            entries = record.get("attempts", _MISSING)
            if isinstance(entries, list):
                count = len(entries)
                attempts[str(count)] += 1
                attempt_counts.append(count)
                invalid_attempt_entries += sum(not isinstance(item, dict) for item in entries)
            else:
                attempts["<missing>" if entries is _MISSING else "<invalid>"] += 1
            chosen = record.get("selected_attempt", _MISSING)
            if isinstance(chosen, int) and not isinstance(chosen, bool) and chosen >= 1:
                selected[str(chosen)] += 1
            else:
                selected["<missing>" if chosen is _MISSING else "<invalid>"] += 1
        groups.append({
            "mode": mode, "evaluation_kind": kind, "n_records": len(group),
            "stop_reason_counts": dict(sorted(stop_reasons.items())),
            "validation_status_counts": dict(sorted(statuses.items())),
            "records_with_error": errors, "records_with_invalid_error": invalid_errors,
            "attempt_count": {"records_with_list": len(attempt_counts),
                              "total": sum(attempt_counts),
                              "mean": statistics.mean(attempt_counts) if attempt_counts else None,
                              "distribution": dict(sorted(attempts.items())),
                              "invalid_entries": invalid_attempt_entries},
            "selected_attempt_counts": dict(sorted(selected.items())),
        })
    return {
        "scope": "all parsed provided result files before performance exclusions, including other tracks, "
                 "mock, unknown, invalid and duplicate records; counts are not pass rates",
        "unit": "result_file", "performance_metric": False,
        "parsed_records": len(records), "unreadable_result_files": unreadable_files,
        "non_object_records": non_objects,
        "attempt_count_scope": "length of recorded attempts lists, including malformed entries; "
                               "not a count of independent samples or unrecorded generation calls",
        "error_count_scope": "nonempty top-level error strings; malformed values counted separately",
        "category_markers": {"<missing>": "field absent", "<invalid>": "wrong type or empty value"},
        "groups": groups,
    }



def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _source_error(record):
    source = record.get("inference_source")
    if not isinstance(source, dict) or record.get("inference_source_digest") != _digest(source):
        return "missing or changed inference source binding"
    if source.get("kind") != "http":
        return "real results require an HTTP inference source; replay/custom sources are excluded"
    endpoint = source.get("endpoint")
    if (not isinstance(endpoint, str) or not endpoint
            or source.get("endpoint_sha256") != hashlib.sha256(endpoint.encode()).hexdigest()
            or not isinstance(source.get("model"), str) or not source["model"]):
        return "invalid endpoint/model source binding"
    return None


def _track_summary(groups):
    """Equal task weights; never pool samples from different tasks/configs/modes."""
    buckets = defaultdict(list)
    for group in groups:
        buckets[(group["config_digest"], group["mode"])].append(group)
    result = []
    for (config, mode), tasks in sorted(buckets.items()):
        stages = {}
        for stage, name in STAGES.items():
            values = {}
            for k in (1, 5):
                estimates = [task["stages"][str(stage)][f"pass_at_{k}"] for task in tasks]
                available = [value for value in estimates if value is not None]
                values[f"pass_at_{k}"] = (statistics.mean(available)
                                           if len(available) == len(tasks) and tasks else None)
                values[f"tasks_with_pass_at_{k}"] = len(available)
            stages[str(stage)] = {"name": name, **values}
        result.append({"config_digest": config, "mode": mode, "n_tasks": len(tasks), "stages": stages})
    return {"weighting": "equal task weights; official dataset weights are not available",
            "scope": "observed task groups only; see planned_performance for the full planned denominator",
            "groups": result}


def _planned_summary(plan, root, runs):
    if plan is None:
        return {"available": False, "reason": "no batch plan supplied; planned denominator is unknown"}
    if plan.get("requested_evaluation_kind") != "real":
        return {"available": False, "reason": "mock plans cannot support performance estimates"}
    accepted = {_sampling_identity(run): run for run in runs}
    tasks = defaultdict(dict)
    present = missing = eligible = 0
    for entry in plan["runs"]:
        for mode in ("baseline", "agent"):
            key = (entry["task_id"], entry["task_digest"], plan["config_digest"], mode, entry["seed"])
            task_key = (entry["task_id"], entry["task_digest"], mode, plan["config_digest"])
            if entry["seed"] in tasks[task_key]:
                raise ValueError("duplicate planned task/mode/seed")
            path = (root / entry["output"] / mode / "result.json").resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("batch output escapes plan directory")
            present += path.is_file()
            missing += not path.is_file()
            run = accepted.get(key)
            if run is not None and Path(run["_source"]) != path:
                run = None
            eligible += run is not None
            tasks[task_key][entry["seed"]] = run
    groups = []
    for (task_id, task_digest, mode, config), slots in sorted(tasks.items()):
        stages = {}
        for stage, name in STAGES.items():
            c = sum(run is not None and run["validation"]["highest_stage"] >= stage for run in slots.values())
            stages[str(stage)] = {"name": name, "n": len(slots), "c": c,
                                  "pass_at_1": pass_at_k(len(slots), c, 1),
                                  "pass_at_5": pass_at_k(len(slots), c, 5)}
        groups.append({"task_id": task_id, "task_digest": task_digest, "mode": mode,
                       "config_digest": config, "n": len(slots), "stages": stages})
    expected = sum(len(slots) for slots in tasks.values())
    return {"available": True, "official_competition_rule": False,
            "policy": "missing, excluded and nonconclusive slots count as no verified success; this is a conservative estimate, not a diagnosis of design failure",
            "expected_results": expected, "present_results": present, "missing_results": missing,
            "eligible_results": eligible, "unverified_or_excluded_results": present - eligible,
            "complete": bool(expected and eligible == expected), "groups": groups,
            "track_summary": _track_summary(groups)}


def _validate(record: Any) -> tuple[str | None, str]:
    if not isinstance(record, dict):
        return "invalid", "result must be a JSON object"
    if record.get("track", "rtl") != "rtl":
        return "invalid", "track must be rtl when present; other tracks cannot enter RTL summaries"
    if record.get("evaluation_kind") == "mock":
        return "mock", "mock runs cannot support performance conclusions"
    validation = record.get("validation")
    if isinstance(validation, dict) and validation.get("status") in ("unverified", "unavailable"):
        status = validation["status"]
        return status, "unknown outcomes are excluded, never treated as failures"
    if record.get("schema_version") != 1 or isinstance(record.get("schema_version"), bool):
        return "invalid", "schema_version must be 1"
    if record.get("evaluation_kind") != "real":
        return "invalid", "evaluation_kind must be real or mock"
    source_error = _source_error(record)
    if source_error:
        return "invalid", source_error
    if record.get("mode") not in ("agent", "baseline"):
        return "invalid", "mode must be agent or baseline"
    for field in ("task_id", "task_digest", "config_digest"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return "invalid", field + " must be a nonempty string"
    if not isinstance(record.get("paired_run_id", ""), str):
        return "invalid", "paired_run_id must be a string when provided"
    if not isinstance(record.get("seed"), int) or isinstance(record["seed"], bool):
        return "invalid", "seed must be an integer"
    if not _number(record.get("wall_s")):
        return "invalid", "wall_s must be a finite nonnegative number"
    if not isinstance(validation, dict) or validation.get("status") not in ("passed", "failed"):
        return "invalid", "validation.status must be passed or failed"
    stage = validation.get("highest_stage")
    if not isinstance(stage, int) or isinstance(stage, bool) or not 0 <= stage <= 3:
        return "invalid", "validation.highest_stage must be an integer from 0 through 3"
    if (validation["status"] == "passed") != (stage == 3):
        return "invalid", "passed requires all three RTL stages; failed requires highest_stage below 3"
    return None, ""


def _identity(run: dict[str, Any], include_mode: bool = True) -> tuple[Any, ...]:
    identity = (run["task_id"], run["task_digest"], run["config_digest"],
                run["paired_run_id"], run["seed"])
    return identity + (run["mode"],) if include_mode else identity


def _sampling_identity(run: dict[str, Any]) -> tuple[Any, ...]:
    return (run["task_id"], run["task_digest"], run["config_digest"], run["mode"], run["seed"])


def _paired_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    slots: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    unmatched = []

    def add_unmatched(run: dict[str, Any], reason: str) -> None:
        unmatched.append({"task_id": run["task_id"], "task_digest": run["task_digest"],
                          "config_digest": run["config_digest"],
                          "paired_run_id": run["paired_run_id"], "seed": run["seed"],
                          "mode": run["mode"], "reason": reason})

    for run in runs:
        if not run["paired_run_id"]:
            add_unmatched(run, "missing_paired_run_id")
        else:
            slots[_identity(run, include_mode=False)][run["mode"]] = run
    paired: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for key, by_mode in sorted(slots.items()):
        if len(by_mode) == 2:
            paired[key[:3]].append((by_mode["baseline"], by_mode["agent"]))
        else:
            run = next(iter(by_mode.values()))
            add_unmatched(run, "no_matching_real_conclusive_partner")
    groups = []
    for (task_id, task_digest, config), pairs in sorted(paired.items()):
        baseline = [pair[0] for pair in pairs]
        agent = [pair[1] for pair in pairs]
        stages = {}
        for stage, name in STAGES.items():
            baseline_rate = statistics.mean(int(run["validation"]["highest_stage"] >= stage)
                                            for run in baseline)
            agent_rate = statistics.mean(int(run["validation"]["highest_stage"] >= stage)
                                         for run in agent)
            stages[str(stage)] = {"name": name, "baseline_reached_fraction": baseline_rate,
                                  "agent_reached_fraction": agent_rate,
                                  "gain_percentage_points": 100 * (agent_rate - baseline_rate)}
        groups.append({
            "task_id": task_id, "task_digest": task_digest, "config_digest": config,
            "n_pairs": len(pairs),
            "pairs": [{"paired_run_id": base["paired_run_id"], "seed": base["seed"]}
                      for base, _ in pairs],
            "baseline_wall_s": _wall_summary(baseline), "agent_wall_s": _wall_summary(agent),
            "agent_minus_baseline_wall_s_mean": statistics.mean(
                added["wall_s"] - base["wall_s"] for base, added in pairs),
            "highest_stage_gain_mean": statistics.mean(
                added["validation"]["highest_stage"] - base["validation"]["highest_stage"]
                for base, added in pairs),
            "stages": stages,
        })
    return {"matching_fields": ["task_id", "task_digest", "config_digest", "paired_run_id", "seed"],
            "requires_nonempty_paired_run_id": True,
            "n_pairs": sum(item["n_pairs"] for item in groups), "groups": groups,
            "unpaired_runs": len(unmatched), "unpaired": unmatched}


def summarize(run_dirs: Iterable[str | Path], *, plan=None, plan_root=None) -> dict[str, Any]:
    """Read result.json trees and return an auditable, JSON-serializable report."""
    paths: set[Path] = set()
    run_dirs = list(run_dirs)
    if plan is None:
        plans = [Path(item) / "batch.json" for item in run_dirs if Path(item).is_dir() and (Path(item) / "batch.json").is_file()]
        if len(plans) > 1:
            raise ValueError("summarize each batch separately to preserve its planned denominator")
        if plans:
            plan = json.loads(plans[0].read_text(encoding="utf-8"))
            plan_root = plans[0].parent
    if plan is not None and plan_root is None:
        raise ValueError("plan_root is required with a plan")
    for item in run_dirs:
        root = Path(item)
        if root.is_dir():
            paths.update(path.resolve() for path in root.rglob("result.json") if path.is_file())
        elif root.is_file() and root.name == "result.json":
            paths.add(root.resolve())
        else:
            raise ValueError(f"not a run directory or result.json: {root}")
    exclusions: list[dict[str, str]] = []
    candidates = []
    records = []
    unreadable_files = 0
    planned_slots = {}
    if plan is not None:
        for entry in plan["runs"]:
            for mode in ("baseline", "agent"):
                slot = (Path(plan_root) / entry["output"] / mode / "result.json").resolve()
                if not slot.is_relative_to(Path(plan_root).resolve()):
                    raise ValueError("batch output escapes plan directory")
                if slot in planned_slots:
                    raise ValueError("duplicate planned output slot")
                planned_slots[slot] = {"task_id": entry["task_id"], "task_digest": entry["task_digest"],
                                       "config_digest": plan["config_digest"], "seed": entry["seed"], "mode": mode}
    for path in sorted(paths):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            unreadable_files += 1
            exclusions.append({"source": str(path), "reason": "invalid", "detail": str(error)})
            continue
        records.append(record)
        reason, detail = _validate(record)
        if reason is None:
            try:
                if plan is not None:
                    expected = planned_slots.get(path)
                    if expected is None or any(type(record.get(key)) is not type(value) or record.get(key) != value for key, value in expected.items()):
                        raise ValueError("result differs from planned slot")
                    if plan.get("requested_evaluation_kind") == "mock":
                        raise ValueError("mock plan cannot produce real performance records")
                    if "inference_source" in plan and record["inference_source"] != plan["inference_source"]:
                        raise ValueError("result inference source differs from batch snapshot")

                config_path = path.parent / "config.json"
                if config_path.is_file():
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    source = record["inference_source"]
                    if (_digest(config) != record["config_digest"]
                            or any(source.get(key) != config.get(key) for key in ("endpoint", "model", "model_revision"))):
                        raise ValueError("inference source differs from saved config")
                for response_path in path.parent.glob("attempt_*/response.json"):
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                    if response.get("mock") is True:
                        reason, detail = "mock", "saved response is a mock fixture"
                        break
            except (OSError, ValueError, AttributeError) as error:
                reason, detail = "invalid", str(error)
        if reason:
            exclusions.append({"source": str(path), "reason": reason, "detail": detail})
        else:
            record = dict(record)
            pair_id = record.get("paired_run_id", "")
            record["paired_run_id"] = pair_id if pair_id.strip() else ""
            record["_source"] = str(path)
            candidates.append(record)
    counts = Counter(_identity(run) for run in candidates)
    sampling_counts = Counter(_sampling_identity(run) for run in candidates)
    runs = []
    for run in candidates:
        if counts[_identity(run)] > 1:
            exclusions.append({"source": run["_source"], "reason": "duplicate_run_identity",
                               "detail": "ambiguous duplicate identity; all copies excluded"})
        elif sampling_counts[_sampling_identity(run)] > 1:
            exclusions.append({"source": run["_source"], "reason": "duplicate_sampling_seed",
                               "detail": "repeated seed for the same task/config/mode, regardless of "
                                         "paired_run_id; all such observations excluded"})
        else:
            runs.append(run)
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[(run["task_id"], run["task_digest"], run["mode"], run["config_digest"])].append(run)
    groups = []
    for (task, task_digest, mode, config), group in sorted(grouped.items()):
        groups.append({"task_id": task, "task_digest": task_digest, "mode": mode, "config_digest": config,
                       **_summary(group, with_estimates=True)})
    excluded_counts = dict.fromkeys(["mock", "unverified", "unavailable", "invalid",
                                     "duplicate_run_identity", "duplicate_sampling_seed"], 0)
    excluded_counts.update(Counter(item["reason"] for item in exclusions))
    return {
        "schema_version": 1, "track": "rtl",
        "estimator": {"official_competition_rule": False,
                      "unit": "independent_complete_run",
                      "formula": "1 - comb(n-c, k) / comb(n, k)", "k": [1, 5],
                      "insufficient_n": None,
                      "stage_success_rule": "validation.highest_stage >= stage",
                      "repair_attempts_are_samples": False,
                      "duplicate_sampling_policy": "all repeated-seed observations within "
                                                   "task_id/task_digest/config_digest/mode are "
                                                   "excluded, including wall-clock summaries; "
                                                   "duplicate_run_identity takes reason precedence",
                      "independence": "assumed, not proved; repeated run seeds are "
                                       "excluded, and the sampler derives each attempt's "
                                       "seed as run_seed * 1000 + attempt - 1, so distinct "
                                       "run seeds stay disjoint at the sampling level",
                      "percentiles": "linear interpolation of sorted observations",
                      "stage_wall_scope": "final validation attempt only; missing timings excluded"},
        "input": {"unique_result_files": len(paths), "eligible_runs": len(runs),
                  "excluded_counts": excluded_counts,
                  "exclusions": sorted(exclusions, key=lambda item: item["source"])},
        "diagnostics": _diagnostics(records, unreadable_files),
        "overall": _summary(runs, with_estimates=False),
        "track_summary": _track_summary(groups),
        "planned_performance": _planned_summary(plan, Path(plan_root) if plan_root else None, runs),
        "groups": groups, "paired_comparisons": _paired_summary(runs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", help="directories recursively containing result.json")
    parser.add_argument("--output", type=Path, help="also save the JSON report")
    args = parser.parse_args(argv)
    try:
        report = summarize(args.run_dirs)
        content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        sys.stdout.write(content)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
