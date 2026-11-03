import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent.client import LocalClient, check_context
from agent.core import (SAMPLING_SEED_STRIDE, digest, load_config, load_task,
                        repair_feedback, run_generation, sampling_seed)
from scripts.metrics import summarize


CONFIG = {'endpoint':'http://127.0.0.1:8000/v1', 'model':'test', 'context_length':8192,
          'max_tokens':512, 'temperature':.2, 'top_p':.95, 'max_attempts':3,
          'run_timeout_s':10, 'tool_timeout_s':5}
TASK = {'task_id':'t', 'prompt':'Implement module top(input a, output y); assign y=a.', 'top':'top'}


class Client:
    def __init__(self, responses):
        self.provenance = LocalClient(CONFIG).provenance
        self.responses = iter(responses)
        self.requests = []

    def generate(self, messages, seed, timeout_s):
        self.requests.append(messages)
        return {'content':next(self.responses), 'usage':{}, 'mock':False}


def verdict(stage, status='failed'):
    return {'status':status, 'highest_stage':stage, 'stages':[], 'feedback':'width mismatch'}


class CoreTests(unittest.TestCase):
    def run_case(self, mode, client, validator=None, seed=7):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)/'run'
        result = run_generation(TASK, CONFIG, mode, client, path, seed, 'pair', validator=validator)
        return result, path

    def test_baseline_exact_prompt_no_tools_single_call(self):
        c = Client(['module top; endmodule'])
        r, _ = self.run_case('baseline', c, lambda *a:self.fail('Baseline invoked tool'))
        self.assertEqual(c.requests, [[{'role':'user', 'content':TASK['prompt']} ]])
        self.assertEqual(len(r['attempts']), 1)
        self.assertEqual(r['validation']['status'], 'unverified')

    def test_repair_and_best_candidate(self):
        c = Client(['module top; /*a*/ endmodule','module top; /*b*/ endmodule','module top; /*c*/ endmodule'])
        scores = iter([verdict(1), verdict(0), verdict(0)])
        r, path = self.run_case('agent', c, lambda *a:next(scores))
        self.assertEqual(r['selected_attempt'], 1)
        self.assertIn('/*a*/', (path/'design.sv').read_text(encoding='utf-8'))
        self.assertIn('width mismatch', c.requests[1][-1]['content'])

    def test_stop_on_pass(self):
        c = Client(['module top; endmodule'])
        r, _ = self.run_case('agent', c, lambda *a:verdict(3,'passed'))
        self.assertEqual(len(c.requests),1)
        self.assertEqual(r['validation']['highest_stage'],3)

    def test_reset_repair_keeps_specification_interface_and_timing_contract(self):
        task = dict(TASK, prompt='Implement top with synchronous active-low reset, reset before enable.')
        client = Client(['module top; endmodule', 'module top; wire fixed; endmodule'])
        scores = iter([dict(verdict(1), feedback='simulate: failed: reset changed q before a clock edge'),
                       verdict(3, 'passed')])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_generation(task, CONFIG, 'agent', client, Path(tmp)/'run', 0,
                                    validator=lambda *args: next(scores))
        self.assertEqual(result['stop_reason'], 'passed')
        for messages in client.requests:
            self.assertEqual(messages[1], {'role': 'user', 'content': task['prompt']})
            self.assertIn('only the requested clock edge', messages[0]['content'])
            self.assertIn('only when the specification requests an asynchronous reset', messages[0]['content'])
        repair = client.requests[1][-1]['content']
        self.assertIn('ports, widths, latency, and cycle-level behavior', repair)
        self.assertIn('reset type, active level, and priority', repair)
        self.assertIn('reset changed q before a clock edge', repair)
        self.assertNotIn('rewrite it from scratch', repair)

    def test_unavailable_is_not_pass(self):
        r, _ = self.run_case('agent', Client(['module top; endmodule']), lambda *a:verdict(0,'unavailable'))
        self.assertEqual(r['stop_reason'],'unavailable')

    def test_duplicate_candidate_stops(self):
        r, _ = self.run_case('agent', Client(['x','x']), lambda *a:verdict(0))
        self.assertEqual(r['stop_reason'],'repeated_candidate')

    def test_sampling_seeds_do_not_collide_across_independent_runs(self):
        class SeedClient(Client):
            def __init__(self, responses):
                super().__init__(responses)
                self.seeds = []
            def generate(self, messages, seed, timeout_s):
                self.seeds.append(seed)
                return super().generate(messages, seed, timeout_s)

        used = []
        for seed in range(5):
            client = SeedClient(['module top; /*%d-%d*/ endmodule' % (seed, index)
                                 for index in range(CONFIG['max_attempts'])])
            result, _ = self.run_case('agent', client, lambda *a: verdict(0), seed)
            self.assertEqual(result['stop_reason'], 'attempt_budget')
            self.assertEqual(len(set(client.seeds)), CONFIG['max_attempts'])
            self.assertEqual(result['sampling_seeds'], client.seeds)
            self.assertEqual([entry['seed'] for entry in result['attempts']], client.seeds)
            used.extend(client.seeds)
        self.assertEqual(len(set(used)), 5 * CONFIG['max_attempts'])

    def test_sampling_seed_keying_is_injective_over_supported_seeds(self):
        keys = {sampling_seed(seed, attempt)
                for seed in range(100) for attempt in range(1, SAMPLING_SEED_STRIDE + 1)}
        self.assertEqual(len(keys), 100 * SAMPLING_SEED_STRIDE)
        self.assertEqual(sampling_seed(0, 1), 0)
        self.assertEqual(sampling_seed(1, 1), SAMPLING_SEED_STRIDE)

    def test_attempt_budget_cannot_exceed_the_seed_stride(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            original = CONFIG['max_attempts']
            try:
                CONFIG['max_attempts'] = SAMPLING_SEED_STRIDE + 1
                path.write_text(json.dumps(CONFIG), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'sampling seed stride'):
                    load_config(path)
            finally:
                CONFIG['max_attempts'] = original

    def test_duplicate_candidate_keeps_usage_without_repeating_eda(self):
        class CountedClient(Client):
            def generate(self, *args):
                answer = super().generate(*args)
                answer.update(usage={'completion_tokens': len(self.requests) * 10}, finish_reason='stop')
                return answer

        validations = []

        def validate(*args):
            validations.append(args)
            return verdict(1)

        r, path = self.run_case('agent', CountedClient(['x', 'x']), validate)
        self.assertEqual(len(validations), 1)
        self.assertEqual(len(r['attempts']), 2)
        self.assertEqual(r['attempts'][1]['duplicate_of_attempt'], 1)
        self.assertEqual(r['attempts'][1]['usage'], {'completion_tokens': 20})
        self.assertEqual(r['attempts'][1]['finish_reason'], 'stop')
        self.assertEqual(r['attempts'][1]['validation']['status'], 'unverified')
        self.assertEqual(r['selected_attempt'], 1)
        self.assertEqual(r['validation']['highest_stage'], 1)
        self.assertTrue((path / 'attempt_02' / 'response.json').is_file())
        self.assertFalse((path / 'attempt_02' / 'eda').exists())

    def test_long_repair_feedback_preserves_stage_reason_and_recent_diagnostic(self):
        feedback = 'compile: failed: unknown signal enable\n' + ('unhelpful log data\n' * 400) + 'final diagnostic line'
        client = Client(['module top; endmodule', 'module top; wire enable; endmodule'])
        failed = dict(verdict(0), feedback=feedback)
        scores = iter([failed, verdict(3, 'passed')])
        r, _ = self.run_case('agent', client, lambda *args: next(scores))
        diagnostic = client.requests[1][-1]['content'].split('\n', 1)[1]
        self.assertEqual(r['stop_reason'], 'passed')
        self.assertLessEqual(len(diagnostic), 2000)
        self.assertTrue(diagnostic.startswith('compile: failed: unknown signal enable'))
        self.assertTrue(diagnostic.endswith('final diagnostic line'))
        self.assertIn('feedback truncated', diagnostic)
        self.assertEqual(client.requests[1][1]['content'], TASK['prompt'])
        self.assertEqual(repair_feedback('short feedback'), 'short feedback')

    def test_invalid_generation_mode_rejected_before_artifacts_or_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            client = Mock()
            for mode in ('pair', 'agnt', None):
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'Generation mode'):
                    run_generation(TASK, CONFIG, mode, client, output, 0)
            self.assertFalse(output.exists())
            client.generate.assert_not_called()

    def test_empty_completed_baseline_is_one_failed_sample_in_metrics(self):
        for content in ('', '  ', None, [], {'text': 'module top; endmodule'}, '```verilog\n```', '```sv\n  \n```'):
            with self.subTest(content=content):
                client = Client([content])
                result, path = self.run_case('baseline', client, lambda *args: self.fail('Baseline used EDA'))
                self.assertEqual(result['validation']['status'], 'failed')
                self.assertEqual(result['validation']['highest_stage'], 0)
                self.assertEqual(result['validation']['stage_evidence_mode'], 'output_rejected')
                self.assertEqual(result['stop_reason'], 'output_format_rejected')
                self.assertNotIn('error', result)
                self.assertEqual(len(result['attempts']), 1)
                self.assertEqual(len(client.requests), 1)
                self.assertFalse((path / 'design.sv').exists())
                self.assertEqual(summarize([path])['input']['eligible_runs'], 1)

    def test_agent_repairs_empty_response_with_feedback_and_preserved_usage(self):
        class MeteredClient(Client):
            def generate(self, *args):
                answer = super().generate(*args)
                answer.update(usage={'completion_tokens': 10}, finish_reason='stop')
                return answer

        for content in ('', None, {'text': 'unsupported'}, '```systemverilog\n```'):
            with self.subTest(content=content):
                client = MeteredClient([content, 'module top; endmodule'])
                validator = Mock(return_value=verdict(3, 'passed'))
                result, path = self.run_case('agent', client, validator)
                self.assertEqual(result['selected_attempt'], 2)
                self.assertEqual(result['validation']['status'], 'passed')
                self.assertEqual(result['attempts'][0]['usage'], {'completion_tokens': 10})
                self.assertEqual(result['attempts'][0]['finish_reason'], 'stop')
                self.assertEqual(result['attempts'][0]['validation']['status'], 'failed')
                self.assertIn('Output format rejected', client.requests[1][-1]['content'])
                self.assertIsInstance(client.requests[1][-2]['content'], str)
                self.assertTrue((path / 'design.sv').is_file())
                validator.assert_called_once()

    def test_empty_repairs_cannot_replace_best_verified_candidate(self):
        validator = Mock(return_value=verdict(2))
        result, path = self.run_case('agent', Client(['module top; endmodule', '', '```sv\n```']), validator)
        self.assertEqual(result['selected_attempt'], 1)
        self.assertEqual(result['validation']['highest_stage'], 2)
        self.assertEqual(result['stop_reason'], 'attempt_budget')
        self.assertEqual(len(result['attempts']), 3)
        self.assertEqual((path / 'design.sv').read_text(encoding='utf-8'), 'module top; endmodule\n')
        validator.assert_called_once()

    def test_empty_mock_response_stays_mock_and_never_runs_eda(self):
        client = Mock()
        client.generate.return_value = {'content': '', 'mock': True, 'usage': {'completion_tokens': 2}, 'finish_reason': 'stop'}
        result, path = self.run_case('agent', client, lambda *args: self.fail('Mock used EDA'))
        self.assertEqual(result['evaluation_kind'], 'mock')
        self.assertEqual(result['validation']['status'], 'failed')
        self.assertEqual(result['stop_reason'], 'output_format_rejected')
        self.assertEqual(result['attempts'][0]['usage'], {'completion_tokens': 2})
        self.assertEqual(summarize([path])['input']['eligible_runs'], 0)
        client.generate.assert_called_once()

    def test_rtl_track_defaults_and_cross_track_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path = Path(tmp) / 'task.json'
            config_path = Path(tmp) / 'config.json'
            task_path.write_text(json.dumps(TASK), encoding='utf-8')
            config_path.write_text(json.dumps(CONFIG), encoding='utf-8')
            self.assertEqual(load_task(task_path)['track'], 'rtl')
            self.assertEqual(load_config(config_path), CONFIG)
            for track in ('hls', None, ''):
                with self.subTest(track=track):
                    task_path.write_text(json.dumps(dict(TASK, track=track)), encoding='utf-8')
                    config_path.write_text(json.dumps(dict(CONFIG, track=track)), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'RTL'):
                        load_task(task_path)
                    with self.assertRaisesRegex(ValueError, 'RTL'):
                        load_config(config_path)
            for task, config in ((dict(TASK, track='hls'), CONFIG), (TASK, dict(CONFIG, track='hls'))):
                output = Path(tmp) / 'rejected'
                with self.assertRaisesRegex(ValueError, 'RTL'):
                    run_generation(task, config, 'agent', Client([]), output, 0)
                self.assertFalse(output.exists())
        result, _ = self.run_case('baseline', Client(['module top; endmodule']))
        self.assertEqual(result['track'], 'rtl')

    def test_errors_recorded_and_existing_run_not_overwritten(self):
        c = Client([])
        r, path = self.run_case('agent', c)
        self.assertIn('error',r)
        self.assertTrue((path/'result.json').exists())
        with self.assertRaises(FileExistsError):
            run_generation(TASK, CONFIG, 'baseline',c,path,0)

    def test_context_guard_does_not_truncate(self):
        with self.assertRaises(ValueError):
            check_context([{'role':'user','content':'x'*10000}], CONFIG)

    def test_cloud_endpoint_rejected(self):
        for endpoint in ('https://example.com/v1','http://localhost.example.com/v1','http://127.0.0.1@evil.test/v1'):
            with self.assertRaises(ValueError):
                LocalClient(dict(CONFIG,endpoint=endpoint))

    def test_mock_response_skips_validator_and_stays_mock(self):
        class MockClient:
            def generate(self, *args):
                return {'content':'module top; endmodule', 'mock':True}
        result, _ = self.run_case('agent',MockClient(),lambda *a:self.fail('Mock inference dispatched EDA'))
        self.assertEqual(result['evaluation_kind'],'mock')
        self.assertEqual(result['validation']['status'],'unverified')

    def test_testbench_content_changes_task_identity_and_declared_hash_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            task_path, tb = tmp / 'task.json', tmp / 'counter_tb.sv'
            tb.write_text('module tb; initial $display("RTL_TEST_PASS:counter"); endmodule\n', encoding='utf-8')
            task = dict(TASK, testbench_files=['counter_tb.sv'], testbench_top='tb')
            task_path.write_text(json.dumps(task), encoding='utf-8')
            original = load_task(task_path)
            self.assertEqual(original['testbench_files'], [str(tb.resolve())])
            self.assertEqual(original['asset_sha256'],
                             {str(tb.resolve()): hashlib.sha256(tb.read_bytes()).hexdigest()})
            # Pathnames alone would leave the digest unchanged; the bytes must not.
            tb.write_text('module tb; initial $display("RTL_TEST_PASS:counter"); endmodule\n'
                          '// weakened assertion\n', encoding='utf-8')
            changed = load_task(task_path)
            self.assertNotEqual(digest(original), digest(changed))
            task['source_sha256'] = {'upstream/counter_tb.sv':
                                     original['asset_sha256'][str(tb.resolve())]}
            task_path.write_text(json.dumps(task), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'differs from declared'):
                load_task(task_path)
            # Restoring the exact bytes restores the asset snapshot. The declared
            # source_sha256 stays in the task file, so compare the asset binding
            # rather than the whole task digest.
            tb.write_text('module tb; initial $display("RTL_TEST_PASS:counter"); endmodule\n', encoding='utf-8')
            restored = load_task(task_path)
            self.assertEqual(restored['asset_sha256'], original['asset_sha256'])
            self.assertEqual(restored['testbench_files'], original['testbench_files'])

    def test_eda_budget_does_not_trigger_a_design_repair(self):
        client = Client(['module top; endmodule', 'module top; wire a; endmodule'])
        validation = dict(verdict(2), failure_cause='tool_timeout', budget_exhausted=True)
        result, _ = self.run_case('agent', client, lambda *args: validation)
        self.assertEqual(result['stop_reason'], 'eda_budget')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result['validation'], validation)

    def test_clock_inference_respects_explicit_task_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'task.json'
            value = dict(TASK, prompt=' - input sys_clk\n - output q\n')
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(load_task(path)['clock_port'], 'sys_clk')
            value['clock_port'] = None
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertIsNone(load_task(path)['clock_port'])

    def test_missing_testbench_is_left_to_the_validator_not_silently_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path = Path(tmp) / 'task.json'
            task_path.write_text(json.dumps(dict(TASK, testbench_files=['absent_tb.sv'])),
                                 encoding='utf-8')
            task = load_task(task_path)
            self.assertEqual(task['asset_sha256'], {})
            self.assertEqual(len(task['testbench_files']), 1)


if __name__ == '__main__':
    unittest.main()
