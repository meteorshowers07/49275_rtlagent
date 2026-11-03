#!/usr/bin/env python3
"""Measure real Vivado stage latency and derive the RTL time budgets.

Why this exists
---------------
``serve/config.json`` carries ``run_timeout_s`` and ``tool_timeout_s``. The EDA
adapter shares one ``tool_timeout_s`` deadline across the version probe,
``xvlog``, ``xelab``, ``xsim`` and ``synth_design``, and synthesis runs *last*.
A budget that is too small for real synthesis therefore does not merely slow the
run down: the third stage can never be reached, which suppresses the 30-point
"can it be synthesized" pass rate while looking like a design failure. The RTL
numbers are guesses until real tool latency is measured; the HLS track uses a
different pair with a different stage inventory, so the two tracks are not
comparable and neither set of numbers may be copied from the other.

What it does
------------
Runs the exact production synthesis script (``agent.eda._write_synthesis_script``)
on the exact production absolute shell invocation, plus the compile/simulate
commands, and reports the per-stage wall clock. Then it derives budgets from the
upper percentile of the observations and states whether the current
configuration is even feasible for the measured pipeline.

It never rewrites the configuration unless ``--apply`` is given, and it always
writes an evidence record so a budget claim can be traced to the measurement
that produced it.

Limits that must be stated wherever the result is reported
---------------------------------------------------------
* Local measurements on this host, with this Vivado build and this design mix,
  are *not* a competition result and *not* evidence for the scoring claims.
* With one repetition per stage the percentile is the observation itself. Use
  ``--repeats`` and several ``--task`` values before freezing any number.
* The measured stages are the EDA pipeline only. Model generation time is
  separate; a run budget must cover both, and ``--apply`` prints that warning.

Usage
-----
Measure the built-in probe design, then look before you leap::

    python3 scripts/eda_budget.py --output metrics/eda_budget_profile.json

Measure a sample of real imported tasks::

    python3 scripts/eda_budget.py --repeats 5 \\
        --task data/verilogeval/tasks/Prob001_zero.json \\
        --task data/verilogeval/tasks/Prob035_count1to10.json \\
        --output metrics/eda_budget_profile.json

Apply the measurement to ``serve/config.json`` (the only mode that writes)::

    python3 scripts/eda_budget.py --repeats 5 --task ... --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

if __package__ in (None, ""):  # allow `python3 scripts/eda_budget.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import eda  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
STAGES = ("version", "compile", "simulate", "synthesize")
# Rounding and safety factors are deliberately visible constants, not literals
# buried in the arithmetic: change them here and the printed rationale changes.
BUDGET_QUANTUM_S = 10.0
SAFETY_FACTOR = 1.25
STARTUP_ALLOWANCE_S = 15.0
WALL_MINIMUM_S = 30.0
DEFAULT_REPEATS = 1
DEFAULT_TIMEOUT_S = 1800.0
# A probe large enough to exercise a real implementation, small enough that a
# mistake in this script cannot occupy an EDA licence for an hour.
PROBE_DESIGN = """\
module budget_probe #(parameter int WIDTH = 32) (
    input  logic             clk,
    input  logic             rst,
    input  logic             en,
    input  logic [WIDTH-1:0] a,
    input  logic [WIDTH-1:0] b,
    output logic [WIDTH-1:0] acc,
    output logic [WIDTH-1:0] mixed,
    output logic             flag
);
    logic [WIDTH-1:0] counter;
    logic [2*WIDTH-1:0] product;
    always_comb begin
        product = a * b;
        mixed = product[WIDTH-1:0] ^ {b[WIDTH-2:0], a[0]};
        flag = ^counter;
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            counter <= '0;
            acc <= '0;
        end else if (en) begin
            counter <= counter + 1'b1;
            acc <= mixed + counter;
        end
    end
endmodule
"""


def stage_budget(values: list[float], quantum: float = BUDGET_QUANTUM_S,
                 safety: float = SAFETY_FACTOR) -> float:
    """Upper-percentile stage budget, rounded up, with a stated safety factor.

    The upper percentile is used instead of the mean because exceeding this
    budget does not degrade a result, it deletes a stage.
    """
    if not values:
        raise ValueError("at least one observation is required")
    if quantum <= 0 or safety < 1:
        raise ValueError("quantum must be positive and safety must be at least 1")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool)
           or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("durations must be finite nonnegative numbers")
    # percentile() interpolates like metrics.py does for wall-clock reporting.
    upper = values[0] if len(values) == 1 else _percentile(values, 0.95)
    return math.ceil(upper * safety / quantum) * quantum


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def tool_timeout_suggestion(stages: dict[str, float]) -> float:
    """One shared deadline must cover every stage, so it is the sum, not the max."""
    return math.ceil(sum(stages.values()) / BUDGET_QUANTUM_S) * BUDGET_QUANTUM_S


def run_timeout_suggestion(tool_timeout: float, attempts: int) -> float:
    """A run budget that lets one attempt fail early and the next one finish."""
    return max(WALL_MINIMUM_S, tool_timeout * max(1, attempts))


def load_probe_task(top: str, clock_port: str | None) -> dict[str, Any]:
    return {"task_id": "eda_budget_probe", "prompt": PROBE_DESIGN, "top": top,
            "clock_port": clock_port, "track": "rtl"}


def load_measured_task(path: str | Path, top: str | None, clock_port: str | None) -> dict[str, Any]:
    """Read a real task file, optionally overriding the top/clock for probing."""
    from agent.core import load_task

    task = load_task(path)
    if top is not None:
        task["top"] = top
        task.pop("testbench_files", None)
        task.pop("testbench_top", None)
    if clock_port is not None or top is not None:
        task["clock_port"] = clock_port
    return task


class Toolchain:
    """Resolve the tools and the shared-major-version requirement once."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.timeout_s = timeout_s
        self.paths = {name: shutil.which(name) for name in ("vivado", "xvlog", "xelab", "xsim")}
        self.error: str | None = None
        self.version: str | None = None

    def check(self, work_dir: Path) -> None:
        missing = [name for name in ("vivado", "xvlog", "xelab") if not self.paths[name]]
        if missing:
            self.error = "required tools not found on PATH: " + ", ".join(missing)
            return
        log = work_dir / "version.log"
        result = eda._run_command([self.paths["vivado"], "-version"], work_dir, log,
                                  time.monotonic() + self.timeout_s)
        output = eda._read_log(log)
        if not result["ok"]:
            self.error = "Vivado version check failed: " + result["reason"]
            return
        match = None
        for line in output.splitlines():
            if "Vivado v" in line:
                match = line.strip()
                break
        self.version = match or "unrecognised"
        if not eda.re.search(r"\bVivado\s+v?2025\.2(?:\b|\.)", output, eda.re.I):
            self.error = f"requires Vivado 2025.2; tool reported {self.version}"


def measure(task: dict[str, Any], toolchain: Toolchain, work_dir: Path,
            repeats: int, timeout_s: float) -> dict[str, Any]:
    """Time the production command sequence stage by stage.

    Every repetition gets a fresh directory so a stale netlist can never be
    mistaken for a fast synthesis, and the measured command is byte-identical to
    the one ``validate_rtl`` issues.
    """
    source = task["prompt"]
    durations: dict[str, list[float]] = {name: [] for name in STAGES}
    runs = []
    for index in range(repeats):
        run_dir = Path(tempfile.mkdtemp(prefix=f"budget_{index:02d}_", dir=work_dir))
        (run_dir / "design.sv").write_text(source, encoding="utf-8")
        entry: dict[str, Any] = {"index": index, "dir": str(run_dir), "stages": {}, "ok": True}
        deadline = time.monotonic() + timeout_s
        commands = {
            "compile": [toolchain.paths["xvlog"], "--sv", "design.sv"],
            "simulate": [toolchain.paths["xelab"], task["top"], "-s", "sim_snapshot", "-timescale", "1ps/1ps"],
            "synthesize": [toolchain.paths["vivado"], "-mode", "batch", "-notrace",
                           "-source", "synthesize.tcl"],
        }
        eda._write_synthesis_script(run_dir, task["top"], task.get("clock_port"))
        for name in ("compile", "simulate", "synthesize"):
            log = run_dir / f"{name}.log"
            log.touch()
            started = time.monotonic()
            result = eda._run_command(commands[name], run_dir, log, deadline)
            elapsed = round(time.monotonic() - started, 4)
            entry["stages"][name] = {"wall_s": elapsed, "ok": result["ok"],
                                     "reason": result["reason"]}
            durations[name].append(elapsed)
            if not result["ok"]:
                entry["ok"] = False
                break
        entry["wall_s"] = round(sum(item["wall_s"] for item in entry["stages"].values()), 4)
        runs.append(entry)
    return {"task_id": task["task_id"], "top": task["top"],
            "clock_port": task.get("clock_port"), "runs": runs,
            "durations": durations}


def summarize(measurements: list[dict[str, Any]], attempts: int,
              current: dict[str, Any]) -> dict[str, Any]:
    pooled: dict[str, list[float]] = {name: [] for name in STAGES}
    for measurement in measurements:
        for name, values in measurement["durations"].items():
            pooled[name].extend(values)
    complete = all(pooled[name] for name in ("compile", "simulate", "synthesize"))
    stages = {name: stage_budget(values) for name, values in pooled.items() if values}
    tool_timeout = tool_timeout_suggestion(stages)
    run_timeout = run_timeout_suggestion(tool_timeout, attempts)
    current_tool = current.get("tool_timeout_s")
    current_run = current.get("run_timeout_s")
    return {
        "observed_s": {name: {"samples": len(values), "min": min(values), "max": max(values),
                              "mean": sum(values) / len(values)} for name, values in pooled.items() if values},
        "all_stages_measured": complete,
        "recommended_s": {"stages": stages, "tool_timeout_s": tool_timeout,
                          "run_timeout_s": run_timeout},
        "current_s": {"tool_timeout_s": current_tool, "run_timeout_s": current_run},
        "feasible_today": (isinstance(current_tool, (int, float)) and complete
                           and current_tool >= sum(stages.values())),
        "evidence": {
            "measured_on": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": f"{os.name}/{sys.platform}",
            "vivado": None,
            "official_competition_evidence": False,
            "note": "Local EDA latency on this host and design mix; not a scoring result. "
                    "A percentile from a single sample is that sample.",
        },
    }


def apply_budgets(path: Path, recommended: dict[str, Any]) -> dict[str, Any]:
    """Rewrite only the two budget keys, preserving every other key and its order."""
    original = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(original, dict):
        raise ValueError("configuration must be a JSON object")
    updated = dict(original)
    updated["tool_timeout_s"] = recommended["tool_timeout_s"]
    updated["run_timeout_s"] = recommended["run_timeout_s"]
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    changed = {key: {"before": original.get(key), "after": updated[key]}
               for key in ("tool_timeout_s", "run_timeout_s") if original.get(key) != updated[key]}
    return {"config": str(path), "changed": changed, "unchanged_keys_preserved": sorted(
        set(original) - set(changed))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task", action="append", default=[], metavar="TASK.json",
                        help="real task file to measure; repeat for several. "
                             "Without it the built-in probe design is used")
    parser.add_argument("--top", help="override the top module name for the probe")
    parser.add_argument("--clock-port", help="override the clock port name for the probe")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                        help=f"measurements per design (default {DEFAULT_REPEATS})")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S,
                        help="safety cap per repetition, not a budget claim")
    parser.add_argument("--config", type=Path, default=ROOT / "serve" / "config.json")
    parser.add_argument("--apply", action="store_true",
                        help="write the measured budgets into --config (sole writing mode)")
    parser.add_argument("--output", type=Path, default=ROOT / "metrics" / "eda_budget_profile.json")
    parser.add_argument("--work-dir", type=Path,
                        help="where probes run (default: a fresh directory under outputs/)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        print("eda_budget: --repeats must be at least 1", file=sys.stderr)
        return 2
    current = json.loads(args.config.read_text(encoding="utf-8"))
    attempts = current.get("max_attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        attempts = 3

    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="eda_budget_", dir=ROOT / "outputs"))
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.task:
        tasks = [load_measured_task(path, args.top, args.clock_port) for path in args.task]
    else:
        tasks = [load_probe_task(args.top or "budget_probe", args.clock_port or "clk")]

    toolchain = Toolchain(timeout_s=args.timeout_s)
    toolchain.check(work_dir)
    if toolchain.error:
        # Never invent a number when the measurement could not be taken.
        print(json.dumps({"status": "unavailable", "reason": toolchain.error,
                          "config": str(args.config), "measured": False},
                         ensure_ascii=False, indent=2))
        return 3

    measurements = []
    for task in tasks:
        print(f"measuring {task['task_id']} x{args.repeats} ...", file=sys.stderr)
        measurements.append(measure(task, toolchain, work_dir, args.repeats, args.timeout_s))

    report = summarize(measurements, attempts, current)
    report["evidence"]["vivado"] = toolchain.version
    report["measurements"] = measurements
    report["inputs"] = {"config": str(args.config), "tasks": [item["task_id"] for item in measurements],
                        "repeats": args.repeats, "work_dir": str(work_dir),
                        "attempts_assumed": attempts}
    report["warnings"] = warnings_for(report, args)
    report["recommendation_basis"] = {
        "stage_budget": f"p95 of the observations x {SAFETY_FACTOR}, rounded up to "
                        f"{BUDGET_QUANTUM_S:g} s",
        "tool_timeout_s": "sum of the stage budgets (all stages share one deadline)",
        "run_timeout_s": f"tool_timeout_s x max_attempts ({attempts}), floor {WALL_MINIMUM_S:g} s",
        "startup_allowance_s": STARTUP_ALLOWANCE_S,
        "important": "A stage budget is not a wall-clock target. Raising run_timeout_s raises "
                     "cost on the 10-point wall-clock item and caps how many tasks fit in the "
                     "evaluation window; choose it from an end-to-end run on the development "
                     "split, not from this file alone.",
    }
    if args.apply:
        report["applied"] = apply_budgets(args.config, report["recommended_s"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                           encoding="utf-8")

    print(f"measured {len(measurements)} design(s) x {args.repeats} repetition(s)")
    for name in STAGES:
        observed = report["observed_s"].get(name)
        if observed:
            print(f"  {name:<11} n={observed['samples']:<3} "
                  f"min={observed['min']:>8.3f}s max={observed['max']:>8.3f}s "
                  f"budget->{report['recommended_s']['stages'].get(name, 0):>7.1f}s")
    print(f"  shared tool deadline  {report['current_s']['tool_timeout_s']} -> "
          f"{report['recommended_s']['tool_timeout_s']:.1f} s")
    print(f"  run budget            {report['current_s']['run_timeout_s']} -> "
          f"{report['recommended_s']['run_timeout_s']:.1f} s")
    if report["all_stages_measured"] and not report["feasible_today"]:
        print("  INFEASIBLE TODAY: the configured tool deadline cannot cover the measured "
              "pipeline, so the last stage cannot be reached.")
    for warning in report["warnings"]:
        print(f"  warning: {warning}")
    print(f"  evidence -> {args.output}")
    if args.apply:
        print(f"  applied -> {args.config}")
    return 0


def warnings_for(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Say plainly what this measurement cannot support."""
    warnings = []
    if not report["all_stages_measured"]:
        warnings.append("at least one stage never completed; the budget is derived only from the "
                        "stages that did, and is therefore a lower bound")
    if args.repeats < 5:
        warnings.append(f"--repeats {args.repeats} gives no usable percentile; "
                        "an upper percentile needs several observations")
    if len(report["inputs"]["tasks"]) < 3:
        warnings.append("fewer than three designs measured; latency depends on the design, so "
                        "sample the development split before freezing a budget")
    if not args.apply:
        warnings.append("nothing was written to the configuration; re-run with --apply only "
                        "after reviewing this file")
    return warnings


if __name__ == "__main__":
    raise SystemExit(main())
