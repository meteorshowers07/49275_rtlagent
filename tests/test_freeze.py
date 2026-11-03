"""Tests for portable, safe, complete submission manifests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import os


SPEC = importlib.util.spec_from_file_location("freeze_under_test", Path(__file__).resolve().parents[1] / "scripts/freeze.py")
freeze = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(freeze)


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, content="hello"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_stable_manifest_includes_model_metrics_and_unicode(self):
        for name in ("model/weights.bin", "metrics/report.json", "serve/main.py", "设计说明.md"):
            self.write(name)
        for name in ("outputs/generated.txt", ".git/config", "x/__pycache__/mod.pyc", "tmp/scratch"):
            self.write(name)
        manifest = freeze.create_manifest(self.root, self.manifest)
        self.assertEqual(set(manifest["files"]), {"model/weights.bin", "metrics/report.json",
                                                 "serve/main.py", "设计说明.md"})
        self.assertEqual(manifest["files"]["model/weights.bin"], hashlib.sha256(b"hello").hexdigest())
        content = self.manifest.read_bytes()
        freeze.create_manifest(self.root, self.manifest)
        self.assertEqual(self.manifest.read_bytes(), content)
        self.assertTrue(freeze.verify_manifest(self.root, self.manifest)["ok"])

    def test_modified_missing_and_added_detected(self):
        self.write("modified", "before")
        missing = self.write("missing")
        self.write("unchanged")
        freeze.create_manifest(self.root, self.manifest)
        self.write("modified", "after")
        missing.unlink()
        self.write("added")
        result = freeze.verify_manifest(self.root, self.manifest)
        self.assertFalse(result["ok"])
        self.assertEqual(result["modified"], ["modified"])
        self.assertEqual(result["missing"], ["missing"])
        self.assertEqual(result["added"], ["added"])

    def test_new_outputs_do_not_change_freeze(self):
        self.write("code.py")
        freeze.create_manifest(self.root, self.manifest)
        self.write("outputs/new/result.json")
        self.assertTrue(freeze.verify_manifest(self.root, self.manifest)["ok"])

    def test_external_manifest_supported(self):
        project = self.root / "project"
        project.mkdir()
        self.write("project/source.py")
        freeze.create_manifest(project, self.manifest)
        self.assertTrue(freeze.verify_manifest(project, self.manifest)["ok"])

    def test_symlink_files_and_directories_rejected(self):
        self.write("code.py")
        freeze.create_manifest(self.root, self.manifest)
        link = self.root / "link.py"
        try:
            link.symlink_to(self.root / "code.py")
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            freeze.verify_manifest(self.root, self.manifest)
        with self.assertRaisesRegex(ValueError, "symlink"):
            freeze.create_manifest(self.root, self.manifest)
        link.unlink()
        directory = self.root / "nested"
        directory.mkdir()
        try:
            link.symlink_to(directory, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            freeze.create_manifest(self.root, self.manifest)

    def test_manifest_symlink_rejected(self):
        destination = self.write("destination.json", "untouched")
        try:
            self.manifest.symlink_to(destination)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            freeze.create_manifest(self.root, self.manifest)
        self.assertEqual(destination.read_text(encoding="utf-8"), "untouched")

    def test_unsafe_paths_and_bad_hashes_rejected(self):
        data = freeze.create_manifest(self.root, self.manifest)
        for name in ("../escape", "/absolute", "sub/../escape", "sub//file", "./file", "C:/escape", "a\\b"):
            data["files"] = {name: "0" * 64}
            self.manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                freeze.verify_manifest(self.root, self.manifest)
        data["files"] = {"valid": "invalid-hash"}
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            freeze.verify_manifest(self.root, self.manifest)

    def test_exclusions_cannot_be_changed_by_manifest(self):
        data = freeze.create_manifest(self.root, self.manifest)
        data["excluded_directories"].append("model")
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exclusions"):
            freeze.verify_manifest(self.root, self.manifest)

    def test_manifest_self_reference_and_duplicate_keys_rejected(self):
        data = freeze.create_manifest(self.root, self.manifest)
        data["files"] = {"manifest.json": "0" * 64}
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "own file"):
            freeze.verify_manifest(self.root, self.manifest)
        self.manifest.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            freeze.verify_manifest(self.root, self.manifest)


if __name__ == "__main__":
    unittest.main()
