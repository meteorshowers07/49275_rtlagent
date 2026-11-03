"""Native Windows containment tests; no taskkill, psutil, or EDA required."""

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from agent import processes
from scripts import batch


class PosixProcessPolicyTests(unittest.TestCase):
    def test_term_grace_and_kill_escalation_are_preserved(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 3), -9]
        with patch.object(processes.os, "name", "posix"), \
             patch.object(processes.signal, "SIGKILL", 9, create=True), \
             patch.object(processes.os, "killpg", create=True) as killpg:
            processes.kill_process_tree(process)
            self.assertEqual(killpg.call_args_list,
                             [call(12345, processes.signal.SIGTERM), call(12345, processes.signal.SIGKILL)])
            self.assertEqual(process.wait.call_args_list, [call(timeout=3.0), call()])

    def test_leader_exit_still_kills_remaining_group(self):
        process = Mock(pid=12346)
        process.wait.return_value = -15
        with patch.object(processes.os, "name", "posix"), \
             patch.object(processes.signal, "SIGKILL", 9, create=True), \
             patch.object(processes.os, "killpg", create=True) as killpg:
            processes.kill_process_tree(process)
            self.assertEqual(killpg.call_args_list,
                             [call(12346, processes.signal.SIGTERM), call(12346, processes.signal.SIGKILL)])
            process.wait.assert_called_once_with(timeout=3.0)

    def test_vanished_group_is_safe_after_leader_exit(self):
        process = Mock(pid=12347)
        with patch.object(processes.os, "name", "posix"), \
             patch.object(processes.signal, "SIGKILL", 9, create=True), \
             patch.object(processes.os, "killpg", side_effect=ProcessLookupError, create=True):
            processes.kill_process_tree(process)
            process.wait.assert_called_once_with(timeout=3.0)

    @unittest.skipUnless(os.name == "posix", "native POSIX group and pipe cleanup")
    def test_exited_leader_cannot_leave_term_ignoring_pipe_holder(self):
        with tempfile.TemporaryDirectory(prefix="hls_posix_group_") as directory:
            ready = Path(directory) / "ready"
            child = ("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                     f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
            parent = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)")
            process = processes.managed_popen([sys.executable, "-c", parent],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), "TERM-ignoring descendant did not start")
                processes.kill_process_tree(process, grace_s=.2)
                # This times out under the old implementation: the descendant
                # survives the leader and retains the inherited stdout pipe.
                process.communicate(timeout=2)
                self.assertIsNotNone(process.returncode)
            finally:
                try:
                    os.killpg(process.pid, processes.signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=2)


@unittest.skipUnless(os.name == "nt", "native Windows Job Object regression tests")
class WindowsProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hls_job_test_")
        self.root = Path(self.temporary.name)
        self.pidfile = self.root / "descendants.txt"
        self.handles = []
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.api.OpenProcess.restype = ctypes.c_void_p
        self.api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]

    def tearDown(self):
        for handle in self.handles:
            # Do not leave fixture descendants alive even if the assertion fails.
            if self.api.WaitForSingleObject(handle, 0) == 258:
                self.api.TerminateProcess(handle, 99)
                self.api.WaitForSingleObject(handle, 5000)
            self.api.CloseHandle(handle)
        self.temporary.cleanup()

    def command(self, exit_parent=False):
        grandchild = "import time; time.sleep(30)"
        child = ("import os,pathlib,subprocess,sys,time; "
                 f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
                 f"pathlib.Path({str(self.pidfile)!r}).write_text(str(os.getpid())+','+str(p.pid)); "
                 "time.sleep(30)")
        parent = ("import pathlib,subprocess,sys,time; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}], "
                  "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); ")
        if exit_parent:
            parent += (f"p=pathlib.Path({str(self.pidfile)!r}); "
                       "exec('while not p.exists(): time.sleep(.01)')")
        else:
            parent += "time.sleep(30)"
        return [sys.executable, "-c", parent]

    def watch_descendants(self):
        deadline = time.monotonic() + 5
        pids = []
        while time.monotonic() < deadline:
            try:
                pids = list(map(int, self.pidfile.read_text(encoding='utf-8').split(",")))
            except (OSError, ValueError):
                pass
            if len(pids) == 2:
                break
            time.sleep(.01)
        self.assertEqual(len(pids), 2, "native nested fixture did not start")
        for pid in pids:
            handle = self.api.OpenProcess(0x00100000 | 1, False, pid)
            self.assertTrue(handle, f"cannot monitor fixture process {pid}")
            self.handles.append(handle)
        self.assertEqual(len(self.handles), 2)
        self.assertTrue(all(self.api.WaitForSingleObject(h, 0) == 258 for h in self.handles))

    def assert_descendants_reaped(self):
        for handle in self.handles:
            self.assertEqual(self.api.WaitForSingleObject(handle, 5000), 0,
                             "child or grandchild survived process-tree cleanup")

    def test_batch_timeout_kills_child_and_grandchild(self):
        def launch(*args, **kwargs):
            process = processes.managed_popen(*args, **kwargs)
            self.watch_descendants()
            return process

        entry = {"command": self.command(), "stdout_log": "stdout.log", "stderr_log": "stderr.log"}
        with patch.object(batch, "managed_popen", side_effect=launch):
            batch._run_pair(entry, self.root, .05)
        self.assertEqual(entry["status"], "timed_out")
        self.assert_descendants_reaped()

    def test_batch_interruption_kills_child_and_grandchild(self):
        def launch(*args, **kwargs):
            process = processes.managed_popen(*args, **kwargs)
            self.watch_descendants()
            real_wait = process.wait
            interrupted = False

            def wait(*wait_args, **wait_kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                return real_wait(*wait_args, **wait_kwargs)

            process.wait = wait
            return process

        entry = {"command": self.command(), "stdout_log": "stdout.log", "stderr_log": "stderr.log"}
        with patch.object(batch, "managed_popen", side_effect=launch):
            with self.assertRaises(KeyboardInterrupt):
                batch._run_pair(entry, self.root, 5)
        self.assert_descendants_reaped()

    def test_success_release_kills_orphan_descendants(self):
        process = processes.managed_popen(self.command(exit_parent=True), stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        try:
            self.watch_descendants()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            processes.release_process_tree(process)
        self.assert_descendants_reaped()

    def test_assignment_failure_prevents_child_execution(self):
        marker = self.root / "must_not_run.txt"
        command = [sys.executable, "-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"]
        with patch.object(processes._kernel, "AssignProcessToJobObject", return_value=0):
            with self.assertRaises(OSError):
                processes.managed_popen(command)
        self.assertFalse(marker.exists())

    def test_managed_run_capture_nonzero_and_timeout(self):
        result = processes.managed_run([sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr)"],
                                       capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (0, "out", "err"))
        with self.assertRaises(subprocess.CalledProcessError):
            processes.managed_run([sys.executable, "-c", "raise SystemExit(7)"], check=True, timeout=5)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            processes.managed_run([sys.executable, "-c", "import time; print('before',flush=True); time.sleep(30)"],
                                  capture_output=True, text=True, timeout=.3)
        self.assertIn("before", caught.exception.output)

    def test_managed_run_echoes_binary_and_text_input(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        binary = b"request\x00\xff\n"
        result = processes.managed_run(command, input=binary, capture_output=True, timeout=5)
        self.assertEqual(result.stdout, binary)
        text = "request \u4e2d\u6587\n"
        result = processes.managed_run(command, input=text, capture_output=True, text=True,
                                       encoding="utf-8", timeout=5)
        self.assertEqual(result.stdout, text)

    def test_managed_run_rejects_input_stdin_conflict_before_launch(self):
        with patch.object(processes, "managed_popen") as popen:
            with self.assertRaisesRegex(ValueError, "stdin and input"):
                processes.managed_run([sys.executable], input=b"request", stdin=subprocess.DEVNULL)
            popen.assert_not_called()

    def test_input_write_to_unread_pipe_obeys_timeout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            processes.managed_run([sys.executable, "-c", "import time; time.sleep(5)"],
                                  input=b"x" * (1024 * 1024), capture_output=True, timeout=.2)
        self.assertLess(time.monotonic() - started, 3,
                        "a full stdin pipe blocked the deadline watchdog")
        self.assertFalse(any(thread.name == "hls-process-communication"
                             for thread in threading.enumerate()),
                         "stdin communication worker survived deadline cleanup")


if __name__ == "__main__":
    unittest.main()
