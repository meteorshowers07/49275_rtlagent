"""Data integrity and split tests; these do not invoke an HDL tool."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import os


SPEC = importlib.util.spec_from_file_location("import_verilogeval_under_test", Path(__file__).resolve().parents[1] / "scripts/import_verilogeval.py")
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


class ImportVerilogEvalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "repo"
        self.source.mkdir()
        self.dataset = self.source / importer.SUBSET
        self.dataset.mkdir()
        self.output = self.root / "export"
        self.git("init", "-q")
        (self.source / "LICENSE").write_bytes(b"Fixture license\r\nPreserve these bytes.\n")
        (self.source / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        for index in range(1, 8):
            task_id = f"Prob{index:03d}_fixture"
            prompt = "\nImplement TopModule with this interface:\n - input clk\n - output q\nOriginal prompt.\n"
            if index == 2:
                prompt = "Implement TopModule with interface:\n - input clock\n - output q\n"
            (self.dataset / (task_id + "_prompt.txt")).write_text(prompt, encoding="utf-8")
            (self.dataset / (task_id + "_ref.sv")).write_text("module RefModule; // SECRET_REFERENCE\nendmodule\n", encoding="utf-8")
            (self.dataset / (task_id + "_test.sv")).write_text("module tb; // ORIGINAL_TESTBENCH\nendmodule\n", encoding="utf-8")
        (self.dataset / "problems.txt").write_text("Auxiliary upstream metadata\n", encoding="utf-8")
        self.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                               "-C", str(self.source), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        self.revision = self.git("rev-parse", "HEAD")

    def run_import(self, output=None, **kwargs):
        return importer.import_dataset(self.source, output or self.output,
                                       expected_revision=self.revision, **kwargs)

    def read(self, relative):
        return json.loads((self.output / relative).read_text(encoding="utf-8"))

    def test_preserves_prompt_evaluation_license_and_source_hashes(self):
        provenance = self.run_import()
        task = self.read("tasks/Prob001_fixture.json")
        original = (self.dataset / "Prob001_fixture_prompt.txt").read_bytes()
        self.assertEqual(task["prompt"], original.decode("utf-8"))
        self.assertNotIn("SECRET_REFERENCE", task["prompt"])
        self.assertNotIn("ORIGINAL_TESTBENCH", task["prompt"])
        self.assertEqual((self.output / "prompts/Prob001_fixture_prompt.txt").read_bytes(), original)
        self.assertEqual((self.output / "LICENSE").read_bytes(), (self.source / "LICENSE").read_bytes())
        for suffix, path in zip(("_ref.sv", "_test.sv"), task["testbench_files"]):
            self.assertEqual((self.output / "tasks" / path).read_bytes(),
                             (self.dataset / ("Prob001_fixture" + suffix)).read_bytes())
        self.assertEqual(task["simulation_protocol"], "verilogeval_v2")
        self.assertEqual(task["top"], "TopModule")
        self.assertEqual(task["testbench_top"], "tb")
        source_key = importer.SUBSET + "/Prob001_fixture_prompt.txt"
        self.assertEqual(task["source_sha256"][source_key], hashlib.sha256(original).hexdigest())
        self.assertIn(importer.SUBSET + "/problems.txt", provenance["source_files_sha256"])
        self.assertEqual(len(provenance["source_files_sha256"]), 23)
        self.assertTrue(importer.verify_dataset(self.output)["ok"])

    def test_missing_companion_is_rejected_without_output(self):
        (self.dataset / "Prob001_fixture_ref.sv").unlink()
        self.commit()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.run_import()
        self.assertFalse(self.output.exists())

    def test_disjoint_covering_stable_hash_split_and_byte_determinism(self):
        self.run_import(split_seed=20261007, dev_fraction=0.8)
        manifest = self.read("manifest.json")["tasks"]
        dev = self.read("dev.json")["tasks"]
        holdout = self.read("holdout.json")["tasks"]
        self.assertEqual(len(dev), 5)
        self.assertEqual(len(holdout), 2)
        self.assertFalse(set(dev) & set(holdout))
        self.assertEqual(set(dev) | set(holdout), set(manifest))
        ids = [Path(path).stem for path in manifest]
        expected = sorted(ids, key=lambda task_id: hashlib.sha256(f"20261007\n{task_id}".encode()).hexdigest())
        self.assertEqual(dev + holdout, [f"tasks/{task_id}.json" for task_id in expected])
        second = self.root / "second"
        self.run_import(second, split_seed=20261007, dev_fraction=0.8)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (second / path.relative_to(self.output)).read_bytes())

    def test_small_split_nonempty_and_invalid_fractions(self):
        for fraction in (0.001, 0.999):
            with self.subTest(fraction=fraction):
                dev, holdout = importer._split(["a", "b"], 42, fraction)
                self.assertEqual((len(dev), len(holdout)), (1, 1))
                self.assertEqual(set(dev + holdout), {"a", "b"})
        self.assertEqual(importer._split(["only"], 42, 0.8), (["only"], []))
        for fraction in (0, 1, -0.1, float("nan"), float("inf")):
            with self.subTest(fraction=fraction), self.assertRaisesRegex(ValueError, "fraction"):
                self.run_import(dev_fraction=fraction)
        self.assertFalse(self.output.exists())

    def test_revision_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "revision mismatch"):
            importer.import_dataset(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_dirty_tracked_untracked_and_ignored_dataset_rejected(self):
        prompt = self.dataset / "Prob001_fixture_prompt.txt"
        original = prompt.read_bytes()
        prompt.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "dirty source"):
            self.run_import()
        prompt.write_bytes(original)
        for name in ("untracked.txt", "untracked.ignored"):
            path = self.dataset / name
            path.write_text("untracked", encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "dirty source"):
                self.run_import()
            path.unlink()
        self.assertFalse(self.output.exists())

    def test_symlinks_to_source_files_directories_and_output_rejected(self):
        link = self.dataset / "link"
        for target in (self.source / "LICENSE", self.source):
            try:
                link.symlink_to(target, target_is_directory=target.is_dir())
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege is unavailable")
                raise
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "symlink"):
                self.run_import()
            link.unlink()
        alias = self.root / "source_alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            importer.import_dataset(alias, self.output, expected_revision=self.revision)
        try:
            self.output.symlink_to(self.source / "LICENSE")
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_import()

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.run_import()
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_clock_detection_uses_only_prompt_and_warns(self):
        provenance = self.run_import()
        self.assertEqual(self.read("tasks/Prob001_fixture.json")["clock_port"], "clk")
        self.assertEqual(self.read("tasks/Prob002_fixture.json")["clock_port"], "clock")
        self.assertEqual(provenance["warnings"], [])
        for port in ("clk_i", "sys_clk", "clock", "CLK"):
            self.assertEqual(importer.detect_clock_port(f" - input logic {port}\n"), port)
        for prompt in (" - input a\n", " - input [7:0] clock\n", " - input clk\n - input clock\n"):
            self.assertIsNone(importer.detect_clock_port(prompt))

    def test_verify_detects_changed_missing_extra_files_and_links(self):
        self.run_import()
        path = self.output / "evaluation/Prob001_fixture/Prob001_fixture_test.sv"
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            importer.verify_dataset(self.output)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            importer.verify_dataset(self.output)
        path.write_bytes(original)
        extra = self.output / "extra"
        extra.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            importer.verify_dataset(self.output)
        extra.unlink()
        try:
            extra.symlink_to(self.source / "LICENSE")
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            importer.verify_dataset(self.output)


if __name__ == "__main__":
    unittest.main()
