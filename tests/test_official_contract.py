"""Protocol/I/O tests using synthetic outputs; never model or EDA capability evidence."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent import official_io as io

TRACK = json.loads((io.ROOT / 'manifest.json').read_text(encoding='utf-8'))['track']
NAME = 'solution.cpp' if TRACK == 'hls' else 'solution.v'
CODE = ('#include "sum.h"\nint sum(int a, int b) { return a+b; }\n' if TRACK == 'hls'
        else 'module TopModule(input a, input b, output y); assign y = a & b; endmodule\n')
INTERFACE = '// header: sum.h\n#ifndef SUM_H\n#define SUM_H\nint sum(int a, int b);\n#endif\n' if TRACK == 'hls' else ''


class Client:
    def __init__(self, codes):
        self.codes = iter(codes)
        self.calls = 0

    def generate(self, messages, seed, timeout_s):
        self.calls += 1
        return dict(content=next(self.codes), usage=dict(prompt_tokens=10, completion_tokens=20))


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inp, self.out = self.root / 'in', self.root / 'out'
        self.inp.mkdir()
        self.out.mkdir()
        (self.inp / 'prompt.txt').write_text('Implement the specified interface.\nmodule TopModule', encoding='utf-8')
        (self.inp / 'interface.txt').write_text(INTERFACE, encoding='utf-8')

    def trace(self):
        return [json.loads(s) for s in (self.out / 'trace.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_existing_output_header_and_unverified_functionality(self):
        client = Client([CODE])
        with patch.dict(os.environ, {'AGENT_DEADLINE_S': '60'}):
            io.solve(self.inp, self.out, client, lambda *args: ('passed', 2, 'synthesis only'))
        self.assertEqual((self.out / NAME).read_text(encoding='utf-8'), CODE)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.trace()[-1]['functional_verification'], 'unverified')
        if TRACK == 'hls':
            self.assertEqual((self.out / 'sum.h').read_text(encoding='utf-8'), INTERFACE)

    def test_missing_input_clears_stale_result_and_returns_normal_empty_solution(self):
        (self.out / NAME).write_text('stale', encoding='utf-8')
        (self.inp / 'prompt.txt').unlink()
        self.assertEqual(io.solve(self.inp, self.out, Client([CODE])), 0)
        self.assertEqual((self.out / NAME).read_text(encoding='utf-8'), '')
        self.assertTrue(any(row.get('event') == 'error' for row in self.trace()))

    def test_short_budget_does_not_start_inference(self):
        client = Client([CODE])
        with patch.dict(os.environ, {'AGENT_DEADLINE_S': '1'}):
            io.solve(self.inp, self.out, client)
        self.assertEqual(client.calls, 0)
        self.assertEqual((self.out / NAME).read_text(encoding='utf-8'), '')

    def test_worse_repair_does_not_replace_better_candidate(self):
        client = Client([CODE, CODE + '\n// repair\n'])
        outcomes = iter([('failed', 1, 'design failure'), ('unavailable', 0, 'tool crash')])
        io.solve(self.inp, self.out, client, lambda *args: next(outcomes))
        self.assertEqual(client.calls, 2)
        self.assertEqual((self.out / NAME).read_text(encoding='utf-8'), CODE)

    def test_header_path_cannot_escape_output(self):
        with self.assertRaises(ValueError):
            io.split_interface('// header: ../escape.h\nvoid x();\n')

    @unittest.skipIf(TRACK == 'hls', 'RTL module interface regression')
    def test_corrected_interface_supersedes_rejected_fallback_when_eda_is_unavailable(self):
        bad = CODE.replace('TopModule', 'WrongModule')
        (self.inp / 'interface.txt').write_text('module TopModule(input a, input b, output y);', encoding='utf-8')
        io.solve(self.inp, self.out, Client([bad, CODE]), lambda *args: ('unavailable', 0, 'no tools'))
        self.assertEqual((self.out / NAME).read_text(encoding='utf-8'), CODE)

    def test_official_baseline_bytes_match_vendored_hash(self):
        lock = json.loads((io.ROOT / 'docs/official_reference_sha256.json').read_text(encoding='utf-8'))
        for name in ('baseline.py', 'run_baseline.sh'):
            self.assertEqual(hashlib.sha256((io.ROOT / name).read_bytes()).hexdigest(), lock['files'][name])


class RuntimeFeedbackTests(unittest.TestCase):
    def check(self, artifacts=True, version='2025.2'):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / 'logs'
            logs.mkdir()
            commands = []
            def tool(command, cwd, log, deadline, trace, name):
                commands.append(command)
                if name == 'version':
                    return 'passed', ('Vitis' if TRACK == 'hls' else 'Vivado') + ' v' + version + '\n'
                if artifacts and name in {'csynth', 'synth'}:
                    paths = ['hls/syn/report/csynth.rpt', 'hls/syn/verilog/dut.v'] if TRACK == 'hls' else ['netlist.v', 'synthesized.dcp']
                    for name in paths:
                        path = cwd / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('synthetic artifact', encoding='utf-8')
                return 'passed', 'synthetic tool output'
            variable = 'HLS_AGENT_VITIS_VERSION' if TRACK == 'hls' else 'RTL_AGENT_VIVADO_VERSION'
            with patch.object(io, 'find_tool', side_effect=lambda name, track: name), \
                 patch.object(io, 'run_tool', side_effect=tool), \
                 patch.dict(os.environ, {variable: '2025.2', 'EDA_TMP': str(root / 'scratch')}):
                result = io.runtime_check(TRACK, CODE, 'sum' if TRACK == 'hls' else 'TopModule',
                                          io.split_interface(INTERFACE) if TRACK == 'hls' else {},
                                          logs, time.monotonic() + 30, io.Trace(root / 'trace.jsonl'))
            self.assertEqual(list((root / 'scratch').iterdir()), [])
            return result, commands

    def test_synthesis_runs_without_hidden_testbench_and_stays_functionally_unverified(self):
        result, commands = self.check()
        self.assertEqual(result[:2], ('passed', 2))
        self.assertIn('UNVERIFIED', result[2])
        if TRACK == 'hls':
            self.assertIn('--mode', commands[-1])
            self.assertEqual(commands[-1][commands[-1].index('--mode') + 1], 'hls')
        else:
            self.assertTrue(any('-timescale' in c for c in commands))
            self.assertFalse(any(c[0] == 'xsim' for c in commands))

    def test_success_exit_without_synthesis_artifacts_is_rejected(self):
        result, _ = self.check(artifacts=False)
        self.assertEqual(result[0], 'failed')

    def test_wrong_official_tool_version_does_not_compile(self):
        result, commands = self.check(version='2024.2')
        self.assertEqual(result[:2], ('unavailable', 0))
        self.assertEqual(len(commands), 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from serve import serve_api as api
        self.api = api
        api.service = api.SubmissionService()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, dict(FPGACHINA_TOKEN='fixture-token', AGENT_TMP=self.temp.name))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.health = patch.object(api, 'probe_health', return_value=dict(ready=False, track=TRACK, model=TRACK+'-local', vram_gb=0.0))
        self.health.start()
        self.addCleanup(self.health.stop)
        self.client = TestClient(api.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {'Authorization': 'Bearer fixture-token'}

    def request(self, **fields):
        body = dict(task_id='fixture', nonce='nonce', mode='agent', prompt='module TopModule: implement a & b', interface=INTERFACE, deadline_s=60)
        body.update(fields)
        return self.client.post('/v1/solve', json=body, headers=self.headers)

    def done(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get('/v1/jobs/' + job, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            if response.json()['status'] == 'done':
                return response.json()
            time.sleep(0.02)
        self.fail('Fixture job did not finish')

    def test_auth_validation_and_unknown_job(self):
        for path in ('/v1/health', '/v1/jobs/missing'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/v1/solve', json=dict(task_id='x', prompt='x')).status_code, 401)
        self.assertEqual(self.client.get('/v1/jobs/missing', headers=self.headers).status_code, 404)
        self.assertEqual(self.request(mode='invalid').status_code, 422)
        self.assertEqual(self.request(deadline_s=0).status_code, 422)
        self.assertEqual(self.client.get('/v1/health', headers=self.headers).json()['track'], TRACK)

    def test_nonce_exactly_once_and_isolated_tasks(self):
        calls = []
        def solve(req, submitted):
            calls.append(req.task_id)
            return req.task_id, json.dumps(dict(tool='fixture')) + '\n'
        with patch.object(self.api.service, 'solve', side_effect=solve):
            first = self.request(task_id='one').json()
            retry = self.request(task_id='one').json()
            self.assertEqual(first['job_id'], retry['job_id'])
            second = self.request(task_id='two', nonce='two').json()
            self.assertEqual(self.done(first['job_id'])['solution'], 'one')
            self.assertEqual(self.done(second['job_id'])['solution'], 'two')
            self.assertEqual(calls, ['one', 'two'])

    def test_expired_queue_never_launches_child(self):
        req = self.api.SolveRequest(task_id='expired', prompt='x', deadline_s=1)
        with patch.object(self.api, 'managed_popen') as launch:
            solution, trace = self.api.service.solve(req, time.monotonic() - 2)
        launch.assert_not_called()
        self.assertEqual(solution, '')
        self.assertEqual(json.loads(trace)['tool'], 'budget')

    def test_worker_failure_becomes_done_empty_solution(self):
        with patch.object(self.api.service, 'solve', side_effect=RuntimeError('fixture failure')):
            response = self.request()
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.done(response.json()['job_id'])['solution'], '')

    def test_not_ready_is_not_reported_as_ready(self):
        result = self.client.get('/v1/health', headers=self.headers).json()
        self.assertIs(result['ready'], False)

    def test_real_entrypoints_against_synthetic_loopback_model(self):
        records = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                self.send(dict(data=[dict(id=TRACK+'-local')]))
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                records.append(request)
                self.send(dict(choices=[dict(message=dict(content=CODE), finish_reason='stop')],
                               usage=dict(prompt_tokens=10, completion_tokens=20),
                               hls_agent_mock=True, rtl_agent_mock=True))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, dict(LLM_BASE_URL='http://127.0.0.1:'+str(server.server_port)+'/v1')):
                for mode in ('baseline', 'agent'):
                    response = self.request(mode=mode, nonce=mode)
                    self.assertEqual(response.status_code, 202)
                    result = self.done(response.json()['job_id'])
                    self.assertEqual(result['solution'], CODE, result)
                    trace = [json.loads(line) for line in result['trace'].splitlines()]
                    self.assertEqual(sum(row['tool'] == 'llm' for row in trace), 1)
                    if mode == 'baseline':
                        self.assertEqual(trace[0]['tool'], 'baseline_meta')
                        self.assertFalse(any(row['tool'] in {'synth', 'csynth', 'xelab'} for row in trace))
                for request in records:
                    self.assertEqual(request['temperature'], 0.0)
                    self.assertEqual(request['top_p'], 1.0)
                    self.assertEqual(request['max_tokens'], 16384)
                    self.assertEqual(request['chat_template_kwargs'], {'enable_thinking': False})
                self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
