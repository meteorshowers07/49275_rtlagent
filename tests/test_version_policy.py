"""Tool identity/policy regressions; injected tools are not real EDA evidence."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from agent import eda, tool_versions
from scripts import doctor
import test_eda as fixture

HLS = hasattr(eda, 'validate_hls')
PRODUCT = 'vitis' if HLS else 'vivado'
LABEL = 'Vitis HLS' if HLS else 'Vivado'
VARIABLE = 'HLS_AGENT_VITIS_VERSION' if HLS else 'RTL_AGENT_VIVADO_VERSION'

class VersionPolicyTests(unittest.TestCase):
    def setUp(self):
        fixture.EdaTests.setUp(self)
        env = patch.dict(os.environ, {VARIABLE: '2025.2'})
        env.start()
        self.addCleanup(env.stop)

    def validate(self):
        if HLS:
            return fixture.EdaTests.validate(self)
        return fixture.EdaTests.run_validation(self)

    def test_default_and_override_are_explicit_and_invalid_values_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(eda.required_tool_version(), '2025.2')
        for value in ('2025.2',):
            with patch.dict(os.environ, {VARIABLE: value}):
                self.assertEqual(eda.required_tool_version(), value)
        for value in ('', '2099.9', '2025.20', '2025.2preview', '*', '2024.2'):
            with self.subTest(value=value), patch.dict(os.environ, {VARIABLE: value}):
                self.lab.calls.clear()
                result = self.validate()
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(self.lab.calls, [])

    def test_banner_identity_rejects_wrong_product_embedded_and_ambiguous_versions(self):
        for output in (f'INFO: requires {LABEL} v2025.2',
                       f'COMMAND ["/{LABEL}/2025.2/bin/tool"]',
                       f'{LABEL} v2025.2preview',
                       f'{LABEL} v2025.2\n{LABEL} v2024.2',
                       ('Vivado' if HLS else 'Vitis')+' v2025.2'):
            with self.subTest(output=output):
                self.assertIsNone(tool_versions.reported_version(output, PRODUCT))
        self.assertEqual(tool_versions.reported_version(f'{LABEL} v2025.20', PRODUCT), '2025.20')
        self.assertEqual(tool_versions.reported_version(f'****** {LABEL} v2025.2.1 (64-bit)\n', PRODUCT), '2025.2')

    def test_correct_version_is_written_to_stage_feedback_and_original_log(self):
        with patch.dict(os.environ, {VARIABLE:'2025.2'}):
            self.lab.outputs['version'] = f'****** {LABEL} v2025.2 (64-bit)\n'
            result = self.validate()
        self.assertEqual(result['status'], 'passed')
        self.assertIn('2025.2', result['feedback'])
        self.assertIn('2025.2', result['stages'][0 if not HLS else 1]['reason'])
        log = Path(result['stages'][0]['log']).parent/'version.log'
        self.assertIn('v2025.2', log.read_text(encoding='utf-8'))

    def test_unselected_version_is_unavailable_and_never_compiles(self):
        self.lab.outputs['version'] = f'{LABEL} v2024.2 (64-bit)\n'
        result = self.validate()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['highest_stage'], 0)
        self.assertEqual(len(self.lab.calls), 1)
        self.assertIn('2024.2', result['feedback'])

    def test_wrapper_nonzero_exception_does_not_admit_arbitrary_failure_codes(self):
        for code in (1,2,3221226356):
            with self.subTest(code=code):
                self.lab.calls.clear()
                self.lab.codes['version'] = code
                result = self.validate()
                admitted = not HLS and code == 1
                self.assertEqual(result['status'], 'passed' if admitted else ('unavailable' if HLS and code in (1,2) else 'failed'))
                if not admitted:
                    self.assertEqual(len(self.lab.calls), 1)

    def test_error_banner_never_passes(self):
        self.lab.outputs['version'] += 'ERROR: runtime unavailable\n'
        result = self.validate()
        self.assertNotEqual(result['status'], 'passed')
        self.assertEqual(len(self.lab.calls), 1)

    def test_doctor_and_evaluator_agree_on_selected_release_and_launcher_exit(self):
        runner = 'managed_run' if HLS else 'subprocess.run'
        for selected, actual, code in (('2025.2','2025.2',0), ('2025.2','2024.2',0),
                                       ('2025.2','2025.2',1), ('2025.2','2025.2',2)):
            with self.subTest(selected=selected,actual=actual,code=code), patch.dict(os.environ,{VARIABLE:selected}):
                self.lab.calls.clear()
                self.lab.outputs['version'] = f'{LABEL} v{actual} (64-bit)\n'
                self.lab.codes['version'] = code
                validation = self.validate()
                outcome = subprocess.CompletedProcess([], code, self.lab.outputs['version'], '')
                with patch('scripts.doctor.'+runner, return_value=outcome) as call:
                    checks = doctor.collect_checks()
                self.assertEqual(checks['tool_version_verified'], validation['status']=='passed')
                self.assertEqual(checks['reported_tool_version'], actual)
                self.assertEqual(call.call_args.args[0][0], checks['tools']['vitis-run' if HLS else 'vivado'])
                self.assertEqual(checks['required_tool_version'], selected)

    def test_longer_numeric_version_is_not_a_prefix_match(self):
        self.lab.outputs['version'] = f'{LABEL} v2025.20 (64-bit)\n'
        result = self.validate()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(len(self.lab.calls), 1)


class FixtureEnvironmentTests(unittest.TestCase):
    def test_fixture_restores_external_version_even_after_failure(self):
        for outer in (None, '2025.2', 'unsupported-release'):
            with self.subTest(outer=outer), patch.dict(os.environ):
                os.environ.pop(VARIABLE, None)
                if outer is not None:
                    os.environ[VARIABLE] = outer
                probe = unittest.TestCase()
                try:
                    fixture.EdaTests.setUp(probe)
                    self.assertEqual(os.environ[VARIABLE], '2025.2')
                    self.assertEqual(eda.required_tool_version(), '2025.2')
                finally:
                    probe.doCleanups()
                self.assertEqual(os.environ.get(VARIABLE), outer)
