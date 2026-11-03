"""Opt-in real Vivado mutation tests for the trusted counter reset testbench.

Set RTL_AGENT_RUN_VIVADO_TESTS=1 with one Vivado installation's bin on PATH.
Handwritten fixtures test the harness, not model quality; synthesis is separate.
"""
import os
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from agent.processes import managed_run


ROOT = Path(__file__).resolve().parents[1]
GOOD = """`timescale 1ns/1ps
module counter(input logic clk, rst, en, output logic [3:0] q);
always_ff @(posedge clk)
  if (rst) q <= 4'd0;
  else if (en) q <= q + 4'd1;
endmodule
"""


@unittest.skipUnless(os.environ.get('RTL_AGENT_RUN_VIVADO_TESTS') == '1',
                     'opt-in real Vivado reset mutation tests')
class ResetSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = {name: shutil.which(name) for name in ('xvlog', 'xelab', 'xsim')}
        if not all(cls.tools.values()):
            raise RuntimeError('Requested real reset tests require xvlog, xelab and xsim on PATH')
        if len({Path(path).parent.resolve() for path in cls.tools.values()}) != 1:
            raise RuntimeError('Reset tests require a single Vivado bin directory')

    def simulate(self, source):
        evidence = os.environ.get('RTL_AGENT_RESET_EVIDENCE_DIR')
        if evidence:
            directory = Path(evidence) / self.id().split('.')[-1]
            directory.mkdir(parents=True, exist_ok=False)
            context = nullcontext(str(directory))
        else:
            context = tempfile.TemporaryDirectory()
        with context as tmp:
            run = Path(tmp)
            (run/'design.sv').write_text(source, encoding='utf-8')
            shutil.copyfile(ROOT/'examples/counter_tb.sv', run/'counter_tb.sv')
            commands = ([self.tools['xvlog'], '--sv', 'design.sv', 'counter_tb.sv'],
                        [self.tools['xelab'], 'counter_tb', '-s', 'reset_test', '-timescale', '1ps/1ps'])
            records = []
            for index, command in enumerate(commands):
                result = managed_run(command, cwd=run, capture_output=True, timeout=60)
                output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
                records.append(dict(command=command, returncode=result.returncode))
                if evidence:
                    (run / ('compile_' + str(index) + '.log')).write_text(output, encoding='utf-8')
                    (run / 'commands.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
                self.assertEqual(result.returncode, 0, output)
            command = [self.tools['xsim'], 'reset_test', '-runall']
            result = managed_run(command,
                                 cwd=run, capture_output=True, timeout=30)
            output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            if evidence:
                (run / 'simulation.log').write_text(output, encoding='utf-8')
                records.append(dict(command=command, returncode=result.returncode))
                (run / 'commands.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
            return result.returncode, output

    def test_correct_synchronous_reset_passes(self):
        code, output = self.simulate(GOOD)
        self.assertEqual(code, 0, output)
        self.assertIn('RTL_TEST_PASS:counter', output)
        self.assertNotIn('Fatal:', output)

    def test_asynchronous_reset_is_rejected_before_clock_edge(self):
        _, output = self.simulate(GOOD.replace('@(posedge clk)', '@(posedge clk or posedge rst)'))
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('reset is asynchronous: q changed before a clock edge', output)

    def test_inverted_reset_polarity_is_rejected(self):
        _, output = self.simulate(GOOD.replace('if (rst)', 'if (!rst)'))
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('reset must set q to zero', output)

    def test_enable_gating_reset_is_rejected(self):
        _, output = self.simulate(GOOD.replace("if (rst) q <= 4'd0;\n  else if (en) q <= q + 4'd1;",
                                              "if (en) begin if (rst) q <= 4'd0; else q <= q + 4'd1; end"))
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('reset must set q to zero', output)

    def test_enable_priority_over_reset_is_rejected(self):
        _, output = self.simulate(GOOD.replace("if (rst) q <= 4'd0;\n  else if (en) q <= q + 4'd1;",
                                              "if (en) q <= q + 4'd1; else if (rst) q <= 4'd0;"))
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('mismatch cycle=', output)

    def test_broken_hold_behavior_is_rejected(self):
        _, output = self.simulate(GOOD.replace("else if (en) q <= q + 4'd1;", "else q <= q + 4'd1;"))
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('synchronous reset pulse between edges must not change q', output)

    def test_missing_design_timescale_uses_xelab_default(self):
        code, output = self.simulate(GOOD.replace('`timescale 1ns/1ps\n', ''))
        self.assertEqual(code, 0, output)
        self.assertIn('RTL_TEST_PASS:counter', output)

    def test_explicit_timeunit_preserved(self):
        source = GOOD.replace('`timescale 1ns/1ps\n', '').replace('always_ff', 'timeunit 1ns; timeprecision 1ps;\nalways_ff')
        code, output = self.simulate(source)
        self.assertEqual(code, 0, output)
        self.assertIn('RTL_TEST_PASS:counter', output)

    def test_missing_timescale_does_not_hide_async_reset(self):
        source = GOOD.replace('`timescale 1ns/1ps\n', '').replace('@(posedge clk)', '@(posedge clk or posedge rst)')
        _, output = self.simulate(source)
        self.assertNotIn('RTL_TEST_PASS:counter', output)
        self.assertIn('reset is asynchronous', output)
