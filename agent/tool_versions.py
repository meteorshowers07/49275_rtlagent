"""Exact product identity; both competition tracks require release 2025.2 only."""
import os
import re

DEFAULT_VERSION = "2025.2"
CONFIGURABLE_VERSIONS = ("2025.2",)

def required_version(variable):
    value = os.environ.get(variable, DEFAULT_VERSION).strip()
    if value not in CONFIGURABLE_VERSIONS:
        raise ValueError(variable + " must be one of " + ", ".join(CONFIGURABLE_VERSIONS))
    return value

def reported_version(output, product):
    name = r"(?:Vitis(?:[ \t]+HLS)?|vitis-run)" if product == "vitis" else "Vivado"
    versions = re.findall(
        r"^[ \t]*(?:\*+[ \t]*)?" + name +
        r"[ \t]+v?([0-9]+\.[0-9]+)(?:\.[0-9]+)*(?=[ \t\r(]|$)",
        output, re.I | re.M)
    return versions[0] if versions and len(set(versions)) == 1 else None

def probe_completed(output, product, returncode):
    # Only Vivado's observed ordinary exit 1 can accompany a valid banner.
    # A timeout/crash/launch failure is rejected separately by the evaluator.
    if returncode not in ((0, 1) if product == "vivado" else (0,)):
        return False
    diagnostic = r"^\s*(?:ERROR|FATAL(?:_ERROR)?|Segmentation fault|Abnormal program termination|Internal (?:compiler )?error|Aborted\s*\(core dumped\)|(?:Exception\s*:\s*)?(?:EXCEPTION_ACCESS_VIOLATION|STATUS_HEAP_CORRUPTION))\b"
    return reported_version(output, product) is not None and not re.search(diagnostic, output, re.I | re.M)
