"""Feedback the agent can obtain without the official testbench.

Three tools, in increasing cost:

    check_interface()   pure Python, no tools, microseconds
    lint()              xvlog + xelab on the DUT alone, seconds
    synth()             synth_design, tens of seconds

The official testbench stays with the organisers and is what decides L2, so
nothing here can tell you whether the design is functionally correct. What the
first tool *can* tell you is whether the module will elaborate against that
testbench at all -- and on the RTL side that is worth more than it sounds:

    wrong port name   ERROR: [VRFC 10-3180] cannot find port 'q' on this module
    wrong module name ERROR: [VRFC 10-2063] Module <TopModule> not found

Both kill elaboration, so both score L0. There is no "compiles but does not
link" middle ground the way there is in C++. A deterministic textual check
against the declared interface costs nothing and removes the entire failure
class -- which is also a demonstration that a skill need not involve a model.

If Vivado is not installed, lint() and synth() degrade to rc=-1 with a clear
reason and the agent falls back to fewer rounds. check_interface() still works.
They never pretend to have checked something they did not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap

PART = os.environ.get("RTL_PART", "xczu3eg-sbva484-1-e")


class RtlToolchain:
    def __init__(self) -> None:
        self.xvlog = shutil.which("xvlog")
        self.xelab = shutil.which("xelab")
        self.vivado = shutil.which("vivado")
        self.reason = ""
        missing = [n for n, p in (("xvlog", self.xvlog), ("xelab", self.xelab),
                                  ("vivado", self.vivado)) if not p]
        if missing:
            self.reason = (f"not on PATH: {', '.join(missing)}; "
                           "source your Vivado settings64.sh")

    @property
    def available(self) -> bool:
        return not self.reason

    # ------------------------------------------------------------------ lint

    def lint(self, source: str, top: str, timeout_s: float = 300.0) -> tuple[int, str]:
        """Analyze and elaborate the DUT on its own.

        rc =  0  the module analyzes and elaborates
        rc =  1  the tools ran and rejected it
        rc = -1  the tools could not be run
        """
        if not self.available:
            return -1, f"vivado unavailable: {self.reason}"

        work = tempfile.mkdtemp(prefix="agent_lint_", dir=_scratch_dir())
        try:
            src = os.path.join(work, "solution.sv")
            with open(src, "w") as fh:
                fh.write(source)

            rc, out = _run([self.xvlog or "xvlog", "--sv", "solution.sv"], work, timeout_s)
            if rc != 0:
                return 1, summarize_log(out)

            rc, out2 = _run([self.xelab or "xelab", top, "-s", "dutsim"], work, timeout_s)
            return (0 if rc == 0 else 1), summarize_log(out + "\n" + out2)
        finally:
            if os.environ.get("AGENT_KEEP_WORK") != "1":
                shutil.rmtree(work, ignore_errors=True)

    # ----------------------------------------------------------------- synth

    def synth(self, source: str, top: str, timeout_s: float = 900.0) -> tuple[int, str]:
        if not self.available:
            return -1, f"vivado unavailable: {self.reason}"

        work = tempfile.mkdtemp(prefix="agent_synth_", dir=_scratch_dir())
        try:
            with open(os.path.join(work, "solution.sv"), "w") as fh:
                fh.write(source)
            with open(os.path.join(work, "synth.tcl"), "w") as fh:
                fh.write(
                    textwrap.dedent(
                        f"""\
                        read_verilog -sv solution.sv
                        synth_design -top {top} -part {PART} -mode out_of_context
                        """
                    )
                )
            rc, out = _run(
                [self.vivado or "vivado", "-mode", "batch", "-source", "synth.tcl",
                 "-nojournal", "-log", "synth.log"],
                work, timeout_s,
            )
            ok = rc == 0 and "Synthesis finished" in out
            return (0 if ok else 1), summarize_log(out)
        finally:
            if os.environ.get("AGENT_KEEP_WORK") != "1":
                shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------- interface checking

_MODULE_RE = re.compile(r"module\s+(\w+)\s*(?:#\s*\([^)]*\)\s*)?\((.*?)\)\s*;", re.DOTALL)


# 题面里的端口清单。VerilogEval 的 spec-to-rtl 题面长这样：
#
#     I would like you to implement a module named TopModule with the following
#     interface. All input and output ports are one bit unless otherwise specified.
#
#      - input  clk
#      - input  d   (8 bits)
#      - output q   (8 bits)
#
# 实测 156 道题里 153 道是这个格式（位宽可缺省、括号内可有多余空格、端口名后
# 可带逗号），其余 3 道直接在题面里贴完整的 module 声明，由 _ports() 兜住。
# 位宽单位兼容中英文：评测题集（VerilogEval）是英文的 `(8 bits)`，本仓库的示例题
# 是中文的 `(8 位)`。只认一种的话，带位宽的端口会被整行丢掉 —— 不是报错，是静默
# 少几个端口，比不解析更糟。
_PROMPT_PORT_RE = re.compile(
    r"^\s*-\s*(input|output|inout)\s+([A-Za-z_]\w*)\s*,?"
    r"\s*(?:\(\s*(\d+)\s*(?:bits?|位)\s*\))?\s*$",
    re.MULTILINE)


def ports_from_prompt(prompt: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """从题面还原模块名与端口表。

    **评测题集的 `interface` 是空串** —— 接口信息写在题面里，不另出一份
    （见 docs/API_CONTRACT.md）。不从题面取的话，check_interface 在评测当天会
    静默失效：输入为空、检查照常返回「通过」，而 trace 里看不出区别。
    而接口写错在本 track 判 L0，是全表最重的惩罚，这个检查恰恰最不该失效。
    """
    text = prompt or ""
    ports = [(d, int(w) if w else 1, n)
             for d, n, w in _PROMPT_PORT_RE.findall(text)]
    if not ports:
        # 注意这里**不能**回退到解析题面里的 module 声明。bugs_* 那一类题面贴的
        # 正是「有 bug 的模块」，其端口本身就是要改的东西（实测 Prob062_bugs_mux2
        # 的题面把 8 位的 out 写成 1 位，找出它正是题目要求）。把它当规格会让
        # 智能体把 bug 改回去。解析不出就如实返回空。
        # 另：该题参考实现还把 sel 极性反了（sel ? a : b），题面推不出，属数据集
        # 缺陷，见 selftest/README.md。智能体在这道题上过不了 L2 不说明修复逻辑有错。
        return None, []
    m = re.search(r"module\s+named\s+(\w+)", text)
    return (m.group(1) if m else "TopModule"), ports


def check_interface(source: str, interface: str,
                    prompt: str = "") -> tuple[int, str]:
    """Compare the generated module header against the declared one.

    Deterministic, no tools, no model. Returns (0, "ok") or (1, <diagnosis>).

    `interface` 为空时从 `prompt` 取端口表 —— 评测题集就是这种情况。

    Compares the module name and the ordered list of (direction, width, name).
    Whitespace, comments, and `wire`/`reg`/`logic` keywords are ignored -- those
    do not affect elaboration. Everything that does affect it is compared.
    """
    want_name, want_ports = _ports(interface)
    # interface 是权威来源，不符即判失败；题面是散文，推出来的只作提示。
    # 依据：Prob031_dff 的题面把输出 q 误写成 `- input q`，而模型三次都正确
    # 忽略了它并判到 L3 —— 拿题面硬比对会把这种正确答案改坏。
    advisory = False
    if want_name is None:
        want_name, want_ports = ports_from_prompt(prompt)
        advisory = want_name is not None
    got_name, got_ports = _ports(source)

    if want_name is None:
        return 0, "接口既不在 interface 里，也没能从题面解析出端口表，本次跳过检查"
    if got_name is None:
        return 1, "no module declaration found in the generated code"

    problems = []
    if got_name != want_name:
        problems.append(f"module name is '{got_name}', must be '{want_name}'")

    want_map = {n: (d, w) for d, w, n in want_ports}
    got_map = {n: (d, w) for d, w, n in got_ports}

    for name in want_map:
        if name not in got_map:
            problems.append(f"missing port '{name}'")
    for name in got_map:
        if name not in want_map:
            problems.append(f"extra port '{name}' not in the declared interface")
    for name in want_map.keys() & got_map.keys():
        wd, ww = want_map[name]
        gd, gw = got_map[name]
        if wd != gd:
            problems.append(f"port '{name}' is {gd}, must be {wd}")
        if ww != gw:
            problems.append(f"port '{name}' is {gw} bits, must be {ww}")

    if not problems:
        return 0, ("interface matches the declaration" if not advisory
                   else "interface matches the port list read from the prompt")
    if advisory:
        # 题面推出来的规格只作提示：rc=0 让流程继续，但把差异写进 trace，
        # 由调用方决定要不要喂给下一轮。硬判会把 Prob031_dff 那种
        # 「题面写错、模型写对」的正确答案改坏。
        return 0, ("⚠ 与题面端口清单不符（题面为散文，仅供参考，未拦截）: "
                   + "; ".join(problems))
    return 1, "interface mismatch: " + "; ".join(problems)


def _ports(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    stripped = re.sub(r"//[^\n]*", "", text)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    m = _MODULE_RE.search(stripped)
    if not m:
        return None, []

    ports: list[tuple[str, int, str]] = []
    direction = None
    for decl in m.group(2).split(","):
        decl = decl.strip()
        if not decl:
            continue
        d = re.match(r"\b(input|output|inout)\b", decl)
        if d:
            direction = d.group(1)
        if direction is None:
            continue          # ANSI list with a leading type-only entry
        rng = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", decl)
        width = abs(int(rng.group(1)) - int(rng.group(2))) + 1 if rng else 1
        name = re.sub(r"\[[^\]]*\]", " ", decl).split()[-1]
        ports.append((direction, width, name))
    return m.group(1), ports


# --------------------------------------------------------------------- utils


def _run(cmd: list[str], cwd: str, timeout_s: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout_s,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace",
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return 124, f"[agent] {cmd[0]} timed out after {timeout_s:.0f}s"
    except OSError as exc:
        return -1, f"[agent] could not launch {cmd[0]}: {exc}"


def _scratch_dir() -> str:
    """Local disk, never network storage.

    Vivado produces large numbers of small files. On the NFS-backed /workspace of
    the cloud platform that is pathologically slow. This is not an optimisation.

    Section 9 of docs/API_CONTRACT.md names `/tmp/eda` and the base image exports it
    as `EDA_TMP`. Honour it -- running run.sh directly (rather than through
    serve_api.py, which passes `AGENT_TMP`) otherwise lands in `/tmp`, which is
    local disk too but not the directory the platform cleans up for you.

    `TRACK_TMP` is the old name of `EDA_TMP`, kept as a fallback.
    """
    return (os.environ.get("AGENT_TMP")
            or os.environ.get("EDA_TMP")
            or os.environ.get("TRACK_TMP")
            or "/tmp")


_INTERESTING = (
    "ERROR:", "CRITICAL WARNING:", "error:",
    "Synthesis finished", "Synthesis failed", "Mismatches:",
)


def summarize_log(log: str, budget: int = 2000) -> str:
    """Compress a tool log to something a model can actually read.

    A Vivado synthesis log runs to thousands of lines. Feeding it back whole eats
    the context window and buries the one line that matters. Keep the lines that
    carry a verdict, drop the rest, and mark the truncation honestly.
    """
    lines = [ln.rstrip() for ln in log.splitlines()]
    keep = [ln for ln in lines if any(k in ln for k in _INTERESTING)]
    if not keep:
        keep = lines[-40:]

    text = "\n".join(keep)
    if len(text) > budget:
        text = text[:budget] + f"\n... [truncated, {len(text) - budget} more chars]"
    return text
