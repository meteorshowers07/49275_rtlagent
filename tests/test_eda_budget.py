"""Budget calibration tests. They use fake tools; they are not a Vivado run."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch

from scripts import eda_budget


VERSION_OUTPUT = "Vivado v2025.2 (64-bit)\nSW Build 123\n"


class FakeTools:
    """Stand in for _run_command and record the exact commands issued."""

    def __init__(self, fail=(), version=VERSION_OUTPUT):
        self.fail = set(fail)
        self.version = version
        self.commands = []

    def __call__(self, args, cwd, log, deadline):
        name = "version" if "-version" in args else Path(args[0]).name
        self.commands.append((name, list(args)))
        log.parent.mkdir(parents=True, exist_ok=True)
        if name == "version":
            log.write_text(self.version, encoding="utf-8")
        if name in self.fail:
            return {"ok": False, "reason": "total validation timeout exceeded",
                    "failure_cause": "budget_exhausted"}
        return {"ok": True, "reason": "command completed", "failure_cause": None}


class BudgetMathTests(unittest.TestCase):
    def test_stage_budget_uses_upper_percentile_with_rounding_and_safety(self):
        # p95 of 1..10 is 9.55; x1.25 = 11.94; ceil to a 10 s quantum = 20 s.
        self.assertEqual(eda_budget.stage_budget([float(value) for value in range(1, 11)]), 20.0)
        # A single observation is that observation, rounded up.
        self.assertEqual(eda_budget.stage_budget([41.0]), 60.0)
        self.assertEqual(eda_budget.stage_budget([1.0]), 10.0)
        self.assertEqual(eda_budget.stage_budget([0.0]), 0.0)

    def test_stage_budget_rejects_unusable_observations(self):
        for values in ([], [float("nan")], [float("inf")], [-1.0], [True], ["3"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                eda_budget.stage_budget(values)
        with self.assertRaises(ValueError):
            eda_budget.stage_budget([1.0], quantum=0)
        with self.assertRaises(ValueError):
            eda_budget.stage_budget([1.0], safety=0.5)

    def test_shared_deadline_is_the_sum_of_stage_budgets_not_the_maximum(self):
        stages = {"version": 10.0, "compile": 10.0, "simulate": 20.0, "synthesize": 150.0}
        self.assertEqual(eda_budget.tool_timeout_suggestion(stages), 190.0)

    def test_run_budget_keeps_room_for_a_failed_then_a_successful_attempt(self):
        self.assertEqual(eda_budget.run_timeout_suggestion(190.0, 3), 570.0)
        # A tiny tool deadline still gets the floor, never zero.
        self.assertEqual(eda_budget.run_timeout_suggestion(5.0, 1), eda_budget.WALL_MINIMUM_S)

    def test_summarize_reports_the_current_configuration_as_infeasible(self):
        measurements = [{"task_id": "t", "durations": {"version": [15.0], "compile": [3.0],
                                                       "simulate": [5.0], "synthesize": [120.0]}}]
        report = eda_budget.summarize(measurements, 3, {"tool_timeout_s": 60, "run_timeout_s": 180})
        self.assertTrue(report["all_stages_measured"])
        self.assertFalse(report["feasible_today"])
        self.assertEqual(report["observed_s"]["synthesize"]["samples"], 1)
        self.assertGreaterEqual(report["recommended_s"]["tool_timeout_s"],
                                sum(report["recommended_s"]["stages"].values()))

    def test_summarize_treats_a_missing_stage_as_incomplete_evidence(self):
        measurements = [{"task_id": "t", "durations": {"version": [15.0], "compile": [3.0],
                                                       "simulate": [], "synthesize": []}}]
        report = eda_budget.summarize(measurements, 3, {"tool_timeout_s": 600, "run_timeout_s": 600})
        self.assertFalse(report["all_stages_measured"])
        self.assertFalse(report["feasible_today"])

    def test_apply_rewrites_only_the_two_budget_keys_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            original = {"endpoint": "http://127.0.0.1:8000/v1", "model": "rtl-local",
                        "max_attempts": 3, "run_timeout_s": 180, "tool_timeout_s": 60,
                        "track": "rtl"}
            config.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
            result = eda_budget.apply_budgets(config, {"tool_timeout_s": 240.0, "run_timeout_s": 720.0})
            updated = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(updated["tool_timeout_s"], 240.0)
            self.assertEqual(updated["run_timeout_s"], 720.0)
            self.assertEqual(list(updated), list(original))
            self.assertEqual(updated["max_attempts"], 3)
            self.assertEqual(result["changed"]["tool_timeout_s"], {"before": 60, "after": 240.0})
            self.assertNotIn("tool_timeout_s", result["unchanged_keys_preserved"])
            self.assertTrue(config.read_text(encoding="utf-8").endswith("\n"))


class MeasureTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"RTL_AGENT_VIVADO_VERSION": "2025.2", "HLS_AGENT_WORK_ROOT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / "work"
        self.work.mkdir()
        self.task = eda_budget.load_probe_task("budget_probe", "clk")
        self.tools = FakeTools()
        self.which = patch.object(eda_budget.shutil, "which",
                                  side_effect=lambda name: "/mock/Vivado/2025.2/bin/" + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def run_measure(self, repeats=1, fail=()):
        self.tools = FakeTools(fail=fail)
        with patch.object(eda_budget.eda, "_run_command", side_effect=self.tools):
            return eda_budget.measure(self.task, eda_budget.Toolchain(), self.work, repeats, 600.0)

    def test_commands_match_the_production_pipeline_exactly(self):
        self.run_measure()
        names = [name for name, _ in self.tools.commands]
        self.assertEqual(names, ["xvlog", "xelab", "vivado"])
        compile_args = next(args for name, args in self.tools.commands if name == "xvlog")
        self.assertEqual(compile_args[1:], ["--sv", "design.sv"])
        elaborate = next(args for name, args in self.tools.commands if name == "xelab")
        self.assertEqual(elaborate[1:], ["budget_probe", "-s", "sim_snapshot", "-timescale", "1ps/1ps"])
        synthesize = next(args for name, args in self.tools.commands if name == "vivado")
        self.assertEqual(synthesize[1:], ["-mode", "batch", "-notrace", "-source", "synthesize.tcl"])

    def test_each_repetition_gets_a_fresh_directory_with_the_production_script(self):
        measurement = self.run_measure(repeats=3)
        directories = [entry["dir"] for entry in measurement["runs"]]
        self.assertEqual(len(set(directories)), 3)
        for directory in directories:
            script = (Path(directory) / "synthesize.tcl").read_text(encoding="utf-8")
            self.assertIn("synth_design -top {budget_probe}", script)
            self.assertIn(eda_budget.eda.PART, script)
            constraints = (Path(directory) / "constraints.xdc").read_text(encoding="utf-8")
            self.assertIn("-period 5.000", constraints)
            self.assertIn("[get_ports {clk}]", constraints)

    def test_a_failed_stage_stops_the_pipeline_and_keeps_the_prefix(self):
        measurement = self.run_measure(fail={"xelab"})
        self.assertFalse(measurement["runs"][0]["ok"])
        self.assertEqual(list(measurement["runs"][0]["stages"]), ["compile", "simulate"])
        self.assertEqual([name for name, _ in self.tools.commands], ["xvlog", "xelab"])


class MainTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"RTL_AGENT_VIVADO_VERSION": "2025.2", "HLS_AGENT_WORK_ROOT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "endpoint": "http://127.0.0.1:8000/v1", "model": "rtl-local", "max_attempts": 3,
            "run_timeout_s": 180, "tool_timeout_s": 60, "track": "rtl",
        }, indent=2) + "\n", encoding="utf-8")
        self.output = self.root / "profile.json"
        self.work = self.root / "work"

    def test_missing_vivado_reports_unavailable_and_measures_nothing(self):
        with patch.object(eda_budget.shutil, "which", return_value=None), \
                patch("builtins.print") as printed:
            code = eda_budget.main(["--config", str(self.config), "--output", str(self.output),
                                    "--work-dir", str(self.work)])
        self.assertEqual(code, 3)
        payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["measured"])
        self.assertFalse(self.output.exists())

    def test_wrong_vivado_version_reports_unavailable(self):
        tools = FakeTools(version="Vivado v2024.2 (64-bit)\n")
        with patch.object(eda_budget.shutil, "which",
                          side_effect=lambda name: "/mock/Vivado/2024.2/bin/" + name), \
                patch.object(eda_budget.eda, "_run_command", side_effect=tools), \
                patch("builtins.print"):
            code = eda_budget.main(["--config", str(self.config), "--output", str(self.output),
                                    "--work-dir", str(self.work)])
        self.assertEqual(code, 3)
        self.assertFalse(self.output.exists())

    def test_measurement_writes_evidence_without_touching_the_config_by_default(self):
        before = self.config.read_text(encoding="utf-8")
        tools = FakeTools()
        with patch.object(eda_budget.shutil, "which",
                          side_effect=lambda name: "/mock/Vivado/2025.2/bin/" + name), \
                patch.object(eda_budget.eda, "_run_command", side_effect=tools), \
                patch.object(eda_budget, "measure",
                             return_value={"task_id": "probe", "durations": {
                                 "version": [15.0], "compile": [3.0],
                                 "simulate": [5.0], "synthesize": [120.0]}}), \
                patch("builtins.print") as printed:
            code = eda_budget.main(["--config", str(self.config), "--output", str(self.output),
                                    "--work-dir", str(self.work), "--repeats", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["evidence"]["official_competition_evidence"], False)
        self.assertEqual(report["evidence"]["vivado"], "Vivado v2025.2 (64-bit)")
        self.assertEqual(report["inputs"]["attempts_assumed"], 3)
        self.assertFalse(report["feasible_today"])
        self.assertTrue(any("--repeats" in warning for warning in report["warnings"]))
        self.assertTrue(any("configuration" in warning for warning in report["warnings"]))
        self.assertNotIn("applied", report)
        printed_lines = [call[0][0] for call in printed.call_args_list if call[0]]
        self.assertTrue(any("INFEASIBLE TODAY" in str(line) for line in printed_lines))

    def test_apply_writes_the_measured_budgets_and_records_them(self):
        with patch.object(eda_budget.shutil, "which",
                          side_effect=lambda name: "/mock/Vivado/2025.2/bin/" + name), \
                patch.object(eda_budget.eda, "_run_command", side_effect=FakeTools()), \
                patch.object(eda_budget, "measure",
                             return_value={"task_id": "probe", "durations": {
                                 "version": [15.0], "compile": [3.0],
                                 "simulate": [5.0], "synthesize": [120.0]}}), \
                patch("builtins.print"):
            code = eda_budget.main(["--config", str(self.config), "--output", str(self.output),
                                    "--work-dir", str(self.work), "--apply"])
        self.assertEqual(code, 0)
        updated = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertGreater(updated["tool_timeout_s"], 60)
        self.assertGreater(updated["run_timeout_s"], 180)
        self.assertEqual(updated["max_attempts"], 3)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["applied"]["changed"]["tool_timeout_s"]["before"], 60)
        self.assertNotIn("track", report["applied"]["changed"])

    def test_repeats_below_one_is_rejected_before_any_tool_call(self):
        with patch.object(eda_budget, "measure") as measure, patch("builtins.print"):
            code = eda_budget.main(["--config", str(self.config), "--output", str(self.output),
                                    "--repeats", "0"])
        self.assertEqual(code, 2)
        measure.assert_not_called()

    def test_real_task_files_can_be_measured_without_their_testbench(self):
        task_path = self.root / "task.json"
        task_path.write_text(json.dumps({"task_id": "real", "prompt": "module TopModule(); endmodule",
                                         "top": "TopModule", "clock_port": "clock",
                                         "testbench_files": ["missing_tb.sv"],
                                         "testbench_top": "tb"}), encoding="utf-8")
        task = eda_budget.load_measured_task(task_path, None, None)
        self.assertEqual(task["top"], "TopModule")
        self.assertEqual(task["clock_port"], "clock")
        self.assertEqual(task["testbench_files"], [str((self.root / "missing_tb.sv").resolve())])
        stripped = eda_budget.load_measured_task(task_path, "Other", "clk")
        self.assertEqual(stripped["top"], "Other")
        self.assertEqual(stripped["clock_port"], "clk")
        self.assertNotIn("testbench_files", stripped)
        self.assertEqual(eda_budget.load_probe_task("p", None)["track"], "rtl")


if __name__ == "__main__":
    unittest.main()
