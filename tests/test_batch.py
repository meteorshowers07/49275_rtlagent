"""Batch execution tests: fresh clients, independent seeds, and durable failures."""

from __future__ import annotations

import json
import hashlib
from agent.client import LocalClient
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from agent.core import ROOT
from scripts import batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tasks = []
        for index in range(2):
            path = self.root / f"task{index}.json"
            path.write_text(json.dumps({"task_id": f"task/{index}", "prompt": "Implement a counter.",
                                        "top": "counter"}), encoding="utf-8")
            self.tasks.append(path)
        self.replay = ROOT / "examples/replay.json"
        self.output = self.root / "batch"

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_tasks_two_seeds_restart_replay_and_exclude_all_mock(self):
        report = batch.run_batch(self.tasks, self.output, seeds=[0, 1], replay=self.replay)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["requested_evaluation_kind"], "mock")
        self.assertEqual(report["evaluation_kind"], "mock")
        self.assertEqual(report["observed_evaluation_kinds"], ["mock"])
        self.assertEqual(report["counts"]["completed"], 4)
        self.assertGreater(report["wall_s"], 0)
        self.assertEqual(len({entry["output"] for entry in report["runs"]}), 4)
        pairs = []
        for entry in report["runs"]:
            self.assertTrue((self.output / entry["stdout_log"]).is_file())
            self.assertTrue((self.output / entry["stderr_log"]).is_file())
            pair = json.loads((self.output / entry["output"] / "pair.json").read_text(encoding='utf-8'))
            pairs.append(pair)
            self.assertEqual(pair["baseline"]["seed"], entry["seed"])
            self.assertEqual(pair["agent"]["seed"], entry["seed"])
            self.assertEqual(pair["agent"]["task_digest"], entry["task_digest"])
            self.assertEqual(pair["agent"]["config_digest"], report["config_digest"])
            self.assertNotIn("error", pair["agent"])
        self.assertEqual(len({pair["paired_run_id"] for pair in pairs}), 4)
        summary = json.loads((self.output / "summary.json").read_text(encoding='utf-8'))
        self.assertEqual(summary["input"]["eligible_runs"], 0)
        self.assertEqual(summary["input"]["excluded_counts"]["mock"], 8)
        self.assertEqual(json.loads((self.output / "batch.json").read_text(encoding='utf-8')), report)

    def test_cli_manifest_resolves_paths_relative_to_manifest(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"tasks": ["task0.json"]}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/batch.py"),
                                 "--task-manifest", str(manifest), "--output", str(self.output),
                                 "--seeds", "9", "--replay", str(self.replay)],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.output / "batch.json").read_text(encoding='utf-8'))
        self.assertEqual(report["seeds"], [9])
        self.assertEqual(report["tasks"][0]["source"], str(self.tasks[0].resolve()))

    def test_duplicate_seeds_existing_output_and_invalid_tasks_fail_before_launch(self):
        with patch.object(batch, "managed_popen") as popen:
            with self.assertRaisesRegex(ValueError, "unique integers"):
                batch.run_batch(self.tasks, self.output, seeds=[1, 1])
            self.assertFalse(self.output.exists())
            with self.assertRaisesRegex(ValueError, "task_id must be unique"):
                batch.run_batch([self.tasks[0], self.tasks[0]], self.output, seeds=[1])
            self.assertFalse(self.output.exists())
            bad_task = self.root / "bad.json"
            bad_task.write_text('{"task_id":"broken"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid task"):
                batch.run_batch([self.tasks[0], bad_task], self.output, seeds=[1])
            self.assertFalse(self.output.exists())
            self.output.mkdir()
            sentinel = self.output / "existing"
            sentinel.write_text("preserve", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                batch.run_batch(self.tasks, self.output, seeds=[1])
            self.assertEqual(sentinel.read_text(encoding='utf-8'), "preserve")
            popen.assert_not_called()

    def test_launch_failure_does_not_stop_later_pair_and_metadata_survives(self):
        real_popen = batch.managed_popen
        attempts = 0

        def launch(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated process startup failure")
            return real_popen(*args, **kwargs)

        with patch.object(batch, "managed_popen", side_effect=launch):
            report = batch.run_batch(self.tasks, self.output, seeds=[0], replay=self.replay)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual([entry["status"] for entry in report["runs"]], ["launch_error", "completed"])
        self.assertIsNone(report["runs"][0]["returncode"])
        self.assertIn("startup failure", (self.output / report["runs"][0]["stderr_log"]).read_text(encoding='utf-8'))
        self.assertEqual(json.loads((self.output / "batch.json").read_text(encoding='utf-8')), report)
        summary = json.loads((self.output / "summary.json").read_text(encoding='utf-8'))
        self.assertEqual(summary["input"]["excluded_counts"]["mock"], 2)

    @unittest.skipUnless(os.name == "posix", "process group cleanup uses POSIX killpg")
    def test_timeout_requests_cleanup_reaps_and_continues(self):
        timed_out = Mock(pid=43210, returncode=-signal.SIGTERM)
        timed_out.wait.side_effect = [subprocess.TimeoutExpired("pair", 1), -signal.SIGTERM]
        real_popen = batch.managed_popen
        attempts = 0

        def launch(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            self.assertFalse(kwargs["shell"])
            self.assertTrue(kwargs["start_new_session"])
            return timed_out if attempts == 1 else real_popen(*args, **kwargs)

        with patch.object(batch, "managed_popen", side_effect=launch), patch.object(batch.os, "killpg") as killpg:
            report = batch.run_batch(self.tasks, self.output, seeds=[0], replay=self.replay)
        self.assertEqual(killpg.call_args_list, [call(timed_out.pid, signal.SIGTERM), call(timed_out.pid, signal.SIGKILL)])
        self.assertEqual(timed_out.wait.call_count, 2)
        self.assertEqual(timed_out.wait.call_args, call(timeout=3.0))
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual([entry["status"] for entry in report["runs"]], ["timed_out", "completed"])
        self.assertEqual(report["runs"][0]["returncode"], -signal.SIGTERM)
        self.assertEqual(batch.pair_timeout_s({"run_timeout_s": 10, "tool_timeout_s": 3}), 53)
        self.assertEqual(report["pair_timeout_s"], 450)
        self.assertTrue((self.output / "summary.json").is_file())

    @unittest.skipUnless(os.name == "posix", "process group cleanup uses POSIX killpg")
    def test_cleanup_escalates_to_kill_only_after_grace_timeout(self):
        timed_out = Mock(pid=43213, returncode=-signal.SIGKILL)
        timed_out.wait.side_effect = [subprocess.TimeoutExpired("pair", 1),
                                     subprocess.TimeoutExpired("pair", 3), -signal.SIGKILL]
        with patch.object(batch, "managed_popen", return_value=timed_out), patch.object(batch.os, "killpg") as killpg:
            report = batch.run_batch(self.tasks[:1], self.output, seeds=[0], replay=self.replay)
        self.assertEqual(killpg.call_args_list, [call(timed_out.pid, signal.SIGTERM),
                                                call(timed_out.pid, signal.SIGKILL)])
        self.assertEqual(timed_out.wait.call_args_list,
                         [call(timeout=report["pair_timeout_s"]), call(timeout=3.0), call()])
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["runs"][0]["status"], "timed_out")
        self.assertEqual(report["runs"][0]["returncode"], -signal.SIGKILL)

    @unittest.skipUnless(os.name == "posix", "process group cleanup uses POSIX killpg")
    def test_interruption_cleans_child_marks_pending_and_saves_metadata(self):
        child = Mock(pid=43211, returncode=-signal.SIGTERM)
        child.wait.side_effect = [batch.BatchInterrupted(signal.SIGTERM), -signal.SIGTERM]
        old_handler = signal.getsignal(signal.SIGTERM)
        with patch.object(batch, "managed_popen", return_value=child) as popen, patch.object(batch.os, "killpg") as killpg:
            report = batch.run_batch(self.tasks, self.output, seeds=[0], replay=self.replay)
        popen.assert_called_once()
        self.assertEqual(killpg.call_args_list, [call(child.pid, signal.SIGTERM), call(child.pid, signal.SIGKILL)])
        self.assertEqual(report["exit_code"], 128 + signal.SIGTERM)
        self.assertEqual([entry["status"] for entry in report["runs"]], ["interrupted", "skipped"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_handler)
        self.assertEqual(json.loads((self.output / "batch.json").read_text(encoding='utf-8')), report)
        self.assertTrue((self.output / "summary.json").is_file())

    def test_nonzero_child_exit_is_batch_failure_and_later_pairs_continue(self):
        failed = Mock(pid=43212, returncode=7)
        failed.wait.return_value = 7
        with patch.object(batch, "managed_popen", return_value=failed):
            report = batch.run_batch(self.tasks, self.output, replay=self.replay)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["seeds"], [0, 1, 2, 3, 4])
        self.assertEqual(report["counts"]["failed"], 10)
        self.assertTrue(all(entry["returncode"] == 7 for entry in report["runs"]))

    def test_http_mock_result_provenance_overrides_requested_real_kind(self):
        # Simulate completed LocalClient subprocess outputs. No --replay and no
        # HTTP server are needed to exercise the transport/provenance distinction.
        for kinds, expected in ((["mock", "mock"], "mock"),
                                (["real", "mock"], "mixed"),
                                (["real", "real"], "real")):
            with self.subTest(kinds=kinds):
                def launch(command, **kwargs):
                    pair_output = Path(command[command.index("--output") + 1])
                    task = json.loads(Path(command[command.index("--task") + 1]).read_text(encoding='utf-8'))
                    config = json.loads(Path(command[command.index("--config") + 1]).read_text(encoding='utf-8'))
                    source = {"kind": "http", "endpoint": config["endpoint"],
                              "endpoint_sha256": hashlib.sha256(config["endpoint"].encode()).hexdigest(),
                              "model": config["model"], "model_revision": config.get("model_revision")}
                    for mode, kind in zip(("baseline", "agent"), kinds):
                        result_path = pair_output / mode / "result.json"
                        result_path.parent.mkdir(parents=True)
                        result_path.write_text(json.dumps({
                            "schema_version": 1, "task_id": task["task_id"],
                            "task_digest": batch.digest(task), "config_digest": batch.digest(config),
                            "mode": mode, "evaluation_kind": kind, "paired_run_id": "http-pair",
                            "inference_source": source, "inference_source_digest": batch.digest(source),
                            "seed": 0, "wall_s": 0.1,
                            "validation": {"highest_stage": 3, "status": "passed"},
                        }), encoding="utf-8")
                    child = Mock(returncode=0)
                    child.wait.return_value = 0
                    return child

                output = self.output / expected
                with patch.object(batch, "managed_popen", side_effect=launch):
                    report = batch.run_batch(self.tasks[:1], output, seeds=[0])
                self.assertEqual(report["exit_code"], 0)
                self.assertEqual(report["requested_evaluation_kind"], "real")
                self.assertEqual(report["evaluation_kind"], expected)
                self.assertEqual(report["observed_evaluation_kinds"], sorted(set(kinds)))
                summary = json.loads((output / "summary.json").read_text(encoding='utf-8'))
                self.assertEqual(summary["input"]["excluded_counts"]["mock"], kinds.count("mock"))
                self.assertEqual(summary["input"]["eligible_runs"], kinds.count("real"))


    def test_plan_only_snapshots_without_launching_or_claiming_real_evaluation(self):
        with patch.object(batch, "managed_popen") as popen:
            report = batch.run_batch(self.tasks, self.output, seeds=[1, 2], plan_only=True)
        popen.assert_not_called()
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["evaluation_kind"], "unobserved")
        self.assertEqual(report["requested_evaluation_kind"], "real")
        self.assertEqual(report["planning"]["pairs"], 4)
        config = json.loads((self.output / report["config_snapshot"]).read_text(encoding='utf-8'))
        self.assertEqual(report["planning"]["model_calls_upper_bound"], 4 * (1 + config["max_attempts"]))
        self.assertEqual(report["planning"]["scheduled_pair_budget_s"], 4 * report["pair_timeout_s"])
        for task in report["tasks"]:
            self.assertEqual(batch.digest(json.loads((self.output / task["snapshot"]).read_text(encoding='utf-8'))),
                             task["task_digest"])
        summary = json.loads((self.output / "summary.json").read_text(encoding='utf-8'))
        self.assertEqual(summary["coverage"]["expected_results"], 8)
        self.assertEqual(summary["coverage"]["missing_results"], 8)
        self.assertFalse(summary["coverage"]["full_real_evaluation"])
        self.assertEqual(summary["input"]["eligible_runs"], 0)


    def test_mock_plan_rejects_a_result_claiming_real(self):
        report = batch.run_batch(self.tasks[:1], self.output, seeds=[0], replay=self.replay, plan_only=True)
        entry = report["runs"][0]
        path = self.output / entry["output"] / "agent/result.json"
        path.parent.mkdir(parents=True)
        value = {"track": report["track"], "task_id": entry["task_id"], "task_digest": entry["task_digest"],
                 "config_digest": report["config_digest"], "seed": 0, "mode": "agent", "evaluation_kind": "real"}
        path.write_text(json.dumps(value), encoding='utf-8')
        summary = batch._batch_summary(report, self.output)
        self.assertEqual(summary["coverage"]["rejected_slot_results"], 1)
        self.assertEqual(summary["input"]["eligible_runs"], 0)

    def test_coverage_rejects_changed_identity_and_reports_missing_partner(self):
        report = batch.run_batch(self.tasks[:1], self.output, seeds=[0], plan_only=True)
        entry = report["runs"][0]
        path = self.output / entry["output"] / "baseline/result.json"
        path.parent.mkdir(parents=True)
        record = {"schema_version": 1, "track": 'rtl', "task_id": entry["task_id"],
                  "task_digest": "changed-after-plan", "config_digest": report["config_digest"],
                  "seed": 0, "mode": "baseline", "evaluation_kind": "real",
                  "paired_run_id": "pair-1", "wall_s": 1,
                  "validation": {"status": "passed", "highest_stage": 3}}
        source = LocalClient(json.loads((self.output / report["config_snapshot"]).read_text(encoding='utf-8'))).provenance
        record.update(inference_source=source, inference_source_digest=batch.digest(source))
        path.write_text(json.dumps(record), encoding='utf-8')
        summary = batch._batch_summary(report, self.output)
        self.assertEqual(summary["coverage"]["expected_results"], 2)
        self.assertEqual(summary["coverage"]["present_results"], 1)
        self.assertEqual(summary["coverage"]["missing_results"], 1)
        self.assertEqual(summary["coverage"]["rejected_slot_results"], 1)
        self.assertEqual(summary["input"]["excluded_counts"]["invalid"], 1)
        self.assertEqual(summary["input"]["eligible_runs"], 0)
        self.assertEqual(report["evaluation_kind"], "unobserved")
        record["task_digest"] = entry["task_digest"]
        record["seed"] = False
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(batch._batch_summary(report, self.output)["coverage"]["rejected_slot_results"], 1)
        record["seed"] = 0
        path.write_text(json.dumps(record), encoding='utf-8')
        summary = batch._batch_summary(report, self.output)
        self.assertEqual(summary["coverage"]["eligible_result_fraction"], 0.5)
        self.assertEqual(summary["coverage"]["eligible_pairs"], 0)
        self.assertFalse(summary["coverage"]["full_real_evaluation"])
        partner = path.parent.parent / "agent/result.json"
        partner.parent.mkdir()
        partner.write_text(json.dumps(dict(record, mode="agent")), encoding='utf-8')
        summary = batch._batch_summary(report, self.output)
        self.assertEqual(summary["coverage"]["eligible_pairs"], 1)
        self.assertTrue(summary["coverage"]["full_real_evaluation"])
        partner.write_text(json.dumps(dict(record, mode="agent", paired_run_id="other")), encoding='utf-8')
        self.assertFalse(batch._batch_summary(report, self.output)["coverage"]["full_real_evaluation"])

    def test_plan_only_cli_and_wrong_track_manifest(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/batch.py"),
                                 "--tasks", str(self.tasks[0]), "--output", str(self.output),
                                 "--plan-only"], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "planned")
        self.assertEqual(list((self.output / "runs").iterdir()), [])
        manifest = self.root / "wrong_track.json"
        manifest.write_text(json.dumps({"track": 'hls', "tasks": ["task0.json"]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, "track must be"):
            batch.task_paths_from_manifest(manifest)



if __name__ == "__main__":
    unittest.main()
