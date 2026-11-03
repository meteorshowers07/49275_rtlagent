"""Mocked EDA contract tests; these do not substitute for a real Vivado run."""

from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
import os
from unittest.mock import Mock, patch

from agent import eda


VERILOGEVAL_OUTPUT = (
    "Hint: Output 'zero' has no mismatches.\n"
    "Hint: Total mismatched samples is 0 out of 20 samples\n\n"
    "Simulation finished at 102 ps\n"
    "Mismatches: 0 in 20 samples\n"
)


class FakeProcess:
    pid = 54321

    def __init__(self, lab, args, **kwargs):
        self.lab = lab
        self.args = args
        self.killed = False
        self.timed_out = False
        self.key = "version" if "-version" in args else Path(args[0]).name
        self.lab.calls.append((args, kwargs))
        stream = kwargs["stdout"]
        stream.write(lab.outputs.get(self.key, ""))
        stream.flush()
        if self.key == "vivado" and lab.artifacts:
            for name in lab.artifacts:
                (Path(kwargs["cwd"]) / name).write_text("mock tool artifact\n", encoding="utf-8")

    def wait(self, timeout=None):
        self.lab.waits.append((self.key, timeout))
        if self.key == self.lab.timeout_key and not self.timed_out:
            self.timed_out = True
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.lab.codes.get(self.key, 0)

    def kill(self):
        self.killed = True


class FakeLab:
    def __init__(self):
        self.calls = []
        self.waits = []
        self.codes = {}
        self.outputs = {
            "version": "Vivado v2025.2 (64-bit)\nSW Build 123\n",
            "xvlog": "Analysis completed\n", "xelab": "Elaboration complete\n",
            "xsim": "errors: 0; mismatches = 0; 0 tests failed\nTB_PASS\n",
            "vivado": "Synthesis completed\n",
        }
        self.artifacts = ["netlist.v", "utilization.rpt", "timing_summary.rpt", "timing_paths.rpt"]
        self.timeout_key = None

    def popen(self, args, **kwargs):
        return FakeProcess(self, args, **kwargs)

    @property
    def tools(self):
        return ["version" if "-version" in args else Path(args[0]).name for args, _ in self.calls]


class EdaTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"RTL_AGENT_VIVADO_VERSION": "2025.2", "HLS_AGENT_WORK_ROOT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.code = self.root / "candidate [space] ' name.sv"
        self.code.write_text("module counter(input clk); endmodule\n", encoding="utf-8")
        self.tb = self.root / "trusted testbench.sv"
        self.tb.write_text('module tb; initial begin $display("TB_PASS"); $finish; end endmodule\n', encoding="utf-8")
        self.task = {
            "top": "counter", "clock_port": "clk", "testbench_files": [str(self.tb)],
            "testbench_top": "tb", "simulation_pass_marker": "TB_PASS",
        }
        self.lab = FakeLab()
        self.which = patch.object(eda.shutil, "which", side_effect=lambda name: "/mock/Vivado/2025.2/bin/" + name)
        self.popen = patch.object(eda, "_launch_tool", side_effect=self.lab.popen)
        self.cleanup_tree = patch.object(eda, 'kill_process_tree', side_effect=lambda p: p.kill())
        self.cleanup_tree.start()
        self.addCleanup(self.cleanup_tree.stop)
        self.which.start()
        self.popen.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.which.stop)
        self.addCleanup(self.popen.stop)

    def run_validation(self, timeout=30):
        return eda.validate_rtl(self.code, self.task, self.root / "work [safe]", timeout)

    def use_verilogeval(self):
        self.task["simulation_protocol"] = "verilogeval_v2"
        self.task.pop("simulation_pass_marker", None)
        self.lab.outputs["xsim"] = VERILOGEVAL_OUTPUT

    def test_elaboration_sets_default_timescale_without_editing_source(self):
        before = self.code.read_bytes()
        result = self.run_validation()
        command = next(args for args, _ in self.lab.calls if Path(args[0]).name == 'xelab')
        self.assertEqual(command[-2:], ['-timescale', '1ps/1ps'])
        self.assertEqual(self.code.read_bytes(), before)
        self.assertEqual(result['highest_stage'], 3)

    def test_missing_tools_are_unavailable_without_execution(self):
        with patch.object(eda.shutil, "which", return_value=None):
            result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["highest_stage"], 0)
        self.assertEqual(self.lab.calls, [])

    def test_wrong_vivado_version_stops_validation(self):
        self.lab.outputs["version"] = "Vivado v2024.2 (64-bit)\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.lab.tools, ["version"])

    def test_mixed_installation_is_rejected(self):
        def which(name):
            return "/other/bin/xsim" if name == "xsim" else "/mock/Vivado/2025.2/bin/" + name
        with patch.object(eda.shutil, "which", side_effect=which):
            result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("bin directory", result["feedback"])

    def test_compile_failure_prevents_every_later_command(self):
        self.lab.codes["xvlog"] = 1
        self.lab.outputs["xvlog"] = "ERROR: invalid syntax\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 0)
        self.assertEqual(self.lab.tools, ["version", "xvlog"])
        self.assertIn("invalid syntax", result["feedback"])

    def test_elaboration_failure_prevents_simulation(self):
        self.lab.codes["xelab"] = 2
        result = self.run_validation()
        self.assertEqual(result["highest_stage"], 0)
        self.assertEqual(self.lab.tools, ["version", "xvlog", "xelab"])

    def test_compile_error_diagnostic_rejects_zero_exit(self):
        for tool in ("xvlog", "xelab"):
            with self.subTest(tool=tool):
                self.lab.calls.clear()
                self.lab.outputs[tool] = "ERROR: [VRFC 10-4982] syntax failure\n"
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["highest_stage"], 0)
                self.assertNotIn("xsim", self.lab.tools)
                self.lab.outputs[tool] = "Compilation completed\n"

    def test_no_testbench_only_verifies_compile(self):
        self.task.pop("testbench_files")
        result = self.run_validation()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["highest_stage"], 1)
        self.assertEqual(self.lab.calls[-1][0][1], "counter")
        self.assertNotIn("xsim", self.lab.tools)

    def test_missing_simulator_is_unavailable_after_compile(self):
        with patch.object(eda.shutil, "which", side_effect=lambda name: None if name == "xsim" else "/mock/bin/" + name):
            result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["highest_stage"], 1)

    def test_simulation_failure_prevents_synthesis(self):
        self.lab.outputs["xsim"] = "TB_PASS\nERROR: data mismatches: 1\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 1)
        self.assertNotIn("vivado", self.lab.tools)

    def test_marker_rejects_tool_fatal_error_despite_pass_marker_and_zero_exit(self):
        for diagnostic in ('FATAL_ERROR: stopped', '# FATAL_ERROR: stopped', 'ERROR: 0 unexpected condition'):
            with self.subTest(diagnostic=diagnostic):
                self.lab.calls.clear()
                self.lab.outputs['xsim'] = diagnostic + '\nTB_PASS\n'
                result = self.run_validation()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['highest_stage'], 1)
                self.assertIn(diagnostic.strip(), result['feedback'])
                self.assertNotIn('vivado', self.lab.tools)

    def test_nonzero_simulator_exit_prevents_synthesis(self):
        self.lab.codes["xsim"] = 1
        result = self.run_validation()
        self.assertEqual(result["highest_stage"], 1)
        self.assertNotIn("vivado", self.lab.tools)

    def test_unconfigured_pass_marker_is_unverified(self):
        self.task.pop("simulation_pass_marker")
        result = self.run_validation()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["highest_stage"], 1)
        self.assertNotIn("vivado", self.lab.tools)

    def test_zero_exit_without_marker_is_failure(self):
        self.lab.outputs["xsim"] = "Simulation finished normally\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 1)
        self.assertNotIn("vivado", self.lab.tools)

    def test_pass_marker_must_be_whole_line(self):
        self.lab.outputs["xsim"] = "Would print TB_PASS if tests finished\n"
        result = self.run_validation()
        self.assertEqual(result["highest_stage"], 1)
        self.assertEqual(result["status"], "failed")

    def test_zero_failure_counts_are_not_failures(self):
        for text in ("0 errors", "errors=0", "mismatches: 0", "failed tests: 0", "0 tests failed", "no errors", "zero failures"):
            with self.subTest(text=text):
                self.assertIsNone(eda._simulation_failure(text))
        self.assertIsNotNone(eda._simulation_failure("errors=0, mismatches=2"))
        self.assertIsNotNone(eda._simulation_failure("ERROR: [SIM 0] assertion violation"))
        self.assertIsNotNone(eda._simulation_failure("failed tests: 10"))

    def test_full_success_requires_reports_and_safe_commands(self):
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["highest_stage"], 3)
        self.assertEqual(self.lab.tools, ["version", "xvlog", "xelab", "xsim", "vivado"])
        for args, kwargs in self.lab.calls:
            self.assertIsInstance(args, list)
            self.assertNotIn("shell", kwargs)
            self.assertTrue(kwargs["start_new_session"])
        run_dir = Path(result["stages"][2]["log"]).parent
        script = (run_dir / "synthesize.tcl").read_text(encoding='utf-8')
        self.assertIn("xczu3eg-sbva484-1-e", script)
        self.assertNotIn(str(self.code), script)
        constraints = (run_dir / "constraints.xdc").read_text(encoding='utf-8')
        self.assertIn("-period 5.000", constraints)
        self.assertIn("[get_ports {clk}]", constraints)
        for stage in result["stages"]:
            self.assertTrue(Path(stage["log"]).is_file())

    def test_virtual_clock_when_no_clock_port(self):
        self.task["clock_port"] = None
        result = self.run_validation()
        constraints = (Path(result["stages"][2]["log"]).parent / "constraints.xdc").read_text(encoding='utf-8')
        self.assertIn("virtual_clock", constraints)
        self.assertNotIn("get_ports", constraints)

    def test_successful_tool_without_netlist_is_rejected(self):
        self.lab.artifacts.remove("netlist.v")
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 2)
        self.assertIn("netlist.v", result["feedback"])

    def test_synthesis_error_diagnostic_rejects_zero_exit_and_artifacts(self):
        self.lab.outputs["vivado"] = "ERROR: [Synth 8-6156] failed synthesizing module\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 2)
        self.assertIn("Synth 8-6156", result["feedback"])

    def test_missing_timing_report_is_rejected(self):
        self.lab.artifacts.remove("timing_summary.rpt")
        result = self.run_validation()
        self.assertEqual(result["highest_stage"], 2)
        self.assertEqual(result["status"], "failed")

    def test_each_attempt_is_fresh_and_cannot_reuse_old_netlist(self):
        first = self.run_validation()
        self.lab.artifacts = []
        second = self.run_validation()
        self.assertEqual(first["status"], "passed")
        self.assertEqual(second["status"], "failed")
        self.assertNotEqual(first["stages"][0]["log"], second["stages"][0]["log"])

    @unittest.skipUnless(hasattr(os, "killpg"), "process group cleanup uses POSIX killpg")
    def test_timeout_kills_process_group_and_stops_pipeline(self):
        self.lab.timeout_key = "xvlog"
        with patch.object(eda.os, "killpg") as killpg:
            result = self.run_validation()
        if eda.os.name == "posix":
            killpg.assert_called_once_with(54321, signal.SIGKILL)
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout", result["feedback"])
        self.assertEqual(self.lab.tools, ["version", "xvlog"])

    @unittest.skipUnless(hasattr(os, "killpg"), "process group cleanup uses POSIX killpg")
    def test_cancellation_kills_and_reaps_eda_group_before_reraising(self):
        interrupted = KeyboardInterrupt("batch cancelled")
        events = []
        process = Mock(pid=54322)

        def wait(timeout=None):
            if not events:
                events.append("interrupted_wait")
                raise interrupted
            self.assertEqual(events[-1], "kill_group")
            self.assertIsNone(timeout)
            events.append("reaped")
            return -signal.SIGKILL

        process.wait.side_effect = wait
        process.kill.side_effect = lambda: events.append("kill_group")
        log = self.root / "cancelled.log"
        with patch.object(eda, "_launch_tool", return_value=process), \
                patch.object(eda.os, "killpg", side_effect=lambda *args: events.append("kill_group")) as killpg:
            with self.assertRaises(KeyboardInterrupt) as raised:
                eda._run_command(["xvlog", "--sv", "design.sv"], self.root, log, eda.time.monotonic() + 30)
        self.assertIs(raised.exception, interrupted)
        self.assertEqual(events, ["interrupted_wait", "kill_group", "reaped"])
        self.assertEqual(process.wait.call_count, 2)
        if eda.os.name == "posix":
            killpg.assert_called_once_with(54322, signal.SIGKILL)
            process.kill.assert_not_called()
        else:
            process.kill.assert_called_once()
        self.assertIn("cancelled", log.read_text(encoding='utf-8'))

    def test_all_stages_share_a_single_deadline(self):
        recorded_deadlines = []
        original = eda._run_command
        def record(args, cwd, log, deadline):
            recorded_deadlines.append(deadline)
            return original(args, cwd, log, deadline)
        with patch.object(eda, "_run_command", side_effect=record):
            self.run_validation()
        self.assertEqual(len(recorded_deadlines), 5)
        self.assertEqual(len(set(recorded_deadlines)), 1)

    def test_synthesis_timeout_is_a_budget_failure_not_a_design_error(self):
        # The whole point of P6: with one shared deadline, synthesis is the stage
        # that starves. Report it as a counted failure, labelled as budget.
        self.lab.timeout_key = "vivado"
        result = self.run_validation()
        self.assertEqual(self.lab.tools, ["version", "xvlog", "xelab", "xsim", "vivado"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 2)
        self.assertEqual(result["failure_cause"], "tool_timeout")
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["stages"][2]["status"], "failed")
        self.assertIn("timeout", result["stages"][2]["reason"])

    def test_design_failure_is_not_labelled_as_a_budget_failure(self):
        self.lab.codes["xvlog"] = 1
        self.lab.outputs["xvlog"] = "ERROR: invalid syntax\n"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("failure_cause", result)
        self.assertNotIn("budget_exhausted", result)

    def test_version_probe_timeout_is_a_budget_failure_without_a_design_error(self):
        # A tool that starts and then eats the clock is not a missing tool: the
        # stage still counts as a failure, but the cause is the deadline.
        self.lab.timeout_key = "version"
        result = self.run_validation()
        self.assertEqual(self.lab.tools, ["version"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["highest_stage"], 0)
        self.assertEqual(result["failure_cause"], "tool_timeout")
        self.assertFalse(result["budget_exhausted"])
        self.assertIn("Vivado version check failed", result["feedback"])
        for stage in result["stages"]:
            self.assertEqual(set(stage), {"name", "status", "reason", "wall_s", "log"})

    def test_exhausted_deadline_is_reported_as_budget_exhaustion(self):
        # A synthetic clock models the real call sequence: every _run_command
        # reads monotonic() once for the deadline check and once for the wait
        # timeout, and xsim adds one more.  Steps stay small while elapsed time
        # is below `pivot`, then jump, so the first validation starves the slow
        # synthesis stage and the second starves the cheap compile stage.
        class SyntheticClock:
            def __init__(self, pivot, step, stall=1000.0):
                self.now = 0.0
                self.pivot = pivot
                self.step = step
                self.stall = stall

            def __call__(self):
                self.now += self.step if self.now < self.pivot else self.stall
                return self.now

        # Synthesis starves: this is the P6 failure mode.  It is counted as a
        # failure -- never as a passed stage, never silently dropped from the
        # denominator -- and labelled as a budget shortfall, not a design error.
        with patch.object(eda.time, "monotonic", side_effect=SyntheticClock(10.0, 1.0)):
            starved = self.run_validation()
        self.assertEqual(starved["status"], "failed")
        self.assertEqual(starved["highest_stage"], 2)
        self.assertEqual(starved["failure_cause"], "budget_exhausted")
        self.assertTrue(starved["budget_exhausted"])
        self.assertEqual(starved["stages"][2]["status"], "failed")
        self.assertIn("exceeded", starved["stages"][2]["reason"])
        self.assertNotIn("vivado", self.lab.tools)
        self.assertIn("deadline exhausted", Path(starved["stages"][2]["log"]).read_text(encoding='utf-8'))
        self.assertLess(starved["highest_stage"], 3)

        # A cheap stage can run out of budget too. It is still a counted failure,
        # but it must not claim that *synthesis* was the starved stage.
        with patch.object(eda.time, "monotonic", side_effect=SyntheticClock(250.0, 50.0, 1000.0)):
            starved = self.run_validation(timeout=320)
        self.assertEqual(starved["status"], "failed")
        self.assertEqual(starved["highest_stage"], 0)
        self.assertEqual(starved["failure_cause"], "budget_exhausted")
        self.assertFalse(starved["budget_exhausted"])
        self.assertEqual(starved["stages"][0]["status"], "failed")

    def test_frozen_stage_and_success_record_keys_are_not_extended(self):
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        for stage in result["stages"]:
            self.assertEqual(set(stage), {"name", "status", "reason", "wall_s", "log"})
        self.assertNotIn("failure_cause", result)
        self.assertNotIn("budget_exhausted", result)

    def test_exhausted_deadline_does_not_launch_tool(self):
        log = self.root / "deadline.log"
        with patch.object(eda.time, "monotonic", return_value=20):
            result = eda._run_command(["xvlog"], self.root, log, deadline=19)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_cause"], "budget_exhausted")
        self.assertEqual(self.lab.calls, [])

    def test_feedback_is_bounded_but_log_is_complete(self):
        self.lab.codes["xvlog"] = 1
        self.lab.outputs["xvlog"] = "first line\n" + "x" * 20000 + "\nlast failure detail\n"
        result = self.run_validation()
        self.assertLessEqual(len(result["feedback"]), 6000)
        self.assertIn("last failure detail", result["feedback"])
        self.assertIn("first line", Path(result["stages"][0]["log"]).read_text(encoding='utf-8'))

    def test_identifiers_cannot_inject_tcl(self):
        self.task["top"] = "counter}; exec touch pwned; #"
        result = self.run_validation()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.lab.calls, [])

    def test_generated_external_execution_and_includes_are_rejected(self):
        for source in (
            'module counter; initial $system("touch pwned"); endmodule',
            'import "DPI-C" function int run(); module counter; endmodule',
            '`include "/tmp/untrusted.sv"\nmodule counter; endmodule',
            '`define JOIN(a,b) a``b\nmodule counter; endmodule',
        ):
            with self.subTest(source=source):
                self.code.write_text(source, encoding='utf-8')
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
        self.assertEqual(self.lab.calls, [])

    def test_comments_do_not_trigger_external_call_filter(self):
        self.code.write_text('module counter; // $system("example")\n/* import "DPI-C" */ endmodule\n', encoding='utf-8')
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_generated_rtl_cannot_forge_marker_or_end_testbench(self):
        for call in (
            '$display("TB_PASS")', '$write("TB_PASS")', '$strobe("TB_PASS")',
            '$monitor("TB_PASS")', "$finish", "$stop", '$fatal(1, "stop")',
            '$fopen("arbitrary", "w")', '$fwrite(1, "TB_PASS")',
            '$readmemh("external", data)', "$assertoff", "$custom_pli_call()",
        ):
            with self.subTest(call=call):
                self.code.write_text(f"module counter; initial {call}; endmodule", encoding='utf-8')
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
                self.assertIn("first-release RTL subset", result["feedback"])
        self.assertEqual(self.lab.calls, [])

    def test_pure_system_functions_and_dollar_in_identifiers_are_supported(self):
        self.code.write_text("module counter; localparam W = $clog2(16); logic [W-1:0] count$display; endmodule", encoding='utf-8')
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_nonpositive_timeout_is_rejected(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                self.assertEqual(self.run_validation(timeout)["status"], "failed")
        self.assertEqual(self.lab.calls, [])

    def test_verilogeval_succeeds_without_custom_marker(self):
        self.use_verilogeval()
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["highest_stage"], 3)
        self.assertIn("VerilogEval v2", result["stages"][1]["reason"])

    def test_verilogeval_missing_summary_hint_completion_or_silent_exit_rejected(self):
        self.use_verilogeval()
        lines = VERILOGEVAL_OUTPUT.splitlines(keepends=True)
        outputs = ["", "Simulation finished normally\n"]
        for label in ("Mismatches:", "Hint: Total", "Simulation finished"):
            outputs.append("".join(line for line in lines if not line.startswith(label)))
        for output in outputs:
            with self.subTest(output=output):
                self.lab.calls.clear()
                self.lab.outputs["xsim"] = output
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["highest_stage"], 1)
                self.assertNotIn("vivado", self.lab.tools)

    def test_verilogeval_duplicate_or_ambiguous_summary_rejected(self):
        self.use_verilogeval()
        for extra in (
            "Mismatches: 0 in 20 samples", "Mismatches 0 in 20 samples",
            "mismatches: 0 in 20 samples", "Hint: Total mismatched samples is 0 out of 20 samples",
            "Hint Total mismatched samples is 0 out of 20 samples", "Simulation finished at 102 ps",
        ):
            with self.subTest(extra=extra):
                self.lab.outputs["xsim"] = VERILOGEVAL_OUTPUT + extra + "\n"
                self.assertEqual(self.run_validation()["status"], "failed")

    def test_verilogeval_zero_samples_and_inconsistent_sample_counts_rejected(self):
        self.use_verilogeval()
        for output in (
            VERILOGEVAL_OUTPUT.replace("20 samples", "0 samples"),
            VERILOGEVAL_OUTPUT.replace("in 20 samples", "in 21 samples"),
            VERILOGEVAL_OUTPUT.replace("out of 20", "out of 0"),
        ):
            with self.subTest(output=output):
                self.lab.outputs["xsim"] = output
                self.assertEqual(self.run_validation()["status"], "failed")

    def test_verilogeval_nonzero_unknown_negative_and_oversized_counts_rejected(self):
        self.use_verilogeval()
        mutations = (
            ("Mismatches: 0", "Mismatches: 1"),
            ("Mismatches: 0", "Mismatches: X"),
            ("Mismatches: 0", "Mismatches: -1"),
            ("is 0 out", "is x out"),
            ("is 0 out", "is 1 out"),
            ("20 samples", "X samples"),
            ("20 samples", "z samples"),
            ("20 samples", "-1 samples"),
            ("in 20 samples", "in " + "9" * 5000 + " samples"),
        )
        for old, new in mutations:
            with self.subTest(old=old, new=new[:50]):
                self.lab.outputs["xsim"] = VERILOGEVAL_OUTPUT.replace(old, new)
                self.assertEqual(self.run_validation()["status"], "failed")

    def test_verilogeval_timeout_rejected_even_with_passing_final_block(self):
        self.use_verilogeval()
        for timeout in ("TIMEOUT", "timeout", "ERROR: timeout"):
            with self.subTest(timeout=timeout):
                self.lab.outputs["xsim"] = timeout + "\n" + VERILOGEVAL_OUTPUT
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
                self.assertIn("TIMEOUT", result["stages"][1]["reason"])

    def test_verilogeval_error_fatal_and_first_mismatch_hint_rejected(self):
        self.use_verilogeval()
        for failure in (
            "ERROR: [XSIM 43-1] internal failure", "ERROR: 0 unexpected diagnostic",
            "FATAL_ERROR: stopped", "Fatal: assertion fired", "1 error", "test failed",
            "Hint: First mismatch occurred at time 20.",
            "Hint: Output 'zero' has 1 mismatches. First mismatch occurred at time 20.",
        ):
            with self.subTest(failure=failure):
                self.lab.outputs["xsim"] = failure + "\n" + VERILOGEVAL_OUTPUT
                self.assertEqual(self.run_validation()["status"], "failed")

    def test_verilogeval_completion_requires_original_protocol_format_and_order(self):
        self.use_verilogeval()
        for output in (
            VERILOGEVAL_OUTPUT.replace("102 ps", "X ps"),
            VERILOGEVAL_OUTPUT.replace("102 ps", "102 ns"),
            VERILOGEVAL_OUTPUT.replace("Mismatches:", "mismatches:"),
            "Mismatches: 0 in 20 samples\nHint: Total mismatched samples is 0 out of 20 samples\nSimulation finished at 102 ps\n",
        ):
            with self.subTest(output=output):
                self.lab.outputs["xsim"] = output
                self.assertEqual(self.run_validation()["status"], "failed")

    def test_unknown_protocol_rejected_before_tools(self):
        for protocol in ("verilogeval_v1", "accept_everything", "", None, 3):
            with self.subTest(protocol=protocol):
                self.task["simulation_protocol"] = protocol
                result = self.run_validation()
                self.assertEqual(result["status"], "failed")
                self.assertIn("simulation_protocol", result["feedback"])
        self.assertEqual(self.lab.calls, [])

    def test_explicit_marker_protocol_still_supported(self):
        self.task["simulation_protocol"] = "marker"
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_verilogeval_generated_harness_and_hierarchy_access_rejected(self):
        self.use_verilogeval()
        self.task["testbench_top"] = "counter_tb"
        for source in (
            "module tb; endmodule", "module RefModule; endmodule",
            "module stimulus_gen; endmodule", "module counter_tb; endmodule",
            "module counter; assign q = tb.zero_ref; endmodule",
            "module counter; assign q = good1.zero; endmodule",
            "module counter; assign q = g[0].zero; endmodule",
            "module counter; force valid = 1; endmodule",
            "module counter; release valid; endmodule",
            "module counter; bind Other monitor monitor_inst(); endmodule",
            "module counter; defparam Other.x = 1; endmodule",
            "module \\tb ; endmodule",
        ):
            with self.subTest(source=source):
                self.code.write_text(source, encoding='utf-8')
                self.assertEqual(self.run_validation()["status"], "failed")
        self.assertEqual(self.lab.calls, [])

    def test_verilogeval_named_ports_helpers_and_reserved_names_in_comments_allowed(self):
        self.use_verilogeval()
        self.code.write_text(
            "// tb, RefModule and good1.zero are comments only\n"
            "module helper(output q); assign q = 0; endmodule\n"
            "module counter(output q); helper u(.q(q)); endmodule\n"
        , encoding='utf-8')
        self.assertEqual(self.run_validation()["status"], "passed")

    def test_verilogeval_trusted_testbench_and_reference_copied_unchanged(self):
        self.use_verilogeval()
        original_tb = b'module tb; final $display("Mismatches: 0 in 20 samples"); endmodule\n'
        original_ref = b"module RefModule(output zero); assign zero = 0; endmodule\n"
        self.tb.write_bytes(original_tb)
        reference = self.root / "trusted_reference.sv"
        reference.write_bytes(original_ref)
        self.task["testbench_top"] = "tb"
        self.task["testbench_files"] = [str(self.tb), str(reference)]
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        run_dir = Path(result["stages"][0]["log"]).parent
        self.assertEqual((run_dir / "testbench_0.sv").read_bytes(), original_tb)
        self.assertEqual((run_dir / "testbench_1.sv").read_bytes(), original_ref)
        self.assertEqual(self.tb.read_bytes(), original_tb)
        self.assertEqual(reference.read_bytes(), original_ref)
        compile_args = next(args for args, _ in self.lab.calls if Path(args[0]).name == "xvlog")
        self.assertEqual(compile_args[1:], ["--sv", "design.sv", "testbench_0.sv", "testbench_1.sv"])

    def test_testbench_changed_after_snapshot_is_unavailable_and_never_compiles(self):
        """A testbench edited between task loading and EDA must not be evaluated."""
        import hashlib
        self.task["asset_sha256"] = {str(self.tb.resolve()):
                                     hashlib.sha256(self.tb.read_bytes()).hexdigest()}
        self.tb.write_text('module tb; initial $display("TB_PASS"); $finish; endmodule\n'
                           "// silently weakened after the snapshot\n", encoding="utf-8")
        result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["highest_stage"], 0)
        self.assertIn("asset_sha256", result["feedback"])
        self.assertEqual(self.lab.calls, [])

    def test_missing_testbench_recorded_in_snapshot_is_unavailable(self):
        import hashlib
        self.task["asset_sha256"] = {str(self.tb.resolve()): hashlib.sha256(b"x").hexdigest()}
        result = self.run_validation()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["highest_stage"], 0)
        self.assertEqual(self.lab.calls, [])

    def test_matching_snapshot_still_validates_and_copies_original_bytes(self):
        import hashlib
        self.task["asset_sha256"] = {str(self.tb.resolve()):
                                     hashlib.sha256(self.tb.read_bytes()).hexdigest()}
        result = self.run_validation()
        self.assertEqual(result["status"], "passed")
        run_dir = Path(result["stages"][0]["log"]).parent
        self.assertEqual((run_dir / "testbench_0.sv").read_bytes(), self.tb.read_bytes())

    def test_malformed_asset_snapshot_is_rejected_before_any_tool_call(self):
        for snapshot in ("not-a-dict", {"path": "short"}, {"path": 5}, [["path", "0" * 64]]):
            with self.subTest(snapshot=snapshot):
                self.lab.calls.clear()
                self.task["asset_sha256"] = snapshot
                result = self.run_validation()
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["highest_stage"], 0)
                self.assertIn("asset_sha256", result["feedback"])
                self.assertEqual(self.lab.calls, [])


if __name__ == "__main__":
    unittest.main()
