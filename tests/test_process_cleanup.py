"""Exercise actual nested process groups without starting an EDA binary."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

from scripts.batch import ROOT, _run_pair


@unittest.skipUnless(os.name == 'posix', 'POSIX process groups required')
class ProcessCleanupTests(unittest.TestCase):
    def test_batch_timeout_reaps_eda_in_a_separate_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pidfile = root/'child_pid'
            # The grandchild has its own session exactly like a Vivado command.
            child = "import os,time; from pathlib import Path; Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(60)"
            worker = root/'worker.py'
            worker.write_text(
                "import sys,signal,time\nfrom pathlib import Path\n"
                + "sys.path.insert(0," + repr(str(ROOT)) + ")\n"
                + "from agent.cli import terminate_run,RunTerminated\n"
                + "from agent.eda import _run_command\n"
                + "signal.signal(signal.SIGTERM,terminate_run)\n"
                + "try:\n    _run_command([sys.executable,'-c'," + repr(child) + "],Path(" + repr(str(root)) + "),Path(" + repr(str(root/'eda.log')) + "),time.monotonic()+60)\n"
                + "except RunTerminated as exc:\n    raise SystemExit(128+exc.signum)\n"
            , encoding='utf-8')
            entry = {'command':[sys.executable,str(worker)], 'stdout_log':'stdout.log', 'stderr_log':'stderr.log'}
            try:
                _run_pair(entry,root,1.0)
                self.assertEqual(entry['status'],'timed_out')
                self.assertTrue(pidfile.exists(),'grandchild never started')
                pid = int(pidfile.read_text(encoding='utf-8'))
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid,0)
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text(encoding='utf-8')),9)
                    except ProcessLookupError:
                        pass
