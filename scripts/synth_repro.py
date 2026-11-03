#!/usr/bin/env python3
"""Independent minimal Vivado synthesis repro, without model or repair loop."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import eda
from scripts.doctor import collect_checks
from scripts.freeze import create_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--related-run", default="", help="Related final bundle ID")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    for directory in ("agent", "scripts"):
        shutil.copytree(ROOT / directory, out / "source_snapshot" / directory,
                        ignore=shutil.ignore_patterns("__pycache__"))
    (out / "config.json").write_text(json.dumps({"part": eda.PART, "timeout_s": args.timeout,
        "required_tool_version": eda.required_tool_version(), "experiment": "minimal combinational synthesis"}, indent=2), encoding="utf-8")
    (out / "bundle.json").write_text(json.dumps({"purpose": "experiment", "related_run": args.related_run,
        "created_utc": started, "kind": "minimal_synthesis_repro"}, indent=2), encoding="utf-8")
    checks = collect_checks(out)
    (out / "doctor.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    (out / "minimal.sv").write_text("module minimal(input wire a, b, output wire y); assign y = a & b; endmodule\n", encoding="ascii")
    (out / "synth.tcl").write_text(
        "read_verilog minimal.sv\nsynth_design -top minimal -part " + eda.PART +
        "\nreport_utilization -file utilization.rpt\nwrite_checkpoint -force minimal.dcp\nexit\n", encoding="ascii")
    # No thread/part/constraint changes: this baseline isolates a minimal design.
    if checks["tool_version_verified"]:
        result = eda._run_command([checks["tools"]["vivado"], "-mode", "batch", "-source", "synth.tcl",
                                   "-log", "vivado.log", "-journal", "vivado.jou"],
                                  out, out / "launcher.log", time.monotonic() + args.timeout)
        result["checkpoint_exists"] = (out / "minimal.dcp").is_file()
        result["passed"] = result["ok"] and result["checkpoint_exists"]
        if eda._missing_target_part((out / "launcher.log").read_text(encoding="utf-8", errors="replace")):
            result.update(status="unavailable", failure_cause="tool_environment", reason="target device support is not installed")
    else:
        result = {"passed": False, "status": "unavailable", "reason": "Matching Vivado version not verified"}
    if sys.platform == "win32":
        # Query the actual event payload; do not guess the faulting DLL from NTSTATUS.
        stamp = started.replace("+00:00", "Z")
        query = "*[System[(EventID=1000 or EventID=1001) and TimeCreated[@SystemTime >= '" + stamp + "']]]"
        try:
            events = subprocess.run(["wevtutil.exe", "qe", "Application", "/q:" + query, "/f:xml"],
                                    capture_output=True, text=True, timeout=20)
            (out / "windows_events.xml").write_text(events.stdout, encoding="utf-8")
            (out / "windows_events.log").write_text(events.stderr, encoding="utf-8")
            result["event_query_returncode"] = events.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            (out / "windows_events.log").write_text(str(exc), encoding="utf-8")
    shutil.copy2(__file__, out / "synth_repro.py")
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "README.md").write_text("# Minimal synthesis experiment\n\n" +
        "\n".join("- [" + p.name + "](" + p.name + ")" for p in sorted(out.iterdir()) if p.is_file()), encoding="utf-8")
    create_manifest(out, out / "manifest.json")
    return 0 if result["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
