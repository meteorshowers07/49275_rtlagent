"""Official asynchronous protocol; run with one uvicorn worker on 127.0.0.1:7860."""
from __future__ import annotations

from contextlib import asynccontextmanager
import ctypes
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal
from urllib.request import ProxyHandler, build_opener
import uuid

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.official_io import ROOT, profile
from agent.processes import managed_popen, kill_process_tree, release_process_tree

LOG = logging.getLogger(__name__)
TRACK = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))['track']
OUT_NAME = 'solution.cpp' if TRACK == 'hls' else 'solution.v'


class SolveRequest(BaseModel):
    task_id: str = Field(min_length=1)
    nonce: str = ''
    mode: Literal['agent', 'baseline'] = 'agent'
    prompt: str = Field(min_length=1)
    interface: str = ''
    deadline_s: int = Field(default=900, gt=0)


def become_subreaper():
    if sys.platform != 'linux':
        return False
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def reap_group(pgid):
    if sys.platform != 'linux':
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pending = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                stat = (path / 'stat').read_text()
                fields = stat[stat.rindex(')') + 2:].split()
                if int(fields[1]) == os.getpid() and int(fields[2]) == pgid:
                    pending.append(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
        for pid in pending:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not pending:
            return
        time.sleep(0.02)


def vram_gb():
    """Read byte counters, never interpret a utilization percentage as GiB."""
    try:
        result = subprocess.run(['rocm-smi', '--showmeminfo', 'vram', '--json'],
                                capture_output=True, text=True, timeout=1)
        data = json.loads(result.stdout)
        used = [float(value) for card in data.values() if isinstance(card, dict)
                for key, value in card.items() if 'VRAM Total Used Memory (B)' in key]
        return round(sum(used) / 1024 ** 3, 3) if used else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def probe_health():
    declaration, config = profile()
    model = config['model']
    ready = True
    revision = declaration['model'].get('revision') or ''
    ready &= bool(revision) and not revision.startswith(('UNPINNED', 'REPLACE'))
    ready &= os.environ.get('MODEL_NAME', model) == model
    ready &= declaration['serving']['version'] not in {'', 'UNVALIDATED', 'REPLACE'}
    try:
        ready &= importlib.metadata.version('vllm') == declaration['serving']['version']
    except importlib.metadata.PackageNotFoundError:
        ready = False
    ready &= (ROOT / 'model/MODEL.md').is_file()
    ready &= all((ROOT / name).is_file() for name in ('run.sh', 'run_baseline.sh', 'baseline.py'))
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(config['endpoint'] + '/models', timeout=1) as response:
            models = json.load(response)['data']
        ready &= len(models) == 1 and models[0]['id'] == model
    except Exception:
        ready = False
    measured = vram_gb()
    ready &= measured is not None and measured > 0
    return dict(ready=bool(ready), track=TRACK, model=model, vram_gb=measured or 0.0)


class SubmissionService:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.by_nonce = {}
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.threads = []
        self.health = dict(ready=False, track=TRACK, model='unknown', vram_gb=0.0)

    def start(self):
        self.stop.clear()
        if sys.platform == 'linux' and not become_subreaper():
            raise RuntimeError('Linux submission API requires child-subreaper support')
        self.threads = [threading.Thread(target=self.worker, daemon=True),
                        threading.Thread(target=self.monitor, daemon=True)]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=10)

    def monitor(self):
        while not self.stop.is_set():
            try:
                state = probe_health()
            except Exception:
                LOG.exception('Readiness probe failed')
                state = dict(self.health, ready=False)
            with self.lock:
                self.health = state
            self.stop.wait(2)

    @staticmethod
    def public(job):
        out = {name: job[name] for name in ('job_id', 'task_id', 'status')}
        if job['status'] == 'done':
            out.update({name: job[name] for name in ('solution', 'trace', 'elapsed_s')})
        return out

    def submit(self, req):
        with self.lock:
            if req.nonce and req.nonce in self.by_nonce:
                return self.public(self.jobs[self.by_nonce[req.nonce]])
            job = dict(job_id=uuid.uuid4().hex, task_id=req.task_id, status='queued',
                       req=req, submitted=time.monotonic())
            self.jobs[job['job_id']] = job
            if req.nonce:
                self.by_nonce[req.nonce] = job['job_id']
            # Put under the same lock: concurrent callers cannot reorder the queue.
            self.queue.put(job['job_id'])
            return self.public(job)

    def worker(self):
        while not self.stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                job = self.jobs[job_id]
                job['status'] = 'running'
            try:
                solution, trace = self.solve(job['req'], job['submitted'])
            except Exception:
                LOG.exception('Task failed: %s', job_id)
                solution, trace = '', json.dumps(dict(ts=time.time(), tool='runner', rc=1, excerpt='Task runner failed; inspect service log')) + '\n'
            with self.lock:
                job.update(status='done', solution=solution, trace=trace,
                           elapsed_s=round(time.monotonic() - job['submitted'], 3))
            self.queue.task_done()

    def solve(self, req, submitted):
        deadline = submitted + req.deadline_s
        # No new process for a task whose queue budget has already been consumed.
        if deadline - time.monotonic() <= 10:
            return '', json.dumps(dict(ts=time.time(), tool='budget', rc=124, excerpt='Queue consumed remaining task budget')) + '\n'
        scratch = Path(os.environ.get('AGENT_TMP') or (tempfile.gettempdir() if os.name == 'nt' else '/var/tmp/' + TRACK + '_agent'))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='job_', dir=scratch) as tmp:
            work = Path(tmp)
            inp, out = work / 'in', work / 'out'
            inp.mkdir()
            out.mkdir()
            (inp / 'prompt.txt').write_text(req.prompt, encoding='utf-8')
            (inp / 'interface.txt').write_text(req.interface, encoding='utf-8')
            left = deadline - time.monotonic()
            env = dict(os.environ, AGENT_DEADLINE_S=str(max(1, int(left))), PYTHONUTF8='1')
            # Target Linux uses the exact executable shell entrypoints. Windows
            # developer verification executes their equivalent Python modules.
            if os.name == 'posix':
                command = [str(ROOT / ('run_baseline.sh' if req.mode == 'baseline' else 'run.sh')), str(inp), str(out)]
            elif req.mode == 'baseline':
                command = [sys.executable, str(ROOT / 'baseline.py'), str(inp), str(out), TRACK]
            else:
                command = [sys.executable, '-m', 'agent.official_io', str(inp), str(out)]
            run_deadline = deadline - 10
            if time.monotonic() >= run_deadline:
                return '', json.dumps(dict(ts=time.time(), tool='budget', rc=124)) + '\n'
            with (work / 'runner.log').open('w', encoding='utf-8') as log:
                process = managed_popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
                timed_out = False
                try:
                    while process.poll() is None:
                        if self.stop.is_set() or time.monotonic() >= run_deadline:
                            timed_out = True
                            break
                        self.stop.wait(min(0.05, max(0, run_deadline - time.monotonic())))
                    # Also sweep leftovers after a normal exit. The agent's SIGTERM
                    # handler cleans the separately contained EDA process tree.
                    kill_process_tree(process, grace_s=3)
                finally:
                    if process.poll() is None:
                        kill_process_tree(process, grace_s=1)
                    release_process_tree(process)
                    reap_group(process.pid)
            def read(name):
                try:
                    return (out / name).read_text(encoding='utf-8')
                except (OSError, UnicodeError):
                    return ''
            trace = read('trace.jsonl')
            if timed_out or process.returncode:
                excerpt = (work / 'runner.log').read_text(encoding='utf-8', errors='replace')[-2000:]
                trace += json.dumps(dict(ts=time.time(), tool='runner', rc=124 if timed_out else process.returncode,
                                         excerpt=excerpt), ensure_ascii=False) + '\n'
            return read(OUT_NAME), trace


service = SubmissionService()


@asynccontextmanager
async def lifespan(app):
    service.start()
    try:
        yield
    finally:
        service.close()


app = FastAPI(title=TRACK + ' official submission endpoint', lifespan=lifespan)


def check_token(authorization):
    token = os.environ.get('FPGACHINA_TOKEN', '')
    if not token:
        raise HTTPException(status_code=500, detail='FPGACHINA_TOKEN not set')
    if authorization != 'Bearer ' + token:
        raise HTTPException(status_code=401, detail='bad token')


@app.get('/v1/health')
def health(authorization: str | None = Header(default=None)):
    check_token(authorization)
    with service.lock:
        return dict(service.health)


@app.post('/v1/solve', status_code=202)
def solve(req: SolveRequest, authorization: str | None = Header(default=None)):
    check_token(authorization)
    return service.submit(req)


@app.get('/v1/jobs/{job_id}')
def job_status(job_id: str, authorization: str | None = Header(default=None)):
    check_token(authorization)
    with service.lock:
        if job_id not in service.jobs:
            raise HTTPException(status_code=404, detail='unknown job_id')
        return service.public(service.jobs[job_id])
