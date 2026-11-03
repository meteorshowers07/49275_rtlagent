import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent.core import ROOT
from scripts.metrics import summarize


class CliTests(unittest.TestCase):
    def test_pair_replay_is_paired_and_excluded_from_real_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'pair'
            command = [sys.executable,'-m','agent.cli','pair','--task',str(ROOT/'examples/counter.json'),
                       '--output',str(out),'--replay',str(ROOT/'examples/replay.json')]
            result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            pair = json.loads((out/'pair.json').read_text(encoding='utf-8'))
            b, a = pair['baseline'], pair['agent']
            self.assertEqual(a['paired_run_id'],b['paired_run_id'])
            self.assertEqual(a['config_digest'],b['config_digest'])
            self.assertEqual(a['seed'],b['seed'])
            self.assertEqual(a['evaluation_kind'],'mock')
            self.assertEqual(a['validation']['status'],'unverified')
            req = json.loads((out/'baseline/attempt_01/request.json').read_text(encoding='utf-8'))
            original = json.loads((ROOT/'examples/counter.json').read_text(encoding='utf-8'))
            self.assertEqual(req['messages'],[{'role':'user','content':original['prompt']}])
            summary = summarize([out])
            self.assertEqual(summary['input']['eligible_runs'],0)
            self.assertEqual(summary['input']['excluded_counts']['mock'],2)

    def test_standalone_baseline_replay_never_creates_eda_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'baseline'
            p = subprocess.run([sys.executable,'-m','agent.cli','baseline','--task',str(ROOT/'examples/counter.json'),
                                '--output',str(out),'--replay',str(ROOT/'examples/replay.json')],
                               cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertFalse(list(out.rglob('eda*')))
            self.assertEqual(len(list(out.glob('attempt_*'))),1)
