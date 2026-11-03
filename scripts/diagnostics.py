"""Shared diagnostic helpers; probes create and remove only a private directory."""
import os
from pathlib import Path
import tempfile


def workspace_check(path):
    path = Path(path).expanduser().absolute()
    result = {"path": str(path), "writable": False, "path_compatible": True,
              "warnings": [], "policy": "Conservative Windows EDA path preflight, not an EDA execution guarantee"}
    if os.name == "nt":
        if not str(path).isascii() or any(c.isspace() for c in str(path)):
            result["path_compatible"] = False
            result["warnings"].append("Use a short ASCII directory without whitespace for Windows EDA")
        if len(str(path)) > 180:
            result["warnings"].append("Long root path leaves limited room for generated EDA paths")
        if str(path).startswith("\\\\"):
            result["path_compatible"] = False
            result["warnings"].append("Use a local drive rather than a UNC workspace")
    try:
        # Check an existing root: avoid leaving directories behind during inventory.
        with tempfile.TemporaryDirectory(prefix="doctor_", dir=path) as folder:
            probe = Path(folder) / "write_probe"
            probe.write_bytes(b"doctor workspace probe")
            if probe.read_bytes() != b"doctor workspace probe":
                raise OSError("workspace readback failed")
        result["writable"] = True
    except OSError as exc:
        result["error"] = str(exc)
    result["available"] = result["writable"] and result["path_compatible"]
    return result


def readiness(checks, work_dir, required):
    tools = checks["tools"]
    checks["dependencies"] = {
        "required": required, "missing": [name for name in required if not tools.get(name)],
        "host_cpp": next((tools.get(n) for n in ("clang++", "g++", "cl") if tools.get(n)), None),
        "host_cpp_required": False,
    }
    checks["dependencies_available"] = not checks["dependencies"]["missing"]
    checks["version_matches"] = checks["tool_version_verified"]
    checks["workspace"] = workspace_check(work_dir)
    checks["workspace_available"] = checks["workspace"]["available"]
    checks["eda_smoke"] = {"status": "not_run", "passed": None,
                           "reason": "Run scripts/eda_smoke.py separately; inventory never proves synthesis"}
    checks["preflight_ready"] = bool(checks["dependencies_available"] and checks["version_matches"]
                                      and checks.get("same_installation", True) and checks["workspace_available"])
    # Legacy field is deliberately conservative and cannot imply end-to-end success.
    checks["eda_ready"] = False
    checks["eda_ready_semantics"] = "False until real EDA smoke is independently verified; use the separate preflight fields"
    checks["notice"] = "Inventory plus temporary workspace write probe. EDA smoke, model inference, GPU and deployment acceptance remain separate."
    return checks
