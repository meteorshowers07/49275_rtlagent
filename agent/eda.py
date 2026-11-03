"""Conservative, local Vivado 2025.2 validation of a single RTL candidate.

Test benches and their pass marker are trusted task inputs. Tool exit status alone
does not prove a simulation passed. This adapter never executes generated Tcl or
shell commands; all Tcl is constructed here and subprocesses use argument lists.
The first release accepts self-contained synthesizable RTL with a limited set of
pure system functions. This is not a general Verilog or operating-system sandbox:
use isolation when accepting arbitrary external RTL or test benches.
"""

from __future__ import annotations

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


from .tool_versions import DEFAULT_VERSION, required_version, reported_version, probe_completed
from .processes import managed_popen, kill_process_tree, release_process_tree


PART = "xczu3eg-sbva484-1-e"
PERIOD_NS = 5.0
REQUIRED_VERSION = DEFAULT_VERSION  # Backward-compatible default; see required_tool_version().
def required_tool_version():
    return required_version("RTL_AGENT_VIVADO_VERSION")


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_FAILURE_WORD = r"(?:errors?|fail(?:ed|ures?)?|fatals?|mismatch(?:es)?|timeouts?)"
_PURE_SYSTEM_FUNCTIONS = frozenset({
    "$clog2", "$bits", "$signed", "$unsigned", "$size", "$left", "$right",
    "$low", "$high", "$increment", "$dimensions", "$unpacked_dimensions",
    "$countbits", "$countones", "$onehot", "$onehot0", "$isunknown",
    "$rtoi", "$itor", "$realtobits", "$bitstoreal", "$shortrealtobits",
    "$bitstoshortreal", "$ln", "$log10", "$exp", "$sqrt", "$pow",
    "$floor", "$ceil", "$sin", "$cos", "$tan", "$asin", "$acos", "$atan",
    "$atan2", "$hypot", "$sinh", "$cosh", "$tanh", "$asinh", "$acosh", "$atanh",
})


def _launch_tool(args, **kwargs):
    if os.name == 'nt':
        return managed_popen(args, **kwargs)
    return subprocess.Popen(args, **kwargs)


def _run_command(args: list[str], cwd: Path, log: Path, deadline: float) -> dict:
    """Append complete output; enforce one shared deadline and kill descendants.

    ``failure_cause`` distinguishes deadlines, launcher failures and tool crashes
    from ordinary design errors without parsing the reason string.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\nValidation deadline exhausted before command.\n")
        return {"ok": False, "reason": "total validation timeout exceeded",
                "failure_cause": "budget_exhausted"}
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\nCOMMAND " + json.dumps(args, ensure_ascii=False) + "\n")
        stream.flush()
        try:
            process = _launch_tool(
                args, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            stream.write(f"Cannot start tool: {exc}\n")
            return {"ok": False, "reason": f"cannot start tool: {exc}", "failure_cause": "tool_start_error"}
        try:
            # Account for time spent launching the tool as well.
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    kill_process_tree(process)
            except ProcessLookupError:
                pass
            process.wait()
            release_process_tree(process)
            stream.write("\nValidation timeout; process group killed.\n")
            return {"ok": False, "reason": "total validation timeout exceeded",
                    "failure_cause": "tool_timeout"}
        except BaseException:
            # The EDA tools own a separate session. A cancellation of the parent
            # (including SIGTERM translated to KeyboardInterrupt by the CLI)
            # therefore must explicitly terminate and reap this process group.
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    kill_process_tree(process)
            except ProcessLookupError:
                pass
            process.wait()
            release_process_tree(process)
            stream.write("\nValidation cancelled; process group killed and reaped.\n")
            raise
        release_process_tree(process)
        stream.write(f"\nEXIT_CODE {returncode}\n")
        stream.flush()
        crashed = _tool_crashed(returncode, _read_log(log))
        return {
            "ok": returncode == 0 and not crashed,
            "reason": (f"tool crashed (exit code {returncode})" if crashed else
                       "command completed" if returncode == 0 else f"tool exited with code {returncode}"),
            "failure_cause": "tool_crash" if crashed else None,
            "returncode": returncode,
        }


def _tool_crashed(returncode: int, output: str) -> bool:
    """Recognize process crashes, not ordinary nonzero design-error exits.

    Windows launchers may expose signed or unsigned NTSTATUS values. POSIX
    wrappers may expose a signal as a negative code or as 128 + signal.
    Explicit crash diagnostics also cover wrappers that swallow a child's code.
    """
    if (returncode & 0xF0000000) == 0xC0000000:
        return True
    crash_signals = (4, 6, 7, 8, 11)  # ILL, ABRT, BUS, FPE, SEGV
    if returncode in tuple(-value for value in crash_signals) + tuple(128 + value for value in crash_signals):
        return True
    for line in output.splitlines():
        if line.startswith(("COMMAND ", "EXIT_CODE ")):
            continue
        if re.search(
            r"^\s*(?:Segmentation fault\b|Aborted\s*\(core dumped\)|"
            r"Abnormal program termination\b|Internal (?:compiler )?error\s*:|"
            r"(?:Exception\s*:\s*)?(?:EXCEPTION_ACCESS_VIOLATION|STATUS_HEAP_CORRUPTION)\b)",
            line, re.I,
        ):
            return True
    return False


def _read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _simulation_failure(output: str) -> str | None:
    """Recognize explicit failure diagnostics without treating zero counts as errors."""
    for line in output.splitlines():
        # Our own invocation metadata is not simulator evidence.
        if line.startswith(("COMMAND ", "EXIT_CODE ")):
            continue
        text = re.sub(rf"\b(?:no|zero)\s+{_FAILURE_WORD}\b", "", line, flags=re.I)
        text = re.sub(rf"\b0\s+(?:tests?\s+)?{_FAILURE_WORD}\b", "", text, flags=re.I)
        text = re.sub(
            rf"\b{_FAILURE_WORD}(?:\s+(?:tests?|count))?(?:\s*[:=]\s*|\s+)0\b(?!\.)",
            "", text, flags=re.I,
        )
        if re.search(rf"\b{_FAILURE_WORD}\b", text, flags=re.I):
            return line.strip()[:1000]
    return None


def _tool_error(output: str) -> str | None:
    """Vivado can print ERROR diagnostics even when its wrapper returns zero."""
    for line in output.splitlines():
        if re.match(r"\s*(?:#\s*)?(?:ERROR|FATAL_ERROR)\s*:", line, re.I):
            return line.strip()[:1000]
    return None


def _missing_target_part(output: str) -> bool:
    """Only explicit tool diagnostics confirm missing fixed-target support.

    An invalid-part option alone can have other causes. Do not infer an
    installation problem from design errors or arbitrary mentions of a part.
    """
    lines = output.splitlines()
    missing = any(re.fullmatch(
        r"\s*WARNING:\s*\[Device 21-436\]\s*No parts matched\s+'"
        + re.escape(PART) + r"'\s*", line) for line in lines)
    invalid = any(re.fullmatch(
        r"\s*ERROR:\s*\[Common 17-162\]\s*Invalid option value specified for '-part'\.\s*",
        line) for line in lines)
    return missing and invalid


def _rtl_without_comments(source: str) -> str:
    # Lex comments/strings separately so comment syntax inside strings cannot hide
    # a subsequent external call. Trusted test benches are not filtered here.
    tokens = re.findall(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[^/"\n]+|.', source)
    return " ".join(token for token in tokens if not token.startswith(("//", "/*")))


def _unsafe_rtl(source: str) -> str | None:
    code = _rtl_without_comments(source)
    if re.search(r'\bimport\s*"DPI(?:-C)?"', code, re.I):
        return "generated RTL may not import DPI code"
    nonstrings = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    system_names = re.findall(r"(?<![A-Za-z0-9_$\\])\$[A-Za-z_][A-Za-z0-9_$]*", nonstrings)
    unsupported = sorted(set(system_names) - _PURE_SYSTEM_FUNCTIONS)
    if unsupported:
        return (
            "first-release RTL subset permits only pure system functions; "
            "output, termination, file IO and external calls are disabled: "
            + ", ".join(unsupported)
        )
    if re.search(r"`include\b", nonstrings):
        return "generated RTL must be self-contained; `include is disabled"
    if "``" in nonstrings:
        return "generated RTL macro token concatenation is disabled"
    return None


def _unsafe_verilogeval_rtl(source: str, testbench_top: str | None) -> str | None:
    """Limited public-task subset, not a general sandbox or a Verilog parser.

    Generated modules may not name the public harness/reference or access dotted
    hierarchy. The latter also deliberately excludes struct/interface member
    syntax in this development adapter. Named instance port connections are fine.
    """
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', _rtl_without_comments(source))
    reserved = {"tb", "RefModule", "stimulus_gen"}
    if testbench_top:
        reserved.add(testbench_top)
    # Escaped identifiers and macro concatenation could defeat this deliberately
    # small lexical check; the public task subset does not require either.
    if re.search(r"\\\S+", code):
        return "verilogeval_v2 development subset does not support escaped identifiers"
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", code))
    forbidden = names & reserved
    if forbidden:
        return "generated RTL may not name trusted harness/reference identifiers: " + ", ".join(sorted(forbidden))
    if re.search(r"\b(?:bind|force|release|defparam)\b", code):
        return "verilogeval_v2 development subset forbids bind, force, release and defparam"
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_$]*(?:\s*\[[^\]]*\])*\s*\.\s*[A-Za-z_]", code):
        return "verilogeval_v2 development subset forbids hierarchical/member access in generated RTL"
    return None


def _verilogeval_v2_failure(output: str) -> str | None:
    """Verify the original public v2 testbench's three final summary lines.

    Source: NVlabs/verilog-eval dataset_spec-to-rtl/Prob001_zero_test.sv.
    Its TIMEOUT path also executes the final block, so zero mismatches and a
    simulator exit code of zero are insufficient without the timeout check.
    This is a development protocol adapter, not official competition scoring.
    """
    lines = [line.strip() for line in output.splitlines()
             if not line.startswith(("COMMAND ", "EXIT_CODE "))]
    if any(re.search(r"\bTIMEOUT\b", line, re.I) for line in lines):
        return "VerilogEval testbench reported TIMEOUT"
    diagnostic = _tool_error("\n".join(lines))
    if diagnostic:
        return "simulation reported a tool error: " + diagnostic
    failure = _simulation_failure("\n".join(lines))
    if failure:
        return "simulation reported failure: " + failure
    patterns = (
        ("mismatch summary", r"^Mismatches\b|\bMismatches\s*[:=]", r"Mismatches: ([0-9]{1,20}) in ([0-9]{1,20}) samples"),
        ("total-sample hint", r"\bTotal mismatched samples\b",
         r"Hint: Total mismatched samples is ([0-9]{1,20}) out of ([0-9]{1,20}) samples"),
        ("completion line", r"\bSimulation finished\b", r"Simulation finished at ([0-9]{1,20}) ps"),
    )
    parsed = []
    for label, candidate_pattern, exact_pattern in patterns:
        candidates = [(index, line) for index, line in enumerate(lines)
                      if re.search(candidate_pattern, line, re.I)]
        if len(candidates) != 1:
            return f"VerilogEval requires exactly one {label}; found {len(candidates)}"
        index, line = candidates[0]
        match = re.fullmatch(exact_pattern, line)
        if not match:
            return f"VerilogEval malformed/ambiguous {label}: {line[:1000]}"
        parsed.append((index, tuple(int(value) for value in match.groups())))
    summary, hint, completion = parsed
    if summary[1][0] != 0 or hint[1][0] != 0:
        return "VerilogEval mismatch count must be exactly zero"
    if summary[1][1] <= 0 or hint[1][1] <= 0:
        return "VerilogEval must check at least one sample"
    if summary[1] != hint[1]:
        return "VerilogEval mismatch summary and total-sample hint disagree"
    if not hint[0] < completion[0] < summary[0]:
        return "VerilogEval final summary lines are out of order"
    return None


class _EvaluationAssetError(ValueError):
    """The trusted evaluation inputs no longer match this run's task snapshot."""


def _copy_assets(copies: list[tuple[Path, Path]], task: dict) -> None:
    """Copy trusted inputs, then verify the bytes the tools will actually read.

    The snapshot is taken from the destination rather than from a pre-copy read,
    so a testbench rewritten during the copy cannot pass on stale bytes.
    """
    snapshots = task.get("asset_sha256")
    if snapshots is not None and (not isinstance(snapshots, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in snapshots.items())):
        raise _EvaluationAssetError("asset_sha256 must map asset paths to SHA-256 digests")
    for original, destination in copies:
        try:
            shutil.copyfile(original, destination)
            with destination.open("rb") as stream:
                fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise _EvaluationAssetError(f"Cannot snapshot evaluation asset {original}: {exc}") from exc
        if snapshots is not None and snapshots.get(str(original.resolve())) != fingerprint:
            raise _EvaluationAssetError("Evaluation asset differs from task.asset_sha256: " + str(original))


def _write_synthesis_script(run_dir: Path, top: str, clock_port: str | None) -> None:
    # Identifiers were validated; braces prevent substitution of legal '$' chars.
    if clock_port:
        clock = (
            f"create_clock -name task_clock -period {PERIOD_NS:.3f} "
            f"[get_ports {{{clock_port}}}]\n"
        )
        clock_check = (
            f"if {{[llength [get_ports -quiet {{{clock_port}}}]] != 1}} "
            '{error "Specified clock_port does not identify one top-level port"}\n'
        )
    else:
        clock = f"create_clock -name virtual_clock -period {PERIOD_NS:.3f}\n"
        clock_check = ""
    (run_dir / "constraints.xdc").write_text(clock, encoding="utf-8")
    script = (
        "read_verilog -sv {design.sv}\n"
        "read_xdc {constraints.xdc}\n"
        f"synth_design -top {{{top}}} -part {{{PART}}}\n"
        + clock_check
        + "report_utilization -file {utilization.rpt}\n"
        "report_timing_summary -file {timing_summary.rpt}\n"
        "report_timing -max_paths 10 -file {timing_paths.rpt}\n"
        "write_verilog -force -mode funcsim {netlist.v}\n"
        "exit\n"
    )
    (run_dir / "synthesize.tcl").write_text(script, encoding="utf-8")


def validate_rtl(code_path: Path, task: dict, work_dir: Path, timeout_s: float) -> dict:
    """Compile, simulate, then synthesize; unavailable tools never count as a pass.

    ``simulation_pass_marker`` is a literal, complete output line supplied by the
    task author under the default ``marker`` protocol. ``verilogeval_v2`` instead
    requires the original public testbench's consistent, positive-sample summary
    and completion lines, with no failure or TIMEOUT. It needs no custom marker.
    Missing test benches/markers yield unverified simulation. Reports
    and netlist are retained beside the per-stage logs, in a fresh attempt folder.
    ``highest_stage`` counts consecutive verified stages (0 through 3).

    The one ``timeout_s`` budget is shared by the version probe, compilation,
    elaboration, simulation and synthesis, and synthesis runs last, so it is the
    stage that loses time first. A deadline failure is reported as ``failed`` (a
    sample the official pass rate must not silently drop) and is additionally
    labelled with ``failure_cause`` and ``budget_exhausted`` so a starved budget
    is never confused with a design error. The labels are added only when they
    apply. Tool crashes and launcher errors have their own failure cause, without
    a budget label; they remain counted failures. Explicit fixed-target device
    absence is unavailable/tool_environment, retaining compile/simulation evidence
    and stopping model repairs. An ordinary result keeps exactly ``status``, ``highest_stage``,
    ``stages`` and ``feedback``.
    """
    started = time.monotonic()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="eda_", dir=work_dir))
    stages = []
    for name in ("compile", "simulate", "synthesize"):
        log = run_dir / f"{name}.log"
        log.touch()
        stages.append({
            "name": name, "status": "unverified", "reason": "not run: prerequisite not verified",
            "wall_s": 0.0, "log": str(log),
        })
    # Non-None only for a stage that failed for a reason other than the design.
    failure_cause: dict[str, str] = {}

    def finish(status: str) -> dict:
        highest = 0
        for stage in stages:
            if stage["status"] != "passed":
                break
            highest += 1
        summary = "\n".join(f"{s['name']}: {s['status']}: {s['reason']}" for s in stages)
        failed = next((s for s in stages if s["status"] != "passed"), stages[-1])
        tail = _read_log(Path(failed["log"]))[-4800:]
        result = {
            "status": status, "highest_stage": highest, "stages": stages,
            "feedback": (summary[:1100] + ("\nLog tail:\n" + tail if tail else ""))[-6000:],
        }
        cause = failure_cause.get(failed["name"])
        if cause:
            result["failure_cause"] = cause
            if cause in ("tool_timeout", "budget_exhausted"):
                result["budget_exhausted"] = failed["name"] == "synthesize"
        return result

    def set_stage(index: int, status: str, reason: str, stage_start: float,
                  cause: str | None = None) -> None:
        stages[index].update(status=status, reason=reason, wall_s=round(time.monotonic() - stage_start, 4))
        if cause is not None:
            failure_cause[stages[index]["name"]] = cause

    try:
        timeout_s = float(timeout_s)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        deadline = started + timeout_s
        top = task.get("top")
        if not isinstance(top, str) or not _IDENTIFIER.fullmatch(top):
            raise ValueError("top must be a simple Verilog identifier")
        clock_port = task.get("clock_port")
        if clock_port is not None and (not isinstance(clock_port, str) or not _IDENTIFIER.fullmatch(clock_port)):
            raise ValueError("clock_port must be null or a simple Verilog identifier")
        testbench_files = task.get("testbench_files") or []
        if not isinstance(testbench_files, list):
            raise ValueError("testbench_files must be a list of absolute paths")
        tb_top = task.get("testbench_top")
        if testbench_files and (not isinstance(tb_top, str) or not _IDENTIFIER.fullmatch(tb_top)):
            raise ValueError("testbench_top is required when testbench_files are provided")
        protocol = task.get("simulation_protocol", "marker")
        if protocol not in ("marker", "verilogeval_v2"):
            raise ValueError("simulation_protocol must be marker or verilogeval_v2")
        marker = task.get("simulation_pass_marker")
        if marker is not None and (not isinstance(marker, str) or not marker.strip() or "\n" in marker or "\r" in marker):
            raise ValueError("simulation_pass_marker must be a nonempty, single-line literal")
        source = Path(code_path).read_text(encoding="utf-8")
        unsafe_reason = _unsafe_rtl(source)
        if unsafe_reason:
            raise ValueError(unsafe_reason)
        if protocol == "verilogeval_v2":
            unsafe_reason = _unsafe_verilogeval_rtl(source, tb_top)
            if unsafe_reason:
                raise ValueError(unsafe_reason)
        (run_dir / "design.sv").write_text(source, encoding="utf-8")
        copies = []
        tb_names = []
        for index, file in enumerate(testbench_files):
            original = Path(file)
            if not original.is_absolute() or not original.is_file():
                raise ValueError("each testbench file must be an existing absolute file path")
            name = f"testbench_{index}.sv"
            copies.append((original, run_dir / name))
            tb_names.append(name)
        # Verify the copied bytes rather than trusting the load-time path list: a
        # testbench edited between task loading and this run must not be evaluated
        # under the identity its snapshot was recorded for.
        _copy_assets(copies, task)
    except _EvaluationAssetError as exc:
        set_stage(0, "unavailable",
                  "Evaluation asset integrity check failed before compilation: " + str(exc), started)
        return finish("unavailable")
    except (ValueError, TypeError, OSError) as exc:
        set_stage(0, "failed", f"invalid validation input: {exc}", started)
        return finish("failed")

    try:
        expected = required_tool_version()
    except ValueError as exc:
        set_stage(0, "unavailable", "invalid tool version configuration: " + str(exc), started)
        return finish("unavailable")
    paths = {name: shutil.which(name) for name in ("vivado", "xvlog", "xelab", "xsim")}
    missing = [name for name in ("vivado", "xvlog", "xelab") if not paths[name]]
    if missing:
        set_stage(0, "unavailable", "required tools not found on PATH: " + ", ".join(missing), started)
        return finish("unavailable")

    compile_log = Path(stages[0]["log"])
    version_log = run_dir / "version.log"
    version = _run_command([paths["vivado"], "-version"], run_dir, version_log, deadline)
    version_output = _read_log(version_log)
    with compile_log.open("a", encoding="utf-8") as stream:
        stream.write("VERSION CHECK\n" + version_output)
    actual = reported_version(version_output, "vivado")
    # Execution failures take precedence even if a banner was flushed first.
    if (version.get("failure_cause") or _tool_error(version_output)
            or (not version["ok"] and not probe_completed(
                version_output, "vivado", version.get("returncode", 1)))):
        set_stage(0, "failed", "Vivado version check failed: " + version["reason"], started,
                  cause=version.get("failure_cause"))
        return finish("failed")
    if actual != expected:
        set_stage(0, "unavailable", f"requires Vivado {expected}; executable reported {actual}", started)
        return finish("unavailable")
    # Vivado tool wrappers must be from the same selected installation directory.
    # Resolve only their parent: launcher scripts can be symlinks to common loaders.
    vivado_bin = Path(paths["vivado"]).parent.resolve()
    for name in ("xvlog", "xelab", "xsim"):
        if paths[name] and Path(paths[name]).parent.resolve() != vivado_bin:
            set_stage(0, "unavailable", f"{name} is not from the selected Vivado bin directory", started)
            return finish("unavailable")

    commands = [
        [paths["xvlog"], "--sv", "design.sv", *tb_names],
        [paths["xelab"], tb_top if tb_names else top, "-s", "sim_snapshot", "-timescale", "1ps/1ps"],
    ]
    for command in commands:
        result = _run_command(command, run_dir, compile_log, deadline)
        if not result["ok"]:
            set_stage(0, "failed", result["reason"], started, cause=result.get("failure_cause"))
            return finish("failed")
        diagnostic = _tool_error(_read_log(compile_log))
        if diagnostic:
            set_stage(0, "failed", "compilation reported an error: " + diagnostic, started)
            return finish("failed")
    set_stage(0, "passed", f"Vivado {actual} compilation and elaboration completed", started)

    sim_start = time.monotonic()
    if not tb_names:
        set_stage(1, "unverified", "no trusted testbench supplied; functional correctness was not checked", sim_start)
        return finish("unverified")
    if not paths["xsim"]:
        set_stage(1, "unavailable", "xsim not found on PATH", sim_start)
        return finish("unavailable")
    sim_log = Path(stages[1]["log"])
    result = _run_command([paths["xsim"], "sim_snapshot", "-runall"], run_dir, sim_log, deadline)
    if not result["ok"]:
        set_stage(1, "failed", result["reason"], sim_start, cause=result.get("failure_cause"))
        return finish("failed")
    sim_output = _read_log(sim_log)
    failure = (_verilogeval_v2_failure(sim_output) if protocol == "verilogeval_v2"
               else (_tool_error(sim_output) or _simulation_failure(sim_output)))
    if failure:
        set_stage(1, "failed", "simulation reported failure: " + failure, sim_start)
        return finish("failed")
    if protocol == "marker" and marker is None:
        set_stage(1, "unverified", "no trusted simulation_pass_marker configured", sim_start)
        return finish("unverified")
    if protocol == "marker" and not any(line.strip() == marker.strip() for line in sim_output.splitlines()):
        set_stage(1, "failed", "simulation did not emit the trusted pass marker as a complete line", sim_start)
        return finish("failed")
    sim_reason = ("trusted VerilogEval v2 testbench completed with consistent zero-mismatch, positive-sample summaries"
                  if protocol == "verilogeval_v2"
                  else "trusted testbench emitted the configured pass marker without failure diagnostics")
    set_stage(1, "passed", sim_reason, sim_start)

    synth_start = time.monotonic()
    _write_synthesis_script(run_dir, top, clock_port)
    synth_log = Path(stages[2]["log"])
    result = _run_command(
        [paths["vivado"], "-mode", "batch", "-notrace", "-source", "synthesize.tcl"],
        run_dir, synth_log, deadline,
    )
    if not result.get("failure_cause") and _missing_target_part(_read_log(synth_log)):
        set_stage(2, "unavailable", f"selected Vivado installation lacks target part {PART}; install its device support before retrying",
                  synth_start, cause="tool_environment")
        return finish("unavailable")
    if not result["ok"]:
        set_stage(2, "failed", result["reason"], synth_start, cause=result.get("failure_cause"))
        return finish("failed")
    diagnostic = _tool_error(_read_log(synth_log))
    if diagnostic:
        set_stage(2, "failed", "synthesis reported an error: " + diagnostic, synth_start)
        return finish("failed")
    required = ("netlist.v", "utilization.rpt", "timing_summary.rpt", "timing_paths.rpt")
    absent = [name for name in required if not (run_dir / name).is_file() or (run_dir / name).stat().st_size == 0]
    if absent:
        set_stage(2, "failed", "tool exited successfully but required outputs are missing/empty: " + ", ".join(absent), synth_start)
        return finish("failed")
    set_stage(2, "passed", f"synthesis produced netlist and reports for {PART}; {PERIOD_NS:g} ns clock constraint", synth_start)
    return finish("passed")
