#!/usr/bin/env python3
"""Tool inventory and workspace preflight, never an EDA acceptance claim."""
import argparse
import os
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from agent import eda
from scripts.diagnostics import readiness


def collect_checks(work_dir=None):
    checks = {'os': platform.platform(), 'python': platform.python_version(),
              'track': 'rtl', 'tools': {}, 'vivado_2025_2_verified': False,
              'tool_version_verified': False}
    for name in ('vivado', 'xvlog', 'xelab', 'xsim', 'git', 'bash', 'clang++', 'g++', 'cl', 'docker', 'rocminfo', 'amd-smi', 'vllm'):
        checks['tools'][name] = shutil.which(name)
    try:
        expected = eda.required_tool_version()
        checks['required_tool_version'] = expected
        tool = checks['tools']['vivado']
        checks['tools']['vivado'] = tool
        paths = [checks['tools'][t] for t in ('vivado', 'xvlog', 'xelab', 'xsim')]
        checks['same_installation'] = bool(all(paths) and len({Path(p).parent.resolve() for p in paths}) == 1)
        if tool:
            p = subprocess.run([tool, '-version'], capture_output=True, text=True, timeout=15)
            output = (p.stdout or '') + (p.stderr or '')
            actual = eda.reported_version(output, 'vivado')
            checks['vivado_version'] = output[:3000]
            checks['reported_tool_version'] = actual
            checks['version_probe_returncode'] = p.returncode
            complete = eda.probe_completed(output, 'vivado', p.returncode)
            checks['tool_version_verified'] = complete and actual == expected
            checks['vivado_2025_2_verified'] = complete and actual == '2025.2'
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        checks['vivado_version'] = str(exc)
        checks['tool_discovery_error'] = str(exc)
    checks.setdefault('same_installation', False)
    return readiness(checks, work_dir or ROOT, ['vivado', 'xvlog', 'xelab', 'xsim', 'git', 'bash'])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, help="Existing EDA work root to probe (HLS_AGENT_WORK_ROOT takes precedence for HLS)")
    args = parser.parse_args()
    print(json.dumps(collect_checks(args.work_dir), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
