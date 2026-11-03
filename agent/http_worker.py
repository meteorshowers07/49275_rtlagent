"""One isolated local HTTP request, killed by its parent's absolute deadline."""
import json
from pathlib import Path
import sys

# Executed with Python -I so cwd/PYTHONPATH cannot choose a different agent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.client import _request_http, _validate_timeout, _worker_error


def main():
    try:
        packet = json.loads(sys.stdin.buffer.read())
        timeout_s = packet['timeout_s']
        _validate_timeout(timeout_s)
        answer = _request_http(packet['endpoint'], packet['payload'], timeout_s)
        envelope = {'ok': True, 'answer': answer}
    except Exception as error:
        envelope = {'ok': False, 'error': _worker_error(error)}
    sys.stdout.buffer.write(json.dumps(envelope).encode('utf-8'))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
