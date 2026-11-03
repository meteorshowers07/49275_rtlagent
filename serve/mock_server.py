#!/usr/bin/env python3
"""Explicit fixture server for transport tests. It is NOT a language model."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading


def make_server(responses, port=0, record_path=None):
    if not isinstance(responses, list) or not responses or any(not isinstance(x, str) for x in responses):
        raise ValueError('responses must be a nonempty list of strings')
    lock = threading.Lock()
    state = {'index': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_json(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/health':
                self.send_json(200, {'status': 'ok', 'rtl_agent_mock': True})
            else:
                self.send_json(404, {'error': 'unknown fixture endpoint'})

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                return self.send_json(404, {'error': 'unknown fixture endpoint'})
            try:
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('invalid request size')
                request = json.loads(self.rfile.read(size))
                if not isinstance(request.get('messages'), list) or request.get('stream') is not False:
                    raise ValueError('fixture expects a non-streaming chat request')
            except (ValueError, TypeError):
                return self.send_json(400, {'error': 'invalid fixture request'})
            with lock:
                if record_path:
                    with Path(record_path).open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(request, ensure_ascii=False) + '\n')
                if state['index'] >= len(responses):
                    return self.send_json(503, {'error': 'fixture exhausted', 'rtl_agent_mock': True})
                content = responses[state['index']]
                state['index'] += 1
            self.send_json(200, {'id': 'fixture', 'model': 'fixture-only', 'rtl_agent_mock': True,
                                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                                             'finish_reason': 'stop'}], 'usage': {}})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--responses', required=True, type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--ready-file', type=Path)
    args = parser.parse_args()
    responses = json.loads(args.responses.read_text(encoding='utf-8'))
    server = make_server(responses, args.port, args.record)
    info = {'endpoint': f'http://127.0.0.1:{server.server_port}/v1', 'rtl_agent_mock': True}
    if args.ready_file:
        temporary = args.ready_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(info), encoding='utf-8')
        temporary.replace(args.ready_file)
    print(json.dumps(info), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
