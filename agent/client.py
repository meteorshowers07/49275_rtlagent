"""Loopback-only inference client; never falls back to a cloud service."""
from contextlib import contextmanager
import ipaddress
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .processes import managed_run


_RESPONSE_LIMIT = 8 * 1024 * 1024
_ERROR_TYPES = {error.__name__: error for error in
                (ValueError, TypeError, KeyError, IndexError, AttributeError,
                 TimeoutError, OSError, ConnectionError, ConnectionRefusedError,
                 ConnectionResetError, BrokenPipeError, OverflowError, RecursionError)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Inference redirects are disabled")


def _validate_timeout(timeout_s):
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError('Inference timeout must be a finite positive number')


def _supports_alarm_deadline():
    return (os.name == 'posix' and threading.current_thread() is threading.main_thread()
            and all(hasattr(signal, name) for name in
                    ('SIGALRM', 'ITIMER_REAL', 'setitimer', 'getitimer')))


def _check_endpoint(endpoint):
    url = urlsplit(endpoint)
    try:
        local = ipaddress.ip_address(url.hostname or '').is_loopback
    except ValueError:
        local = False
    if not local or url.scheme != 'http' or url.username or url.password or url.query or url.fragment:
        raise ValueError('endpoint must be plain HTTP on a literal loopback address')


def _request_http(endpoint, payload, timeout_s, opener=None):
    """The shared transport/parser; the caller supplies a hard deadline."""
    _check_endpoint(endpoint)
    opener = opener if opener is not None else build_opener(ProxyHandler({}), NoRedirect())
    request = Request(endpoint.rstrip('/') + '/chat/completions',
                      data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=timeout_s) as response:
        raw = response.read(_RESPONSE_LIMIT + 1)
        if len(raw) > _RESPONSE_LIMIT:
            raise ValueError('Inference response exceeds 8 MiB limit')
        data = json.loads(raw)
    choice = data['choices'][0]
    content = choice['message'].get('content')
    # Completed responses without textual code remain known output failures.
    return {'content': content if isinstance(content, str) else '',
            'usage': data.get('usage', {}), 'finish_reason': choice.get('finish_reason'),
            'mock': data.get('rtl_agent_mock') is True}


def _worker_error(error):
    """Encode known errors without pickling objects or returning tracebacks."""
    if isinstance(error, HTTPError):
        return {'type': 'HTTPError', 'url': error.url, 'code': error.code, 'reason': str(error.reason)}
    if isinstance(error, URLError):
        reason = error.reason
        return {'type': 'URLError', 'reason': _worker_error(reason) if isinstance(reason, Exception)
                else {'type': 'message', 'message': str(reason)}}
    if isinstance(error, json.JSONDecodeError):
        return {'type': 'JSONDecodeError', 'message': error.msg, 'document': error.doc, 'position': error.pos}
    if isinstance(error, UnicodeDecodeError):
        return {'type': 'UnicodeDecodeError', 'encoding': error.encoding, 'object': error.object.hex(),
                'start': error.start, 'end': error.end, 'reason': error.reason}
    if type(error).__name__ in _ERROR_TYPES:
        return {'type': type(error).__name__, 'args': error.args}
    return {'type': 'RuntimeError', 'message': f'{type(error).__name__}: {error}'}


def _restore_worker_error(error):
    kind = error['type']
    if kind == 'HTTPError':
        return HTTPError(error['url'], error['code'], error['reason'], None, None)
    if kind == 'URLError':
        reason = error['reason']
        return URLError(reason['message'] if reason['type'] == 'message' else _restore_worker_error(reason))
    if kind == 'JSONDecodeError':
        return json.JSONDecodeError(error['message'], error['document'], error['position'])
    if kind == 'UnicodeDecodeError':
        return UnicodeDecodeError(error['encoding'], bytes.fromhex(error['object']),
                                  error['start'], error['end'], error['reason'])
    if kind in _ERROR_TYPES:
        return _ERROR_TYPES[kind](*error['args'])
    return RuntimeError(error.get('message', 'Inference worker failed'))


@contextmanager
def absolute_deadline(timeout_s):
    """Bound the entire HTTP operation, including a slowly trickling response.

    Python's socket timeout only limits individual blocking waits. SIGALRM also
    interrupts blocking headers/body reads on the supported Linux/macOS CLI.
    An existing real-time alarm belongs to its caller; reject rather than pause
    or steal that deadline. Other contexts use the isolated HTTP worker instead.
    """
    _validate_timeout(timeout_s)
    if not _supports_alarm_deadline():
        raise RuntimeError('Hard inference deadlines require a POSIX main thread (Linux/macOS)')
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if any(previous_timer):
        raise RuntimeError('An existing ITIMER_REAL alarm prevents a separate inference deadline')
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise TimeoutError(f'Inference exceeded its {timeout_s:g} second absolute deadline')

    signal.signal(signal.SIGALRM, expired)
    try:
        deadline = time.monotonic() + timeout_s
        signal.setitimer(signal.ITIMER_REAL, timeout_s)
        yield
        if time.monotonic() >= deadline:
            # Also reject completion if a native operation delayed signal delivery.
            expired(None, None)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def check_context(messages, config):
    # Conservative byte guard, not a tokenizer. The serving backend remains
    # authoritative. Never silently truncate the original problem.
    bound = sum(len(m['content'].encode('utf-8')) + 64 for m in messages) + 256
    if bound + config['max_tokens'] > config['context_length']:
        raise ValueError('Conservative context guard exceeded; shorten feedback or configure a larger context')


class LocalClient:
    def __init__(self, config):
        self.config = config
        _check_endpoint(config['endpoint'])
        self.provenance = {'kind': 'http', 'endpoint': config['endpoint'],
                           'endpoint_sha256': hashlib.sha256(config['endpoint'].encode()).hexdigest(),
                           'model': config['model'], 'model_revision': config.get('model_revision')}
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def generate(self, messages, seed, timeout_s):
        _validate_timeout(timeout_s)
        check_context(messages, self.config)
        payload = {key: self.config[key] for key in ('model', 'temperature', 'top_p', 'max_tokens')}
        payload.update(messages=messages, seed=seed, stream=False)
        for key in ('chat_template_kwargs', 'reasoning_effort'):
            if key in self.config:
                payload[key] = self.config[key]
        if not _supports_alarm_deadline():
            return self._generate_isolated(payload, timeout_s)
        with absolute_deadline(timeout_s):
            return _request_http(self.config['endpoint'], payload, timeout_s, self.opener)

    def _generate_isolated(self, payload, timeout_s):
        deadline = time.monotonic() + timeout_s
        packet = json.dumps({'endpoint': self.config['endpoint'], 'payload': payload,
                             'timeout_s': timeout_s}).encode('utf-8')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'Inference exceeded its {timeout_s:g} second absolute deadline')
        command = [sys.executable, '-I', str(Path(__file__).with_name('http_worker.py'))]
        try:
            result = managed_run(command, input=packet, capture_output=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f'Inference exceeded its {timeout_s:g} second absolute deadline') from error
        if time.monotonic() >= deadline:
            raise TimeoutError(f'Inference exceeded its {timeout_s:g} second absolute deadline')
        if result.returncode:
            raise RuntimeError(f'Inference worker exited with status {result.returncode}')
        try:
            envelope = json.loads(result.stdout)
            if envelope['ok'] is True:
                answer = envelope['answer']
                if (not isinstance(answer, dict) or not isinstance(answer.get('content'), str)
                        or type(answer.get('mock')) is not bool):
                    raise ValueError('invalid answer')
                return answer
            if envelope['ok'] is not False:
                raise ValueError('invalid status')
            error = _restore_worker_error(envelope['error'])
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError('Inference worker returned an invalid protocol response') from error
        raise error


class ReplayClient:
    """A deterministic software fixture, never a real model or hardware result."""
    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.provenance = {'kind': 'replay', 'fixture_sha256': hashlib.sha256(raw).hexdigest()}
        self.responses = json.loads(raw)
        if not isinstance(self.responses, list) or not self.responses:
            raise ValueError('Replay fixture must be a nonempty list of response strings')
        self.index = 0

    def generate(self, messages, seed, timeout_s):
        if self.index >= len(self.responses):
            raise ValueError('Replay fixture exhausted')
        content = self.responses[self.index]
        self.index += 1
        return {'content': content, 'usage': {}, 'finish_reason': 'fixture', 'mock': True}
