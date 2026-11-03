import argparse
import json
from pathlib import Path
import signal
import time
import uuid

from .client import LocalClient, ReplayClient
from .core import ROOT, load_config, load_task, run_generation, save_json, unverified
from .eda import validate_rtl


def main():
    parser = argparse.ArgumentParser(description='Local RTL workflow; official I/O adapter pending')
    parser.add_argument('mode', choices=['agent', 'baseline', 'pair'])
    parser.add_argument('--task', required=True)
    parser.add_argument('--output', required=True, help='New output directory; never overwrites an existing run')
    parser.add_argument('--config', default=str(ROOT / 'serve' / 'config.json'))
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--replay', help='Software-test responses only; all results marked mock')
    args = parser.parse_args()
    task, config = load_task(args.task), load_config(args.config)
    client = ReplayClient(args.replay) if args.replay else LocalClient(config)
    validator = (lambda *a: unverified('Replay smoke test: EDA is not executed')) if args.replay else validate_rtl
    output = Path(args.output).resolve()
    if args.mode != 'pair':
        result = run_generation(task, config, args.mode, client, output, args.seed,
                                validator=validator, mock=bool(args.replay))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if 'error' in result else 0
    output.mkdir(parents=True, exist_ok=False)
    pair_id = str(uuid.uuid4())
    start = time.monotonic()
    results = {}
    for mode in ('baseline', 'agent'):
        results[mode] = run_generation(task, config, mode, client, output / mode, args.seed,
                                      paired_run_id=pair_id, validator=validator, mock=bool(args.replay))
        if mode == 'baseline' and (output / mode / 'design.sv').exists():
            # Scoring occurs OUTSIDE the pure baseline entrypoint; no scoring
            # feedback is sent to the baseline model or to the agent.
            score_start = time.monotonic()
            try:
                if results[mode]['evaluation_kind'] == 'mock':
                    results[mode]['validation'] = unverified('Mock inference fixture: EDA is not executed')
                else:
                    results[mode]['validation'] = validator(output / mode / 'design.sv', task,
                                                           output / mode / 'score', config['tool_timeout_s'])
            except Exception as exc:
                results[mode]['validation'] = unverified(f'External evaluator error: {type(exc).__name__}: {exc}')
            results[mode]['generation_wall_s'] = results[mode]['wall_s']
            results[mode]['wall_s'] += time.monotonic() - score_start
            save_json(output / mode / 'result.json', results[mode])
    pair = {'paired_run_id': pair_id, 'wall_s': time.monotonic() - start,
            'evaluation_kind': 'mock' if any(r['evaluation_kind'] == 'mock' for r in results.values()) else 'real',
            'baseline': results['baseline'], 'agent': results['agent']}
    save_json(output / 'pair.json', pair)
    print(json.dumps(pair, ensure_ascii=False, indent=2))
    return 2 if any('error' in r for r in results.values()) else 0


class RunTerminated(BaseException):
    def __init__(self, signum):
        self.signum = signum


def terminate_run(signum, frame):
    # Allow an active EDA adapter to kill and reap its separate process group.
    raise RunTerminated(signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, terminate_run)
    try:
        raise SystemExit(main())
    except RunTerminated as exc:
        raise SystemExit(128 + exc.signum)
