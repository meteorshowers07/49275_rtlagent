"""Launch only a declared, installed vLLM version and existing local weights."""
import importlib.metadata
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.official_io import profile

def main():
    declaration, config = profile()
    revision = config['model_revision']
    if not revision or revision.startswith(('UNPINNED', 'REPLACE')):
        raise SystemExit('Pin the real model revision in manifest.json; do not invent a revision')
    version = declaration['serving']['version']
    if version in ('', 'UNVALIDATED', 'REPLACE'):
        raise SystemExit('Record the validated vLLM version in manifest.json')
    if importlib.metadata.version('vllm') != version:
        raise SystemExit('Installed vLLM does not match manifest.json')
    if os.environ.get('EXPECTED_VLLM_VERSION', version) != version:
        raise SystemExit('EXPECTED_VLLM_VERSION conflicts with manifest.json')
    model = Path(os.environ.get('MODEL_PATH', str(Path(__file__).resolve().parents[1] / 'model/weights'))).resolve()
    if not (model / 'config.json').is_file():
        raise SystemExit('Prepare pinned weights locally and set MODEL_PATH before offline startup')
    if '--check' in sys.argv:
        return 0
    command = [sys.executable, '-m', 'vllm.entrypoints.openai.api_server', '--model', str(model),
               '--served-model-name', config['model'], '--host', '127.0.0.1', '--port', '8000',
               '--tensor-parallel-size', '1', '--max-model-len', str(config['context_length']),
               '--max-num-seqs', '1', '--dtype', 'half', '--gpu-memory-utilization', '0.85']
    os.execv(sys.executable, command)

if __name__ == '__main__':
    raise SystemExit(main())
