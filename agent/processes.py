"""Bounded subprocesses with descendant containment on Windows and POSIX.

Windows children start suspended, enter a kill-on-close Job Object, and only
then run. This avoids the child-spawning race of assigning a running process.
Job setup failures terminate the suspended process and fail the launch closed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class _IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", _BasicLimits), ("io", _IOCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel.Thread32First.restype = wintypes.BOOL
    _kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel.Thread32Next.restype = wintypes.BOOL
    _kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenThread.restype = wintypes.HANDLE
    _kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel.ResumeThread.restype = wintypes.DWORD

    def _new_job():
        job = _kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            _kernel.CloseHandle(job)
            raise error
        return job

    def _resume_primary_thread(pid):
        # Popen closes CreateProcess's primary-thread handle. Reopen it using
        # documented Toolhelp32 APIs while the process is still suspended.
        snapshot = _kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = _kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.process_id == pid:
                    thread = _kernel.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if _kernel.ResumeThread(thread) == 0xffffffff:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        _kernel.CloseHandle(thread)
                available = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError(f"suspended child {pid} has no resumable primary thread")
        finally:
            _kernel.CloseHandle(snapshot)


def managed_popen(*args, **kwargs) -> subprocess.Popen:
    """Launch an owned process tree; callers must release it in a finally block."""
    if os.name != "nt":
        kwargs["start_new_session"] = True
        return subprocess.Popen(*args, **kwargs)
    job = _new_job()
    process = None
    try:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
        process = subprocess.Popen(*args, **kwargs)
        if not _kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        process._hls_job_handle = job
        _resume_primary_thread(process.pid)
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
        _kernel.CloseHandle(job)
        raise


def release_process_tree(process: subprocess.Popen) -> None:
    """Release containment after reaping the child; kill any Windows orphans."""
    if os.name == "nt":
        job = vars(process).get("_hls_job_handle")
        if job is not None:
            process._hls_job_handle = None
            if not _kernel.CloseHandle(job):
                raise ctypes.WinError(ctypes.get_last_error())


def kill_process_tree(process: subprocess.Popen, grace_s: float = 3.0) -> None:
    """Reap the tree on timeout/cancellation, preserving POSIX TERM then KILL."""
    if os.name == "nt":
        job = vars(process).get("_hls_job_handle")
        try:
            if job is None:
                raise RuntimeError("Windows child was not launched with Job Object containment")
            if not _kernel.TerminateJobObject(job, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            # Close also activates KILL_ON_JOB_CLOSE if explicit termination fails.
            release_process_tree(process)
        process.wait(timeout=grace_s)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    leader_reaped = False
    try:
        process.wait(timeout=grace_s)
        leader_reaped = True
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader can exit while a descendant ignores TERM and keeps our
        # capture pipes open. Its exit does not prove that the group is gone.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not leader_reaped:
        process.wait()


def managed_run(args, *, input=None, timeout=None, capture_output=False, check=False, **kwargs):
    """Small subprocess.run equivalent with bounded process-tree cleanup.

    Input follows subprocess.run's str/bytes and text-mode rules. Windows stdin
    writes run in an owned communication thread so a full pipe cannot prevent
    the caller from enforcing the deadline. Cleanup joins that thread before
    returning. The timeout includes process launch and communication.
    TimeoutExpired includes captured output, as in subprocess.run.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = managed_popen(args, **kwargs)
    communication = None
    outcome = {}

    def remaining():
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    def drain():
        if communication is not None:
            communication.join()
            if "output" in outcome:
                return outcome["output"]
        # A timed-out communicate may be retried after termination, without
        # resending input. This drains its existing reader threads and buffers.
        return process.communicate()

    try:
        try:
            if os.name == "nt" and input is not None:
                done = threading.Event()

                def communicate_input():
                    try:
                        outcome["output"] = process.communicate(input, timeout=remaining())
                    except BaseException as error:
                        outcome["error"] = error
                    finally:
                        done.set()

                communication = threading.Thread(target=communicate_input, daemon=True,
                                                 name="hls-process-communication")
                communication.start()
                if not done.wait(remaining()):
                    raise subprocess.TimeoutExpired(process.args, timeout)
                communication.join()
                if "error" in outcome:
                    raise outcome["error"]
                stdout, stderr = outcome["output"]
            else:
                stdout, stderr = process.communicate(input, timeout=remaining())
        except subprocess.TimeoutExpired as error:
            kill_process_tree(process)
            stdout, stderr = drain()
            error.output, error.stderr = stdout, stderr
            raise
        except BaseException:
            kill_process_tree(process)
            if communication is not None:
                communication.join()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        release_process_tree(process)
