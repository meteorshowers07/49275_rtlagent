import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from .task_metadata import detect_clock_port

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def extract_code(text):
    """Formatting only, identical for baseline and agent. No semantic repairs."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Model returned no source text')
    blocks = re.findall(r'```(?:verilog|systemverilog|sv)?\s*\n(.*?)```', text, re.S | re.I)
    if blocks:
        return '\n\n'.join(blocks).strip() + '\n'
    return text.strip() + '\n'


def unverified(reason):
    return {'status': 'unverified', 'highest_stage': 0, 'stages': [], 'feedback': reason}


# Sampling seed = run_seed * STRIDE + (attempt - 1). The stride decouples the seed
# a run actually samples with from the attempt ordinal: with a stride of 1 the
# runs for --seeds 0 1 2 3 4 and max_attempts=3 would draw only 7 distinct seeds
# (0..6) across their 15 attempts, so "independent runs" would reuse each other's
# sampling seeds and the estimator's independence claim would fail at the
# sampling level. Requiring max_attempts <= STRIDE keeps one run's attempts from
# ever reaching a sampling seed another run uses.
SAMPLING_SEED_STRIDE = 1000


def sampling_seed(seed, attempt):
    """Derive one attempt's model sampling seed from its run seed."""
    return seed * SAMPLING_SEED_STRIDE + (attempt - 1)


def repair_feedback(feedback, limit=2000):
    """Keep the stage/reason summary and recent log evidence in bounded feedback."""
    if len(feedback) <= limit:
        return feedback
    marker = '\n[... feedback truncated; full diagnostic is in the EDA logs ...]\n'
    available = limit - len(marker)
    head = available * 3 // 5
    return feedback[:head] + marker + feedback[-(available - head):]


def load_task(path):
    path = Path(path).resolve()
    task = json.loads(path.read_text(encoding='utf-8'))
    for name in ('task_id', 'prompt', 'top'):
        if not isinstance(task.get(name), str) or not task[name].strip():
            raise ValueError(f'task.{name} must be a nonempty string')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', task['top']):
        raise ValueError('Unsupported top identifier')
    if task.get('track', 'rtl') != 'rtl':
        raise ValueError('This entrypoint only accepts RTL tasks')
    task['track'] = 'rtl'
    if 'clock_port' not in task:
        task['clock_port'] = detect_clock_port(task['prompt'])
    files = task.get('testbench_files', [])
    if not isinstance(files, list) or any(not isinstance(p, str) for p in files):
        raise ValueError('testbench_files must be a list of paths')
    task['testbench_files'] = [str((path.parent / p).resolve()) for p in files]
    # Bind the run to the bytes evaluated, not only their pathnames. The
    # testbench decides what "passed" means, so a task whose testbench is edited
    # in place must not keep the identity of the original task: without this
    # snapshot the digest is computed from pathnames alone, two different
    # testbenches pool into one pass@k sample, and results pair across them with
    # no visible signal. Imported VerilogEval tasks also declare upstream hashes;
    # refuse a testbench that no longer matches them.
    declared = task.get('source_sha256', {})
    if not isinstance(declared, dict):
        raise ValueError('source_sha256 must be an object')
    asset_hashes = {}
    for asset in task['testbench_files']:
        try:
            fingerprint = hashlib.sha256(Path(asset).read_bytes()).hexdigest()
        except OSError as exc:
            # A missing testbench is not a digest question: the validator reports
            # it, and hashing here must not silently invent a content identity.
            if not declared:
                continue
            raise ValueError(f'Cannot read declared evaluation asset {asset}: {exc}') from exc
        if declared:
            expected = [value for key, value in declared.items() if Path(key).name == Path(asset).name]
            if expected != [fingerprint]:
                raise ValueError('Evaluation asset differs from declared source hash: ' + asset)
        asset_hashes[asset] = fingerprint
    task['asset_sha256'] = asset_hashes
    return task


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('track', 'rtl') != 'rtl':
        raise ValueError('RTL requires a matching track configuration')
    for field in ('context_length', 'max_tokens', 'max_attempts'):
        if type(config.get(field)) is not int or config[field] <= 0:
            raise ValueError(f'{field} must be a positive integer')
    for field in ('run_timeout_s', 'tool_timeout_s'):
        if (type(config.get(field)) not in (int, float) or not math.isfinite(config[field])
                or config[field] <= 0):
            raise ValueError(f'{field} must be positive')
    if config['max_tokens'] >= config['context_length']:
        raise ValueError('Output must fit in context')
    if not (0 <= config['temperature'] <= 2 and 0 < config['top_p'] <= 1):
        raise ValueError('Invalid sampling configuration')
    if config['max_attempts'] > SAMPLING_SEED_STRIDE:
        raise ValueError('max_attempts must not exceed the sampling seed stride')
    return config


def run_generation(task, config, mode, client, output, seed, paired_run_id='', validator=None, mock=False):
    if mode not in ('agent', 'baseline'):
        raise ValueError('Generation mode must be agent or baseline')
    if task.get('track', 'rtl') != 'rtl' or config.get('track', 'rtl') != 'rtl':
        raise ValueError('This entrypoint only accepts RTL tasks and configuration')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    budget = config['run_timeout_s']
    if 'AGENT_DEADLINE_S' in os.environ:
        supplied = float(os.environ['AGENT_DEADLINE_S'])
        if not math.isfinite(supplied) or supplied <= 0:
            raise ValueError('AGENT_DEADLINE_S must be positive and finite')
        budget = min(budget, max(0.0, supplied - 10.0))
    deadline = started + budget
    result = {'schema_version': 1, 'track': 'rtl', 'task_id': task['task_id'], 'mode': mode,
              'evaluation_kind': 'mock' if mock else 'real', 'config_digest': digest(config),
              'task_digest': digest(task), 'paired_run_id': paired_run_id, 'seed': seed,
              'validation': unverified('No EDA evaluation performed'), 'attempts': [],
              'sampling_seeds': [], 'wall_s': 0}
    source = getattr(client, 'provenance', None)
    source = dict(source) if isinstance(source, dict) else {'kind': 'custom'}
    result['inference_source'] = source
    result['inference_source_digest'] = digest(source)
    if source['kind'] == 'replay':
        result['evaluation_kind'] = 'mock'
    save_json(output / 'config.json', config)
    save_json(output / 'task.json', task)
    seen = {}
    best = None
    previous = ''
    feedback = ''
    try:
        # Baseline must never read the skill file, call the validator, or retry.
        skill = (ROOT / 'skill' / 'rtl_rules.md').read_text(encoding='utf-8') if mode == 'agent' else ''
        for index in range(1, (config['max_attempts'] if mode == 'agent' else 1) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result['stop_reason'] = 'wall_clock_budget'
                break
            messages = [{'role': 'user', 'content': task['prompt']}]
            if mode == 'agent':
                messages.insert(0, {'role': 'system', 'content': skill})
                if previous:
                    # Name the repair boundary explicitly: a diagnostic does not
                    # authorize changing the interface or cycle-level contract.
                    messages.extend([{'role': 'assistant', 'content': previous},
                                     {'role': 'user', 'content': 'Repair the RTL using this tool feedback. Return complete RTL only. '
                                                                 'Identify and change the construct that caused the failure. '
                                                                 'Preserve the exact module name, ports, widths, latency, and cycle-level behavior. '
                                                                 'Follow the specified reset type, active level, and priority; '
                                                                 'a synchronous reset must not appear as an edge in the event control. '
                                                                 'Return the full design, without diagnostics or explanations.\n'
                                      + repair_feedback(feedback)}])
            attempt_dir = output / f'attempt_{index:02d}'
            attempt_dir.mkdir()
            # Record the sampling seed actually sent to the model, so seed reuse
            # across "independent" runs stays auditable from the evidence alone.
            attempt_seed = sampling_seed(seed, index)
            result['sampling_seeds'].append(attempt_seed)
            save_json(attempt_dir / 'request.json', {'messages': messages, 'seed': attempt_seed})
            answer = client.generate(messages, attempt_seed, remaining)
            save_json(attempt_dir / 'response.json', answer)
            if answer.get('mock'):
                result['evaluation_kind'] = 'mock'
            try:
                code = extract_code(answer.get('content'))
                if not code.strip():
                    raise ValueError('Model returned an empty source block')
            except ValueError as exc:
                # Completed responses with no usable source are observed failures,
                # rather than missing evidence to exclude from evaluation.
                validation = {'status': 'failed', 'highest_stage': 0, 'stages': [],
                              'stage_evidence_mode': 'output_rejected',
                              'feedback': 'Output format rejected before Vivado: ' + str(exc)}
                result['attempts'].append({'index': index, 'seed': attempt_seed,
                                          'validation': validation,
                                          'usage': answer.get('usage', {}),
                                          'finish_reason': answer.get('finish_reason')})
                if best is None:
                    result['validation'] = validation
                if mode == 'baseline' or result['evaluation_kind'] == 'mock':
                    result['stop_reason'] = 'output_format_rejected'
                    break
                content = answer.get('content')
                previous = content if isinstance(content, str) and content.strip() else '[empty or non-text model response]'
                feedback = validation['feedback']
                continue
            code_path = attempt_dir / 'design.sv'
            code_path.write_text(code, encoding='utf-8')
            fingerprint = hashlib.sha256(code.encode()).hexdigest()
            if fingerprint in seen:
                result['attempts'].append({
                    'index': index, 'seed': attempt_seed, 'sha256': fingerprint,
                    'duplicate_of_attempt': seen[fingerprint],
                    'validation': unverified('Repeated candidate; EDA was not rerun'),
                    'usage': answer.get('usage', {}), 'finish_reason': answer.get('finish_reason'),
                })
                result['stop_reason'] = 'repeated_candidate'
                break
            seen[fingerprint] = index
            validation = unverified('Baseline generation does not invoke tools')
            if result['evaluation_kind'] == 'mock':
                validation = unverified('Mock inference fixture: EDA is not executed')
            elif mode == 'agent' and validator:
                remaining = min(config['tool_timeout_s'], deadline - time.monotonic())
                if remaining > 0:
                    validation = validator(code_path, task, attempt_dir / 'eda', remaining)
                else:
                    validation = unverified('No time remains for EDA')
            entry = {'index': index, 'seed': attempt_seed, 'sha256': fingerprint,
                     'validation': validation,
                     'usage': answer.get('usage', {}), 'finish_reason': answer.get('finish_reason')}
            result['attempts'].append(entry)
            # Keep the candidate with the greatest proven stage. Tie: retain the
            # earlier candidate so a later failed repair cannot replace it.
            if best is None or validation['highest_stage'] > best[0]:
                best = (validation['highest_stage'], code, validation, index)
            if mode == 'baseline':
                result['stop_reason'] = 'single_generation'
                break
            if validation.get('failure_cause') in ('tool_timeout', 'budget_exhausted'):
                result['stop_reason'] = 'eda_budget'
                break
            if validation.get('failure_cause') in ('tool_crash', 'tool_start_error'):
                result['stop_reason'] = 'eda_tool_error'
                result['diagnostic_hint'] = (
                    'EDA tool failed abnormally; RTL repair stopped. Inspect the stage logs, '
                    'exit code and tool environment. A candidate may also trigger a tool defect; '
                    'the root cause requires comparison and reproduction.'
                )
                break
            if validation['status'] != 'failed':
                result['stop_reason'] = validation['status']
                break
            previous, feedback = code, validation.get('feedback', '')
        else:
            result['stop_reason'] = 'attempt_budget'
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        result['stop_reason'] = 'error'
    if best:
        (output / 'design.sv').write_text(best[1], encoding='utf-8')
        result.update(validation=best[2], selected_attempt=best[3])
    result['wall_s'] = time.monotonic() - started
    save_json(output / 'result.json', result)
    return result
