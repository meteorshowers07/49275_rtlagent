"""Exercise the isolated HTTP deadline path with real loopback requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from agent import client as client_module
from agent import processes
from agent.client import LocalClient


class HttpWorkerTests(unittest.TestCase):
    def setUp(self):
        self.requests=[]
        self.body=json.dumps({'rtl_agent_mock':True,'choices':[{'message':{'content':'module top; endmodule'},
                                                               'finish_reason':'stop'}],
                              'usage':{'completion_tokens':6}}).encode()
        self.mode='normal'
        fixture=self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def do_POST(self):
                fixture.requests.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                try:
                    if fixture.mode=='slow_headers':
                        time.sleep(2)
                    if fixture.mode=='redirect':
                        self.send_response(302)
                        self.send_header('Location','https://example.com/must-not-follow')
                        self.end_headers()
                        return
                    self.send_response(503 if fixture.mode=='http_error' else 200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(fixture.body)))
                    self.end_headers()
                    if fixture.mode=='trickle':
                        for value in fixture.body:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(.03)
                    else:
                        self.wfile.write(fixture.body)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                    pass

        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join,2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        config={'endpoint':f'http://127.0.0.1:{self.server.server_port}/v1','model':'rtl-local',
                'context_length':8192,'max_tokens':2048,'temperature':.2,'top_p':.95}
        self.client=LocalClient(config)
        self.messages=[{'role':'user','content':'original problem 中文'}]

    def generate(self,timeout=5):
        with patch.object(client_module,'_supports_alarm_deadline',return_value=False):
            return self.client.generate(self.messages,17,timeout)

    def test_real_worker_preserves_request_mock_usage_and_endpoint_provenance(self):
        source=dict(self.client.provenance)
        with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1'}):
            answer=self.generate()
        self.assertEqual(answer,{'content':'module top; endmodule','usage':{'completion_tokens':6},
                                 'finish_reason':'stop','mock':True})
        self.assertEqual(self.client.provenance,source)
        self.assertEqual(len(self.requests),1)
        path,payload=self.requests[0]
        self.assertEqual(path,'/v1/chat/completions')
        self.assertEqual(payload['messages'],self.messages)
        self.assertEqual(payload['seed'],17)
        self.assertEqual(payload['max_tokens'],2048)

    def test_worker_preserves_nontext_completed_response_as_output_failure(self):
        self.body=b'{"rtl_agent_mock":true,"choices":[{"message":{"content":null},"finish_reason":"stop"}]}'
        answer=self.generate()
        self.assertEqual(answer['content'],'')
        self.assertTrue(answer['mock'])
        self.assertEqual(answer['finish_reason'],'stop')

    def test_bad_http_results_preserve_exception_types_and_never_become_answers(self):
        cases=[('normal',b'not json',json.JSONDecodeError),
               ('normal',b'{"choices":[]}',IndexError),
               ('redirect',b'',ValueError),('http_error',b'{}',HTTPError)]
        for mode,body,error in cases:
            with self.subTest(mode=mode,body=body):
                self.mode,self.body=mode,body
                with self.assertRaises(error):
                    self.generate()
        self.assertEqual(len(self.requests),len(cases))

    def test_slow_headers_and_trickling_body_hit_parent_absolute_deadline(self):
        for mode in ('slow_headers','trickle'):
            with self.subTest(mode=mode):
                self.mode=mode
                started=time.monotonic()
                with self.assertRaisesRegex(TimeoutError,'absolute deadline'):
                    self.generate(.4)
                self.assertLess(time.monotonic()-started,1.8)

    def test_actual_nonmain_thread_uses_worker(self):
        answers,errors=[],[]

        def generate():
            try:
                answers.append(self.client.generate(self.messages,17,5))
            except Exception as error:
                errors.append(error)

        thread=threading.Thread(target=generate)
        thread.start()
        thread.join(7)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertTrue(answers[0]['mock'])

    def test_deadline_reaps_real_http_worker_before_returning(self):
        launched = []
        original = processes.managed_popen

        def record(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process

        for mode in ('slow_headers', 'trickle'):
            with self.subTest(mode=mode):
                self.mode = mode
                with patch.object(processes, 'managed_popen', side_effect=record):
                    with self.assertRaises(TimeoutError):
                        self.generate(.4)
                self.assertEqual(len(launched), 1)
                worker = launched.pop()
                self.assertIsNotNone(worker.poll(), 'HTTP worker survived its deadline')
                if os.name == 'nt':
                    self.assertIsNone(vars(worker).get('_hls_job_handle'), 'Job Object was not released')

    def test_worker_protocol_corruption_is_not_interpreted_as_model_content(self):
        for output in (b'module top; endmodule',b'{"ok":true,"answer":{"content":"x","mock":1}}',b'{"ok":null}'):
            with self.subTest(output=output), patch.object(client_module,'managed_run',return_value=
                    subprocess.CompletedProcess(['worker'],0,output,b'')):
                with self.assertRaisesRegex(RuntimeError,'invalid protocol'):
                    self.generate()

    def test_worker_only_receives_request_fields_not_extra_config_secrets(self):
        self.client.config['unrelated_private_key']='must-not-transfer'
        answer={'content':'x','mock':False}
        with patch.object(client_module,'managed_run',return_value=subprocess.CompletedProcess(
                ['worker'],0,json.dumps({'ok':True,'answer':answer}).encode(),b'')) as run:
            self.assertEqual(self.generate(),answer)
        packet=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(set(packet),{'endpoint','payload','timeout_s'})
        self.assertNotIn('must-not-transfer',run.call_args.kwargs['input'].decode())
