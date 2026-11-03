#!/usr/bin/env python3
"""Run local software checks. Optional HTTP transport fixture remains mock."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new directory')
    parser.add_argument('--http', action='store_true', help='also bind a loopback-only fixture server')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    checks = []
    started = time.monotonic()

    def run(name, command):
        with (out / f'{name}.log').open('w', encoding='utf-8') as log:
            p = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        checks.append({'name': name, 'returncode': p.returncode})
        if p.returncode:
            raise RuntimeError(f'{name} failed; inspect {out / (name + ".log")}')

    server = None
    server_log = None
    error = None
    try:
        run('tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
        run('environment', [sys.executable, 'scripts/doctor.py'])
        run('replay', [sys.executable, '-m', 'agent.cli', 'pair', '--task', 'examples/counter.json',
                       '--output', str(out / 'replay'), '--replay', 'examples/replay.json'])
        run('metrics', [sys.executable, 'scripts/metrics.py', str(out / 'replay'),
                        '--output', str(out / 'replay_summary.json')])
        summary = json.loads((out / 'replay_summary.json').read_text(encoding='utf-8'))
        if summary['input']['eligible_runs'] != 0 or summary['input']['excluded_counts']['mock'] != 2:
            raise RuntimeError('Replay results were not correctly excluded')
        run('batch_plan', [sys.executable, 'scripts/batch.py', '--tasks', 'examples/counter.json',
                           '--seeds', '0', '1', '--plan-only', '--output', str(out/'batch_plan')])
        plan = json.loads((out/'batch_plan/batch.json').read_text(encoding='utf-8'))
        if (plan['status'] != 'planned' or plan['evaluation_kind'] != 'unobserved'
                or list((out/'batch_plan/runs').iterdir())):
            raise RuntimeError('Batch preflight launched work or claimed evaluation')
        run('batch_replay', [sys.executable, 'scripts/batch.py', '--tasks', 'examples/counter.json',
                             '--seeds', '0', '1', '--replay', 'examples/replay.json',
                             '--output', str(out/'batch_replay')])
        batch_summary = json.loads((out/'batch_replay/summary.json').read_text(encoding='utf-8'))
        if batch_summary['input']['eligible_runs'] != 0 or batch_summary['input']['excluded_counts']['mock'] != 4:
            raise RuntimeError('Batch fixture results were counted as performance')
        coverage = batch_summary['coverage']
        if (coverage['expected_results'] != 4 or coverage['present_results'] != 4
                or coverage['missing_results'] != 0 or coverage['eligible_results'] != 0
                or coverage['full_real_evaluation']):
            raise RuntimeError('Batch replay coverage incorrectly claimed real evaluation')
        if (ROOT/'data/verilogeval/manifest.json').exists():
            run('dataset_verify', [sys.executable, 'scripts/import_verilogeval.py',
                                   '--verify', str(ROOT/'data/verilogeval')])
        if args.http:
            fixture = json.loads((ROOT/'examples/replay.json').read_text(encoding='utf-8'))
            (out/'http_responses.json').write_text(json.dumps(fixture + fixture), encoding='utf-8')
            server_log = (out / 'http_server.log').open('w')
            server = subprocess.Popen([sys.executable, 'serve/mock_server.py', '--responses',
                                       str(out/'http_responses.json'), '--record', str(out/'http_requests.jsonl'),
                                       '--ready-file', str(out/'ready.json')], cwd=ROOT,
                                      stdout=server_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 10
            while not (out/'ready.json').exists():
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Loopback fixture did not start; inspect http_server.log')
                time.sleep(.05)
            ready = json.loads((out/'ready.json').read_text(encoding='utf-8'))
            config = json.loads((ROOT/'serve/config.json').read_text(encoding='utf-8'))
            config['endpoint'] = ready['endpoint']
            (out/'http_config.json').write_text(json.dumps(config), encoding='utf-8')
            run('http_pair', [sys.executable, '-m', 'agent.cli', 'pair', '--task', 'examples/counter.json',
                              '--config', str(out/'http_config.json'), '--output', str(out/'http_pair')])
            run('http_metrics', [sys.executable, 'scripts/metrics.py', str(out/'http_pair'),
                                 '--output', str(out/'http_summary.json')])
            pair = json.loads((out/'http_pair/pair.json').read_text(encoding='utf-8'))
            records = [json.loads(line) for line in (out/'http_requests.jsonl').read_text(encoding='utf-8').splitlines()]
            prompt = json.loads((ROOT/'examples/counter.json').read_text(encoding='utf-8'))['prompt']
            if len(records) != 2 or records[0]['messages'] != [{'role':'user','content':prompt}]:
                raise RuntimeError('HTTP baseline was not a pure single generation')
            if pair['evaluation_kind'] != 'mock' or any(pair[m]['evaluation_kind'] != 'mock' for m in ('baseline','agent')):
                raise RuntimeError('HTTP fixture lost its mock label')
            http_summary = json.loads((out/'http_summary.json').read_text(encoding='utf-8'))
            if http_summary['input']['eligible_runs'] != 0 or http_summary['input']['excluded_counts']['mock'] != 2:
                raise RuntimeError('HTTP fixture results were counted as performance')
            run('http_batch', [sys.executable, 'scripts/batch.py', '--tasks', 'examples/counter.json',
                               '--seeds', '17', '--config', str(out/'http_config.json'),
                               '--output', str(out/'http_batch')])
            http_batch = json.loads((out/'http_batch/batch.json').read_text(encoding='utf-8'))
            if http_batch['evaluation_kind'] != 'mock' or http_batch['requested_evaluation_kind'] != 'real':
                raise RuntimeError('Batch did not detect the HTTP fixture origin')
    except BaseException as exc:
        # Cancellation must not publish a successful selfcheck report.
        error = f'{type(exc).__name__}: {exc}'
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if server_log:
            server_log.close()
        report = {'software_checks_passed': error is None, 'evaluation_kind': 'mock',
                  'real_model_tested': False, 'real_eda_tested': False,
                  'wall_s': time.monotonic()-started, 'checks':checks, 'error':error}
        (out/'selfcheck.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if error else 0


if __name__ == '__main__':
    raise SystemExit(main())
