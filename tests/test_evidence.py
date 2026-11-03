"""Evidence bundles survive relocation and retain original imported bytes."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import eda_smoke
from scripts.freeze import verify_manifest


class EvidenceTests(unittest.TestCase):
    def invoke(self, argv):
        with patch.object(sys, "argv", ["eda_smoke.py", *map(str, argv)]), \
             patch.object(eda_smoke, "collect_checks", return_value={"test_fixture": True}), \
             patch.dict(os.environ, {"RTL_AGENT_VIVADO_VERSION": "2025.2"}), redirect_stdout(io.StringIO()):
            return eda_smoke.main()

    def test_import_preserves_original_and_portable_view_survives_move(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "original"
            run.mkdir()
            (run / "compile.log").write_bytes(b"complete log\n")
            raw = json.dumps({"validation": {"status": "failed", "stages": [{"log": str(run / "compile.log")}]}}).encode()
            (run / "result.json").write_bytes(raw)
            out = root / "bundle"
            self.assertEqual(self.invoke(["--archive-run", run, "--output", out, "--purpose", "final"]), 0)
            moved = root / "moved"
            shutil.move(str(out), moved)
            self.assertEqual((moved / "run/result.json").read_bytes(), raw)
            self.assertEqual(json.loads((moved / "missing_references.json").read_text()), [])
            view = json.loads((moved / "result.json").read_text())
            self.assertTrue((moved / view["validation"]["stages"][0]["log"]).is_file())
            verify_manifest(moved, moved / "manifest.json")

    @unittest.skipUnless(os.name == "nt", "Windows path aliases")
    def test_portable_normalizes_case_and_separators_without_mapping_siblings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "original"
            root.mkdir()
            log = root / "compile.log"
            log.write_text("fixture")
            alias = str(log).upper().replace("\\", "/")
            view = eda_smoke.portable(alias, [(str(root), "run")])
            self.assertEqual(view.lower(), "run/compile.log")
            sibling = str(root) + "_other/compile.log"
            self.assertEqual(eda_smoke.portable(sibling, [(str(root), "run")]), sibling)

    def test_external_missing_log_is_not_hidden(self):
        result = {"stages": [{"log": "C:/another_machine/missing.log"}]}
        self.assertEqual(len(eda_smoke.missing_references(result, Path.cwd())), 1)

    def test_smoke_keeps_external_eda_logs_and_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "external_work"
            work.mkdir()
            (work / "synth.tcl").write_text("exit")
            (work / "full.log").write_text("full tool output")
            (work / "design.dcp").write_bytes(b"artifact fixture")
            result = {"status": "passed", "work_dir": str(work), "stages": [{"log": str(work / "full.log")}]}
            out = root / "bundle"
            with patch.object(eda_smoke.eda, "validate_rtl", return_value=result):
                code = self.invoke(["--task", eda_smoke.ROOT / "examples/counter.json", "--source", eda_smoke.ROOT / "examples/counter_reference.sv", "--output", out, "--purpose", "experiment"])
            self.assertEqual(code, 0)
            self.assertEqual((out / "external_eda/design.dcp").read_bytes(), b"artifact fixture")
            self.assertEqual(json.loads((out / "result.json").read_text())["stages"][0]["log"], "external_eda/full.log")
            verify_manifest(out, out / "manifest.json")

    def test_pair_archive_preserves_both_results(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "pair"
            run.mkdir()
            pair = {"baseline": {"validation": {"status": "failed"}}, "agent": {"validation": {"status": "passed"}}}
            (run / "pair.json").write_text(json.dumps(pair))
            out = root / "bundle"
            self.assertEqual(self.invoke(["--archive-run", run, "--output", out, "--purpose", "final"]), 0)
            self.assertEqual(json.loads((out / "bundle.json").read_text())["result_status"], {"baseline": "failed", "agent": "passed"})
            verify_manifest(out, out / "manifest.json")
