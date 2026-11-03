"""Preflight must distinguish missing dependencies, mixed installs and real EDA."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import doctor
from scripts.diagnostics import workspace_check
from scripts.eda_smoke import portable


class PreflightTests(unittest.TestCase):
    def collect(self, folder, missing=(), mixed=False, banner="Vivado v2025.2"):
        def which(name):
            if name in missing:
                return None
            return str(Path(folder) / ("other" if mixed and name == "xsim" else "bin") / name)
        with patch.dict(os.environ, {"RTL_AGENT_VIVADO_VERSION": "2025.2", "HLS_AGENT_WORK_ROOT": ""}), \
             patch.object(doctor.shutil, "which", side_effect=which), \
             patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, banner, "")):
            return doctor.collect_checks(folder)

    def test_preflight_is_not_real_smoke(self):
        with tempfile.TemporaryDirectory() as folder:
            checks = self.collect(folder)
            self.assertTrue(checks["preflight_ready"])
            self.assertIsNone(checks["eda_smoke"]["passed"])
            self.assertFalse(checks["eda_ready"])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_missing_dependency_and_wrong_version(self):
        with tempfile.TemporaryDirectory() as folder:
            checks = self.collect(folder, missing=("git", "bash", "clang++", "g++", "cl"))
            self.assertFalse(checks["dependencies_available"])
            self.assertFalse(checks["preflight_ready"])
            self.assertFalse(checks["dependencies"]["host_cpp_required"])
            checks = self.collect(folder, banner="Vivado v2024.2")
            self.assertFalse(checks["version_matches"])
            self.assertFalse(checks["preflight_ready"])

    def test_missing_and_denied_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(workspace_check(Path(folder) / "missing")["available"])
            with patch("scripts.diagnostics.tempfile.TemporaryDirectory", side_effect=PermissionError("ACL denied")):
                result = workspace_check(folder)
            self.assertFalse(result["writable"])
            self.assertIn("ACL denied", result["error"])

    @unittest.skipUnless(os.name == "nt", "Windows path policy")
    def test_windows_non_ascii_path_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "中文"
            path.mkdir()
            result = workspace_check(path)
            self.assertTrue(result["writable"])
            self.assertFalse(result["path_compatible"])

    def test_portable_paths_do_not_rewrite_unrelated_paths(self):
        root = str(Path("bundle").resolve())
        self.assertEqual(portable({"log": root + os.sep + "a.log"}, [(root, ".")]), {"log": "./a.log"})
        self.assertEqual(portable(root + "_other", [(root, ".")]), root + "_other")

    def test_mixed_vivado_bins_are_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            checks = self.collect(folder, mixed=True)
        self.assertTrue(checks["tool_version_verified"])
        self.assertFalse(checks["same_installation"])
        self.assertFalse(checks["preflight_ready"])
