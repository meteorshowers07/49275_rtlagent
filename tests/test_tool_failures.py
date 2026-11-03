"""Tool failure routing regressions, using simulated processes (not real Vivado)."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent import eda
from agent.core import run_generation
from tests import test_eda as fixtures
from tests.test_core import Client, CONFIG, TASK, verdict
from scripts.metrics import summarize


class ToolFailureTests(unittest.TestCase):
    setUp = fixtures.EdaTests.setUp
    run_validation = fixtures.EdaTests.run_validation

    def test_crash_codes_keep_failed_sample_prefix_and_raw_exit(self):
        for code in (3221226356, -1073740940, 0xC0000005, -11, -6, 139, 134):
            with self.subTest(code=code):
                self.lab.codes['vivado'] = code
                result = self.run_validation()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['highest_stage'], 2)
                self.assertEqual(result['failure_cause'], 'tool_crash')
                self.assertNotIn('budget_exhausted', result)
                self.assertEqual([s['status'] for s in result['stages']], ['passed', 'passed', 'failed'])
                log = Path(result['stages'][2]['log']).read_text(encoding='utf-8')
                self.assertIn(f'EXIT_CODE {code}', log)
                self.assertIn(str(code), result['feedback'])

    def test_child_crash_diagnostic_overrides_wrapper_success_and_artifacts(self):
        for code in (0, 1):
            for diagnostic in ('Abnormal program termination (EXCEPTION_ACCESS_VIOLATION)',
                               'Segmentation fault (core dumped)', 'Internal compiler error: unexpected state'):
                with self.subTest(code=code, diagnostic=diagnostic):
                    self.lab.codes['vivado'] = code
                    self.lab.outputs['vivado'] = diagnostic + '\n'
                    result = self.run_validation()
                    self.assertEqual(result['failure_cause'], 'tool_crash')
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['highest_stage'], 2)

    def test_ordinary_design_error_does_not_become_a_crash(self):
        for code in (0, 1, 2):
            with self.subTest(code=code):
                self.lab.codes['vivado'] = code
                self.lab.outputs['vivado'] = 'ERROR: [Synth 8-6156] failed synthesizing module\n'
                result = self.run_validation()
                self.assertEqual(result['status'], 'failed')
                self.assertNotIn('failure_cause', result)
                self.assertNotIn('budget_exhausted', result)
        self.assertFalse(eda._tool_crashed(0, "INFO: synthesizing module 'STATUS_HEAP_CORRUPTION'\n"))
        self.assertFalse(eda._tool_crashed(0, "INFO: synthesizing module 'EXCEPTION_ACCESS_VIOLATION'\n"))
        self.assertFalse(eda._tool_crashed(1, 'COMMAND ["STATUS_HEAP_CORRUPTION"]\nERROR: signal segmentation_fault not declared\n'))

    def test_confirmed_missing_target_part_stops_repair_and_keeps_simulation(self):
        self.lab.codes['vivado'] = 1
        self.lab.outputs['vivado'] = (
            "WARNING: [Device 21-436] No parts matched 'xczu3eg-sbva484-1-e'\n"
            "ERROR: [Common 17-162] Invalid option value specified for '-part'.\n")
        client = Client(['module counter(input clk); endmodule'])
        result = self.generate(client, eda.validate_rtl)
        self.assertEqual(result['stop_reason'], 'unavailable')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result['validation']['status'], 'unavailable')
        self.assertEqual(result['validation']['highest_stage'], 2)
        self.assertEqual(result['validation']['failure_cause'], 'tool_environment')
        self.assertNotIn('budget_exhausted', result['validation'])

    def test_missing_part_requires_both_exact_diagnostics_and_never_overrides_crash(self):
        missing = "WARNING: [Device 21-436] No parts matched 'xczu3eg-sbva484-1-e'\n"
        invalid = "ERROR: [Common 17-162] Invalid option value specified for '-part'.\n"
        for output in (missing, invalid, (missing + invalid).replace('xczu3eg', 'other'),
                       'INFO: ' + missing + invalid):
            with self.subTest(output=output):
                self.assertFalse(eda._missing_target_part(output))
        self.lab.outputs['vivado'] = missing + invalid
        self.lab.codes['vivado'] = 3221226356
        result = self.run_validation()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_cause'], 'tool_crash')

    def test_crash_at_each_earlier_stage_stops_tools(self):
        order = ['version', 'xvlog', 'xelab', 'xsim']
        for index, tool in enumerate(order):
            with self.subTest(tool=tool):
                self.lab.calls.clear()
                self.lab.codes = {tool: 3221226356}
                result = self.run_validation()
                self.assertEqual(result['failure_cause'], 'tool_crash')
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['highest_stage'], 1 if tool == 'xsim' else 0)
                self.assertEqual(self.lab.tools, order[:index + 1])

    def test_launcher_oserror_is_a_tool_failure(self):
        with patch.object(eda, '_launch_tool', side_effect=OSError('launcher missing')):
            result = self.run_validation()
        self.assertEqual(result['failure_cause'], 'tool_start_error')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('budget_exhausted', result)

    def generate(self, client, validator, name='run'):
        return run_generation(dict(TASK, **self.task), CONFIG, 'agent', client,
                              self.root / name, 7, validator=validator)

    def test_end_to_end_crash_stops_model_and_preserves_evidence_and_statistics(self):
        self.lab.codes['vivado'] = 3221226356
        client = Client(['module counter(input clk); endmodule'])
        result = self.generate(client, eda.validate_rtl)
        self.assertEqual(result['stop_reason'], 'eda_tool_error')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['validation']['highest_stage'], 2)
        self.assertEqual(result['validation']['status'], 'failed')
        self.assertIn('root cause', result['diagnostic_hint'])
        saved = json.loads((self.root / 'run/result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, result)
        report = summarize([self.root / 'run/result.json'])
        # A tool crash remains a counted real failure, with compile/simulate credit.
        self.assertEqual(report['overall']['n'], 1)
        self.assertEqual(report['overall']['stages']['3']['c'], 0)
        self.assertEqual(report['overall']['stages']['2']['c'], 1)

    def test_ordinary_synthesis_failure_still_requests_code_repair(self):
        client = Client(['module counter; endmodule', 'module counter; wire a; endmodule'])
        results = iter([verdict(2), verdict(3, 'passed')])
        result = self.generate(client, lambda *args: next(results))
        self.assertEqual(result['stop_reason'], 'passed')
        self.assertEqual(len(client.requests), 2)
        self.assertIn('Repair the RTL', client.requests[1][-1]['content'])

    def test_crash_after_a_repair_keeps_best_candidate_and_attempt_failure(self):
        client = Client(['module counter; endmodule', 'module counter; wire a; endmodule'])
        first = verdict(2)
        crash = dict(verdict(0), failure_cause='tool_crash')
        results = iter([first, crash])
        result = self.generate(client, lambda *args: next(results))
        self.assertEqual(result['stop_reason'], 'eda_tool_error')
        self.assertEqual(result['selected_attempt'], 1)
        self.assertEqual(result['validation'], first)
        self.assertEqual(result['attempts'][1]['validation'], crash)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual((self.root / 'run/design.sv').read_text(encoding='utf-8'), 'module counter; endmodule\n')

    def test_launcher_failure_stops_model_repairs(self):
        client = Client(['module counter; endmodule'])
        with patch.object(eda, '_launch_tool', side_effect=OSError('launcher missing')):
            result = self.generate(client, eda.validate_rtl)
        self.assertEqual(result['stop_reason'], 'eda_tool_error')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result['validation']['failure_cause'], 'tool_start_error')

    def test_crash_tied_with_best_does_not_replace_earlier_candidate(self):
        client = Client(['module counter; endmodule', 'module counter; wire a; endmodule'])
        first = verdict(2)
        crash = dict(verdict(2), failure_cause='tool_crash')
        results = iter([first, crash])
        result = self.generate(client, lambda *args: next(results))
        self.assertEqual(result['selected_attempt'], 1)
        self.assertEqual(result['validation'], first)
        self.assertEqual(result['attempts'][1]['validation'], crash)
        self.assertEqual(result['stop_reason'], 'eda_tool_error')
