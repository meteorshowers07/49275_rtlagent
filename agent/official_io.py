"""Official directory I/O. Runtime synthesis is feedback, never a hidden-test grade."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from .client import LocalClient, _check_endpoint
from .core import extract_code, repair_feedback
from .processes import managed_run
from .tool_versions import reported_version, required_version

ROOT = Path(__file__).resolve().parents[1]


class Trace:
    def __init__(self, path):
        self.path = Path(path)

    def write(self, tool, **fields):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(ts=time.time(), tool=tool, **fields), ensure_ascii=False) + '\n')


def profile():
    """Read the same model/context/thinking declaration as the immutable baseline."""
    declaration = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    required_version('HLS_AGENT_VITIS_VERSION' if declaration['track'] == 'hls' else 'RTL_AGENT_VIVADO_VERSION')
    if declaration.get('toolchain', {}).get('vivado') != '2025.2' or declaration.get('toolchain', {}).get('vitis') != '2025.2':
        raise ValueError('manifest.json toolchain must specify Vivado/Vitis 2025.2')
    config = json.loads((ROOT / 'serve/config.json').read_text(encoding='utf-8'))
    endpoint = os.environ.get('LLM_BASE_URL', config['endpoint']).rstrip('/')
    _check_endpoint(endpoint)
    # baseline.py selects the first /models entry. Require a single declared model
    # in the readiness probe rather than silently selecting a different model.
    config.update(endpoint=endpoint, model=declaration['model']['name'],
                  model_revision=declaration['model']['revision'],
                  context_length=declaration['context']['max_model_len'],
                  max_tokens=16384, temperature=0.0, top_p=1.0)
    if declaration['context']['max_output_tokens'] != 16384:
        raise ValueError('Official baseline fixes max_tokens=16384; manifest must match')
    if config['context_length'] <= config['max_tokens']:
        raise ValueError('Context must leave space for the official baseline prompt')
    # Use the official baseline parser itself for the shared thinking parameters.
    import baseline
    config.update(baseline.load_generation())
    return declaration, config


def split_interface(interface):
    if not interface.strip():
        return {}
    match = re.match(r'\s*//\s*header:\s*([A-Za-z_][A-Za-z0-9_.-]*\.(?:h|hpp))\s*(?:\r?\n|$)', interface)
    if not match:
        raise ValueError('HLS interface must begin with // header: <safe basename.h>')
    # Keep all supplied bytes as UTF-8 text, including the harmless marker.
    return {match[1]: interface}


def infer_top(track, prompt, interface):
    if track == 'rtl':
        match = re.search(r'\bmodule\s+([A-Za-z_]\w*)', interface or prompt)
        return match[1] if match else 'TopModule'
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', interface, flags=re.S)
    names = re.findall(r'\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*;', text)
    names = list(dict.fromkeys(n for n in names if n not in {'if', 'sizeof', 'static_assert'}))
    return names[0] if len(names) == 1 else None


def find_tool(name, track):
    found = shutil.which(name)
    if found:
        return found
    variable = 'XILINX_HLS' if track == 'hls' else 'XILINX_VIVADO'
    base = os.environ.get(variable)
    if base:
        for suffix in ('', '.bat', '.exe'):
            path = Path(base) / 'bin' / (name + suffix)
            if path.is_file():
                return str(path)
    return None


def run_tool(cmd, cwd, log, deadline, trace, tool):
    started = time.monotonic()
    left = deadline - started
    if left <= 0:
        trace.write(tool, rc=124, failure_cause='budget_exhausted')
        return 'timeout', ''
    cause = None
    rc = -1
    try:
        with log.open('w', encoding='utf-8') as stream:
            stream.write('COMMAND ' + json.dumps(cmd) + '\n')
            stream.flush()
            result = managed_run(cmd, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=left)
        rc = result.returncode
    except subprocess.TimeoutExpired:
        cause = 'tool_timeout'
        rc = 124
    except OSError as error:
        cause = 'tool_start_error'
        with log.open('a', encoding='utf-8') as stream:
            stream.write(str(error))
    output = log.read_text(encoding='utf-8', errors='replace')
    crash = rc < 0 or rc in {3221226356, 3221225477, 3221225725, 3221225501}
    crash |= bool(re.search(r'^\s*(?:Segmentation fault|Abnormal program termination|Internal compiler error|FATAL_ERROR)\b', output, re.M | re.I))
    # A deadline kill without a preceding crash signature stays a timeout.
    if crash and cause != 'tool_start_error' and (cause != 'tool_timeout' or re.search(r'^\s*(?:Segmentation fault|Abnormal program termination|FATAL_ERROR)\b', output, re.M | re.I)):
        cause = 'tool_crash'
    trace.write(tool, rc=rc, failure_cause=cause, elapsed_s=round(time.monotonic() - started, 3), excerpt=output[-2000:])
    if cause in {'tool_crash', 'tool_start_error'}:
        return 'unavailable', output
    if cause == 'tool_timeout':
        return 'timeout', output
    return ('passed' if rc == 0 else 'failed'), output


def runtime_check(track, code, top, headers, directory, deadline, trace):
    """No trusted testbench is used, and no competition highest_stage is assigned."""
    if top is None or not re.fullmatch(r'[A-Za-z_]\w*', top):
        trace.write('runtime_check', rc=-1, excerpt='Top is ambiguous; synthesis skipped, functionality unverified')
        return 'unavailable', 0, 'Cannot infer an unambiguous top from the provided interface'
    try:
        expected = required_version('HLS_AGENT_VITIS_VERSION' if track == 'hls' else 'RTL_AGENT_VIVADO_VERSION')
    except ValueError as error:
        trace.write('runtime_check', rc=-1, excerpt=str(error))
        return 'unavailable', 0, str(error)
    required = ['v++'] if track == 'hls' else ['vivado', 'xvlog', 'xelab']
    tools = {name: find_tool(name, track) for name in required}
    if not all(tools.values()):
        reason = 'Tools unavailable: ' + ', '.join(n for n, p in tools.items() if not p)
        trace.write('runtime_check', rc=-1, excerpt=reason)
        return 'unavailable', 0, reason
    scratch = Path(os.environ.get('EDA_TMP') or (tempfile.gettempdir() if os.name == 'nt' else '/var/tmp/eda'))
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=track + '_runtime_', dir=scratch) as tmp:
        work = Path(tmp)
        src = 'solution.cpp' if track == 'hls' else 'solution.sv'
        (work / src).write_text(code, encoding='utf-8')
        for name, content in headers.items():
            (work / name).write_text(content, encoding='utf-8')
        version_tool = tools['v++' if track == 'hls' else 'vivado']
        status, output = run_tool([version_tool, '--version' if track == 'hls' else '-version'], work,
                                  directory / 'version.log', deadline, trace, 'version')
        banner = re.sub(r'(?m)^(\s*)v\+\+(?=\s)', r'\1Vitis', output)
        actual = reported_version(banner, 'vitis' if track == 'hls' else 'vivado')
        # Vivado may return ordinary exit 1 with a valid version banner.
        if status in {'timeout', 'unavailable'} or actual != expected or re.search(r'^\s*(?:ERROR|FATAL)\b', output, re.M):
            return 'unavailable', 0, f'Requires {expected}, observed {actual}; ' + output[-1500:]
        rank = 0
        if track == 'hls':
            cfg = ('part=xczu3eg-sbva484-1-e\n[hls]\nflow_target=vivado\nclock=5ns\n'
                   f'syn.file={src}\nsyn.top={top}\n')
            (work / 'hls.cfg').write_text(cfg, encoding='utf-8')
            (directory / 'hls.cfg').write_text(cfg, encoding='utf-8')
            command = [tools['v++'], '-c', '--mode', 'hls', '--config', 'hls.cfg', '--work_dir', str(work)]
        else:
            for name, command in [('xvlog', [tools['xvlog'], '--sv', src]),
                                  ('xelab', [tools['xelab'], top, '-s', 'dut', '-timescale', '1ps/1ps'])]:
                status, output = run_tool(command, work, directory / (name + '.log'), deadline, trace, name)
                if status != 'passed':
                    return status, 0, output[-2000:]
            rank = 1
            tcl = (f'read_verilog -sv {src}\nsynth_design -top {{{top}}} -part xczu3eg-sbva484-1-e -mode out_of_context\n'
                   'write_verilog -force netlist.v\nwrite_checkpoint -force synthesized.dcp\n')
            (work / 'synth.tcl').write_text(tcl, encoding='utf-8')
            (directory / 'synth.tcl').write_text(tcl, encoding='utf-8')
            command = [tools['vivado'], '-mode', 'batch', '-source', 'synth.tcl', '-nojournal', '-nolog']
        status, output = run_tool(command, work, directory / 'synth.log', deadline, trace, 'csynth' if track == 'hls' else 'synth')
        if status == 'passed':
            artifacts = ((list(work.glob('**/syn/report/*csynth*.rpt')) and
                          (list(work.glob('**/syn/verilog/*.v')) or list(work.glob('**/syn/vhdl/*.vhd'))))
                         if track == 'hls' else (work / 'netlist.v').is_file() and (work / 'synthesized.dcp').is_file())
            if not artifacts or re.search(r'^\s*(?:ERROR|FATAL)\s*[:\[]', output, re.M):
                return 'failed', rank, 'Synthesis did not provide clean output and expected artifacts\n' + output[-1500:]
            # Keep compact reports, not large build trees.
            for report in work.glob('**/syn/report/*csynth*.rpt'):
                shutil.copyfile(report, directory / report.name)
            return 'passed', 2, 'Synthesis artifacts exist; functional correctness remains UNVERIFIED'
        return status, rank, output[-2000:]


def solve(input_dir, output_dir, client=None, checker=None):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    declaration = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    track = declaration['track']
    solution = output_dir / ('solution.cpp' if track == 'hls' else 'solution.v')
    trace_path = output_dir / 'trace.jsonl'
    # Every invocation starts fresh, including failures; stale code is never reused.
    solution.write_text('', encoding='utf-8')
    trace_path.write_text('', encoding='utf-8')
    trace = Trace(trace_path)
    best, best_rank, feedback, previous = '', -2, '', ''
    stop = 'error'
    started = time.monotonic()
    try:
        declaration, config = profile()
        supplied = float(os.environ.get('AGENT_DEADLINE_S', '900'))
        if not math.isfinite(supplied) or supplied <= 0:
            raise ValueError('AGENT_DEADLINE_S must be positive and finite')
        deadline = started + min(supplied, 900) - min(20.0, supplied)
        input_dir = Path(input_dir)
        prompt = (input_dir / 'prompt.txt').read_text(encoding='utf-8')
        if not prompt.strip():
            raise ValueError('prompt.txt is empty')
        iface = input_dir / 'interface.txt'
        interface = iface.read_text(encoding='utf-8') if iface.is_file() else ''
        headers = split_interface(interface) if track == 'hls' else {}
        if track == 'hls' and not headers:
            raise ValueError('HLS requires the supplied full interface header')
        for name, content in headers.items():
            (output_dir / name).write_text(content, encoding='utf-8')
        top = infer_top(track, prompt, interface)
        prompt += ('\n\nInterface:\n' + interface) if interface else ''
        skill = (ROOT / 'skill' / (track + '_rules.md')).read_text(encoding='utf-8')
        client = client or LocalClient(config)
        checker = checker or runtime_check
        seen = set()
        trace.write('agent', event='start', track=track, mode='agent', model=config['model'],
                    model_revision=config['model_revision'], functional_verification='unverified')
        for index in range(1, min(10, declaration['agent']['max_rounds']) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop = 'budget_exhausted'
                break
            directory = output_dir / ('attempt_' + str(index).zfill(2))
            directory.mkdir(exist_ok=True)
            messages = [{'role': 'system', 'content': skill}, {'role': 'user', 'content': prompt}]
            if feedback:
                messages += [{'role': 'assistant', 'content': previous},
                             {'role': 'user', 'content': 'Repair only the diagnosed defect. Preserve the exact interface and behavior. Return code only.\n' + repair_feedback(feedback)}]
            (directory / 'request.json').write_text(json.dumps(dict(messages=messages, model=config['model']), ensure_ascii=False, indent=2), encoding='utf-8')
            t0 = time.monotonic()
            try:
                answer = client.generate(messages, index - 1, remaining)
            except Exception as error:
                trace.write('llm', rc=1, round=index, excerpt=str(error)[:2000], elapsed_s=time.monotonic() - t0)
                raise
            usage = answer.get('usage') or {}
            trace.write('llm', rc=0, round=index, tokens_in=usage.get('prompt_tokens'), tokens_out=usage.get('completion_tokens'),
                        elapsed_s=time.monotonic() - t0, mock=answer.get('mock', False))
            (directory / 'response.json').write_text(json.dumps(answer, ensure_ascii=False, indent=2), encoding='utf-8')
            text = answer.get('content') or ''
            # Remove reasoning before the existing code-format extractor.
            if '</think>' in text:
                text = text.rsplit('</think>', 1)[1]
            elif '<think>' in text:
                text = text.split('<think>', 1)[0]
            try:
                code = extract_code(text)
            except ValueError as error:
                feedback, previous = str(error), text or '[empty response]'
                continue
            (directory / solution.name).write_text(code, encoding='utf-8')
            trace.write('candidate', round=index, sha256=hashlib.sha256(code.encode('utf-8')).hexdigest())
            for name, content in headers.items():
                (directory / name).write_text(content, encoding='utf-8')
            if code in seen:
                stop = 'repeated_candidate'
                break
            seen.add(code)
            if best_rank == -2:
                best, best_rank = code, -1
                solution.write_text(best, encoding='utf-8')
            if answer.get('mock'):
                stop = 'mock_inference'
                break
            if track == 'rtl':
                from .official_reference_tools import check_interface
                rc, note = check_interface(code, interface, prompt)
                trace.write('check_interface', rc=rc, excerpt=note[:2000])
                if rc:
                    feedback, previous = note, code
                    continue
            # An interface-valid candidate outranks an earlier rejected fallback,
            # even if the toolchain is unavailable for further verification.
            if best_rank < 0:
                best, best_rank = code, 0
                solution.write_text(best, encoding='utf-8')
            status, rank, feedback = checker(track, code, top, headers, directory, deadline, trace)
            if rank > best_rank:
                best, best_rank = code, rank
                solution.write_text(best, encoding='utf-8')
            stop = status
            if status != 'failed':
                break
            previous = code
    except Exception as error:
        trace.write('agent', event='error', rc=1, excerpt=f'{type(error).__name__}: {error}'[:2000])
    finally:
        solution.write_text(best, encoding='utf-8')
        trace.write('agent', event='done', stop_reason=stop, bytes=len(best.encode('utf-8')),
                    wall_s=time.monotonic() - started, functional_verification='unverified',
                    selected_sha256=hashlib.sha256(best.encode('utf-8')).hexdigest())
    # Empty code is a normal unsolved result at the protocol layer.
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task_dir')
    parser.add_argument('out_dir')
    args = parser.parse_args()
    return solve(args.task_dir, args.out_dir)


if __name__ == '__main__':
    def terminated(signum, frame):
        raise KeyboardInterrupt('Task terminated by signal ' + str(signum))
    signal.signal(signal.SIGTERM, terminated)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(143)
