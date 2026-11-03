import io
import json
import os
import signal
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent import client as client_module
from agent.client import LocalClient, NoRedirect


CONFIG = {'endpoint':'http://127.0.0.1:8000/v1', 'model':'rtl-local', 'context_length':8192,
          'max_tokens':2048, 'temperature':.2, 'top_p':.95}


class ClientTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "hard inference deadlines require POSIX timers")
    def test_request_contract_and_no_extra_baseline_prompt(self):
        client = LocalClient(CONFIG)
        body = {'choices':[{'message':{'content':'module top; endmodule'},'finish_reason':'stop'}],
                'usage':{'completion_tokens':6}}
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        prompt = [{'role':'user','content':'original problem'}]
        result = client.generate(prompt, 17, 2.5)
        args, kwargs = client.opener.open.call_args
        request = args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:8000/v1/chat/completions')
        payload = json.loads(request.data)
        self.assertEqual(payload['messages'],prompt)
        self.assertEqual(payload['seed'],17)
        self.assertEqual(payload['max_tokens'],2048)
        self.assertEqual(kwargs['timeout'],2.5)
        self.assertFalse(result['mock'])

    @unittest.skipUnless(os.name == "posix", "hard inference deadlines require POSIX timers")
    def test_completed_empty_output_keeps_provenance_without_client_retry(self):
        for content in ('', '  ', None, 42, [], {'text': 'module top; endmodule'}):
            for mock in (False, True):
                with self.subTest(content=content, mock=mock):
                    client = LocalClient(CONFIG)
                    client.opener = Mock()
                    body = {'rtl_agent_mock': mock, 'choices': [
                        {'message': {'content': content}, 'finish_reason': 'length'}],
                        'usage': {'completion_tokens': 6}}
                    client.opener.open.return_value = io.BytesIO(json.dumps(body).encode())
                    result = client.generate([{'role':'user','content':'problem'}],0,1)
                    self.assertEqual(result['content'], content if isinstance(content, str) else '')
                    self.assertEqual(result['mock'], mock)
                    self.assertEqual(result['finish_reason'], 'length')
                    self.assertEqual(result['usage'], {'completion_tokens': 6})
                    self.assertEqual(client.opener.open.call_count, 1)

    def test_redirects_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')

    @unittest.skipUnless(os.name == "posix", "hard inference deadlines require POSIX timers")
    def test_http_fixture_cannot_look_like_real_inference(self):
        client = LocalClient(CONFIG)
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"rtl_agent_mock":true,"choices":[{"message":{"content":"module top; endmodule"}}]}')
        self.assertTrue(client.generate([{'role':'user','content':'problem'}],0,1)['mock'])


@unittest.skipUnless(os.name == 'posix' and hasattr(signal, 'setitimer'), 'POSIX timers required')
class AbsoluteDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.client = LocalClient(CONFIG)
        self.client.opener = Mock()
        self.messages = [{'role': 'user', 'content': 'problem'}]
        self.body = b'{"choices":[{"message":{"content":"module top; endmodule"}}]}'
        self.old_handler = signal.getsignal(signal.SIGALRM)
        self.old_timer = signal.getitimer(signal.ITIMER_REAL)
        if any(self.old_timer):
            self.skipTest('Test runner already owns an ITIMER_REAL alarm')
        self.handler = Mock()
        signal.signal(signal.SIGALRM, self.handler)

    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.old_handler)

    def assert_clean_timer(self):
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.assertIs(signal.getsignal(signal.SIGALRM), self.handler)
        self.handler.assert_not_called()

    def test_slow_headers_hit_absolute_deadline_and_restore_handler(self):
        def slow_open(*_args, **_kwargs):
            time.sleep(.5)
            return io.BytesIO(self.body)

        self.client.opener.open.side_effect = slow_open
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'absolute deadline'):
            self.client.generate(self.messages, 0, .03)
        self.assertLess(time.monotonic() - started, .4)
        self.assert_clean_timer()

    def test_slow_body_hit_absolute_deadline_closes_response_and_cleans_timer(self):
        class SlowBody(io.BytesIO):
            def read(self, *_args):
                time.sleep(.5)
                return super().read()

        response = SlowBody(self.body)
        self.client.opener.open.return_value = response
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'absolute deadline'):
            self.client.generate(self.messages, 0, .03)
        self.assertLess(time.monotonic() - started, .4)
        self.assertTrue(response.closed)
        self.assert_clean_timer()

    def test_headers_and_body_share_one_deadline(self):
        class SlowBody(io.BytesIO):
            def read(self, *_args):
                time.sleep(.15)
                return super().read()

        def slow_open(*_args, **_kwargs):
            time.sleep(.08)
            return SlowBody(self.body)

        self.client.opener.open.side_effect = slow_open
        with self.assertRaisesRegex(TimeoutError, 'absolute deadline'):
            self.client.generate(self.messages, 0, .18)
        self.assert_clean_timer()

    def test_success_and_malformed_response_also_restore_timer_state(self):
        self.client.opener.open.return_value = io.BytesIO(self.body)
        self.assertEqual(self.client.generate(self.messages, 0, .1)['content'], 'module top; endmodule')
        self.assert_clean_timer()
        self.client.opener.open.return_value = io.BytesIO(b'not json')
        with self.assertRaises(ValueError):
            self.client.generate(self.messages, 0, .1)
        self.assert_clean_timer()
        # A timer accidentally left armed would invoke our restored handler here.
        time.sleep(.12)
        self.assert_clean_timer()

    def test_existing_alarm_is_rejected_without_changing_its_deadline_or_handler(self):
        signal.setitimer(signal.ITIMER_REAL, 10, 2)
        before = signal.getitimer(signal.ITIMER_REAL)
        with self.assertRaisesRegex(RuntimeError, 'existing ITIMER_REAL'):
            self.client.generate(self.messages, 0, .1)
        after = signal.getitimer(signal.ITIMER_REAL)
        self.assertGreater(after[0], 9)
        self.assertLessEqual(after[0], before[0])
        self.assertEqual(after[1], 2)
        self.assertIs(signal.getsignal(signal.SIGALRM), self.handler)
        self.client.opener.open.assert_not_called()

    def test_worker_thread_is_rejected_before_network_call(self):
        errors = []

        def worker():
            try:
                self.client.generate(self.messages, 0, .1)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn('POSIX main thread', str(errors[0]))
        self.client.opener.open.assert_not_called()
        self.assert_clean_timer()

    def test_unsupported_platform_and_invalid_timeouts_fail_before_network_call(self):
        with patch.object(client_module.os, 'name', 'nt'):
            with self.assertRaisesRegex(RuntimeError, 'POSIX main thread'):
                self.client.generate(self.messages, 0, .1)
        for timeout in (0, -1, float('nan'), float('inf'), True, '1'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.client.generate(self.messages, 0, timeout)
        self.client.opener.open.assert_not_called()
        self.assert_clean_timer()
