"""Reproducible local-delivery package tests, independent of EDA and models."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import zipfile


SPEC = importlib.util.spec_from_file_location("package_under_test", Path(__file__).resolve().parents[1] / "scripts/package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.manifest = self.project / "local_manifest.json"
        self.output = self.root / "output" / "local.zip"
        self.write("run.sh", b"#!/bin/sh\necho local\n", 0o755)
        self.write("docs/说明.md", "开发交付\n".encode("utf-8"), 0o640)
        for name in (".git/config", "outputs/result.json", "tmp/scratch", "__pycache__/cached.pyc"):
            self.write(name, b"excluded")
        self.freeze = package.freeze.create_manifest(self.project, self.manifest)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, content, mode=0o644):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def create(self, output=None):
        return package.create_package(self.project, self.manifest, output or self.output)

    def expected(self):
        result = {"rtl_agent/" + name: digest for name, digest in self.freeze["files"].items()}
        result["rtl_agent/local_manifest.json"] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        return result

    def test_contents_hashes_fixed_metadata_and_executable_mode(self):
        result = self.create()
        expected = self.expected()
        with zipfile.ZipFile(self.output) as zipped:
            self.assertEqual(zipped.namelist(), sorted(expected))
            self.assertEqual(zipped.read("rtl_agent/local_manifest.json"), self.manifest.read_bytes())
            for entry in zipped.infolist():
                self.assertEqual(entry.date_time, package.FIXED_TIME)
                self.assertEqual(entry.create_system, 3)
                self.assertEqual(entry.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(hashlib.sha256(zipped.read(entry)).hexdigest(), expected[entry.filename])
            self.assertEqual(stat.S_IMODE(zipped.getinfo("rtl_agent/run.sh").external_attr >> 16), 0o755)
            self.assertEqual(stat.S_IMODE(zipped.getinfo("rtl_agent/docs/说明.md").external_attr >> 16), 0o644 if os.name == "nt" else 0o640)
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["source_files"], 2)
        self.assertTrue(result["not_final_submission"])
        self.assertEqual(result["archive_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(Path(str(self.output) + ".sha256").read_text(encoding='utf-8'), result["archive_sha256"] + "  local.zip\n")
        self.assertTrue(package.verify_archive(self.output, expected)["ok"])

    def test_repeated_builds_are_byte_identical_despite_source_mtime(self):
        self.create()
        for name in ("run.sh", "docs/说明.md", "local_manifest.json"):
            os.utime(self.project / name, (1700000000, 1700000000))
        second = self.output.parent / "second.zip"
        self.create(second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_dirty_source_is_rejected_without_creating_delivery(self):
        (self.project / "run.sh").write_bytes(b"modified after freeze")
        with self.assertRaisesRegex(ValueError, "does not match freeze"):
            self.create()
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".sha256").exists())

    def test_existing_destinations_unsafe_paths_and_source_symlinks_are_rejected(self):
        self.output.parent.mkdir()
        for path in (self.output, Path(str(self.output) + ".sha256")):
            path.write_bytes(b"keep existing delivery")
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "already exists"):
                self.create()
            self.assertEqual(path.read_bytes(), b"keep existing delivery")
            path.unlink()
        with self.assertRaisesRegex(ValueError, "outside the project"):
            self.create(self.project / "outputs/local.zip")
        try:
            (self.project / "linked").symlink_to(self.project / "run.sh")
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_archive_verifier_rejects_extra_missing_changed_and_bad_mode(self):
        self.create()
        with zipfile.ZipFile(self.output) as zipped:
            original = {info.filename: (info, zipped.read(info)) for info in zipped.infolist()}

        def write_variant(filename, entries):
            path = self.output.parent / filename
            with zipfile.ZipFile(path, "w") as zipped:
                for name, (info, content) in sorted(entries.items()):
                    zipped.writestr(info, content)
            return path

        entries = dict(original)
        entries.pop("rtl_agent/run.sh")
        with self.assertRaisesRegex(ValueError, "inventory"):
            package.verify_archive(write_variant("missing.zip", entries), self.expected())
        entries = dict(original)
        info = zipfile.ZipInfo("rtl_agent/extra", package.FIXED_TIME)
        entries[info.filename] = (info, b"unexpected")
        with self.assertRaisesRegex(ValueError, "inventory"):
            package.verify_archive(write_variant("extra.zip", entries), self.expected())
        entries = dict(original)
        info, content = entries["rtl_agent/run.sh"]
        entries["rtl_agent/run.sh"] = (info, content + b"tampered")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            package.verify_archive(write_variant("changed.zip", entries), self.expected())
        modes = {name: stat.S_IMODE(value[0].external_attr >> 16) for name, value in original.items()}
        modes["rtl_agent/run.sh"] = 0o644
        with self.assertRaisesRegex(ValueError, "mode mismatch"):
            package.verify_archive(self.output, self.expected(), modes)

    def test_failed_verification_cleans_own_temporary_files(self):
        with mock.patch.object(package, "verify_archive", side_effect=ValueError("injected validation failure")):
            with self.assertRaisesRegex(ValueError, "injected"):
                self.create()
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".sha256").exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
