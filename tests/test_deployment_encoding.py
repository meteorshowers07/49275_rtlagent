"""Read actual UTF-8 deployment JSON under non-ASCII paths; no service starts."""
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent.core import ROOT
from agent import official_io
import baseline

class DeploymentEncodingTests(unittest.TestCase):
    def test_startup_reads_unicode_model_name_and_path_without_utf8_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'中文配置'
            serve = root/'serve'
            serve.mkdir(parents=True)
            script = serve/'start.py'
            script.write_bytes((ROOT/'serve/start.py').read_bytes())
            config = json.loads((ROOT/'serve/config.json').read_text(encoding='utf-8'))
            declaration = json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
            declaration['model'].update(name='本地模型', revision='pinned-test')
            declaration['serving']['version'] = 'tested'
            (root/'manifest.json').write_text(json.dumps(declaration, ensure_ascii=False), encoding='utf-8')
            (serve/'config.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
            model = root/'模型权重'
            model.mkdir()
            (model/'config.json').write_text('{}',encoding='utf-8')
            with patch.dict(os.environ,{'EXPECTED_VLLM_VERSION':'tested','MODEL_PATH':str(model)}), \
                 patch.object(importlib.metadata,'version',return_value='tested'), \
                 patch.object(official_io,'ROOT',root), \
                 patch.object(baseline,'__file__',str(root/'baseline.py')), \
                 patch.object(os,'execv') as execute, \
                 patch.object(subprocess,'call',return_value=0) as call:
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(script),run_name='__main__')
                self.assertIn(stopped.exception.code,(None,0))
            command = execute.call_args.args[1] if execute.called else call.call_args.args[0]
            self.assertEqual(command[command.index('--served-model-name')+1],'本地模型')
            self.assertEqual(command[command.index('--model')+1],str(model.resolve()))
