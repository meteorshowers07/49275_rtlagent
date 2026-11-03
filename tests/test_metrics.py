"""Tests for sample accounting and honest baseline/agent comparisons."""

from __future__ import annotations

import importlib.util
import json
import hashlib
import math
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("metrics_under_test", Path(__file__).resolve().parents[1] / "scripts/metrics.py")
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def record(**updates):
    value = {"schema_version": 1, "task_id": "counter", "task_digest": "task-content-1", "mode": "agent",
             "evaluation_kind": "real", "config_digest": "shared-config",
             "paired_run_id": "pair-1", "seed": 1, "wall_s": 10.0,
             "validation": {"highest_stage": 3, "status": "passed"}}
    source = {"kind": "http", "endpoint": "http://127.0.0.1:8000/v1",
              "endpoint_sha256": hashlib.sha256(b"http://127.0.0.1:8000/v1").hexdigest(), "model": "fixture"}
    value["inference_source"] = source
    value["inference_source_digest"] = metrics._digest(source)
    value.update(updates)
    return value


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, folder, value):
        target = self.root / folder / "result.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")
        return target


    def test_track_estimates_weight_tasks_equally_and_require_five_samples_each(self):
        self.write("small", record(task_id="small", seed=0, validation={"highest_stage": 0, "status": "failed"}))
        for seed in range(5):
            self.write(f"large-{seed}", record(task_id="large", seed=seed))
        summary = metrics.summarize([self.root])["track_summary"]["groups"][0]
        stage = summary["stages"][str(max(metrics.STAGES))]
        self.assertEqual(stage["pass_at_1"], 0.5)
        self.assertIsNone(stage["pass_at_5"])
        self.assertEqual(stage["tasks_with_pass_at_5"], 1)

    def test_plan_keeps_missing_unknown_and_invalid_slots_in_denominator(self):
        plan = {"requested_evaluation_kind": "real", "config_digest": "shared-config", "runs": []}
        for seed in range(5):
            plan["runs"].append({"task_id": record()["task_id"], "task_digest": "task-content-1",
                                 "seed": seed, "output": f"runs/{seed}"})
        self.write("runs/0/agent", record(seed=0))
        self.write("runs/1/agent", record(seed=1, validation={"highest_stage": 0, "status": "unavailable"}))
        self.write("runs/2/agent", record(seed=2, inference_source_digest="changed"))
        (self.root / "batch.json").write_text(json.dumps(plan), encoding="utf-8")
        report = metrics.summarize([self.root])
        planned = report["planned_performance"]
        self.assertEqual(planned["expected_results"], 10)
        self.assertEqual(planned["missing_results"], 7)
        self.assertEqual(planned["eligible_results"], 1)
        self.assertFalse(planned["complete"])
        agent = next(group for group in planned["groups"] if group["mode"] == "agent")
        stage = agent["stages"][str(max(metrics.STAGES))]
        self.assertAlmostEqual(stage["pass_at_1"], 0.2)
        self.assertEqual(stage["pass_at_5"], 1)
        self.assertEqual(report["groups"][0]["stages"][str(max(metrics.STAGES))]["pass_at_1"], 1)

    def test_real_label_cannot_override_missing_replay_or_changed_source(self):
        missing = record()
        del missing["inference_source"]
        replay = {"kind": "replay", "fixture_sha256": "0" * 64}
        self.write("missing", missing)
        self.write("replay", record(seed=2, inference_source=replay, inference_source_digest=metrics._digest(replay)))
        self.write("changed", record(seed=3, inference_source_digest="0" * 64))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 0)
        self.assertEqual(report["input"]["excluded_counts"]["invalid"], 3)

    def test_saved_mock_response_overrides_real_result_label(self):
        target = self.write("spoof", record())
        response = target.parent / "attempt_01/response.json"
        response.parent.mkdir()
        response.write_text(json.dumps({"mock": True}), encoding="utf-8")
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 0)
        self.assertEqual(report["input"]["excluded_counts"]["mock"], 1)

    def test_estimator_boundaries(self):
        self.assertIsNone(metrics.pass_at_k(4, 2, 5))
        self.assertIsNone(metrics.pass_at_k(0, 0, 1))
        self.assertEqual(metrics.pass_at_k(5, 0, 5), 0)
        self.assertEqual(metrics.pass_at_k(5, 5, 5), 1)
        self.assertAlmostEqual(metrics.pass_at_k(10, 2, 5), 1 - math.comb(8, 5) / math.comb(10, 5))
        for arguments in ((-1, 0, 1), (5, 6, 1), (5, 2, 0), (True, 1, 1)):
            with self.assertRaises(ValueError):
                metrics.pass_at_k(*arguments)

    def test_unknown_and_mock_excluded_without_false_zeroes(self):
        self.write("real", record())
        self.write("mock", record(evaluation_kind="mock"))
        self.write("unverified", record(validation={"highest_stage": 0, "status": "unverified"}))
        self.write("unavailable", record(validation={"highest_stage": 0, "status": "unavailable"}))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 1)
        for reason in ("mock", "unverified", "unavailable"):
            self.assertEqual(report["input"]["excluded_counts"][reason], 1)
        self.assertEqual(report["groups"][0]["stages"]["3"]["pass_at_1"], 1)

    def test_budget_starved_synthesis_counts_as_a_failure_not_an_unknown(self):
        # P6 in one assertion: a stage that never ran because the time budget was
        # exhausted must stay in the official pass-rate denominator. Recording it
        # as "unverified" would exclude it and silently inflate the pass rate.
        starved = {"highest_stage": 2, "status": "failed", "failure_cause": "budget_exhausted",
                   "budget_exhausted": True, "stages": []}
        self.write("starved", record(validation=starved))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 1)
        self.assertEqual(report["input"]["excluded_counts"]["unverified"], 0)
        self.assertEqual(report["overall"]["stages"]["3"]["reached_fraction"], 0)
        self.assertEqual(report["overall"]["stages"]["2"]["reached_fraction"], 1)
        self.assertEqual(report["groups"][0]["stages"]["3"]["pass_at_1"], 0)
        self.assertEqual(report["groups"][0]["stages"]["2"]["pass_at_1"], 1)

    def test_budget_labels_do_not_replace_the_recorded_measurement(self):
        starved = {"highest_stage": 2, "status": "failed", "failure_cause": "budget_exhausted",
                   "budget_exhausted": True, "stages": []}
        self.write("starved", record(validation=starved))
        report = metrics.summarize([self.root])
        # The label must not become an exclusion reason or a new bucket.
        self.assertNotIn("budget_exhausted", report["input"]["excluded_counts"])
        group = report["diagnostics"]["groups"][0]
        self.assertEqual(group["validation_status_counts"], {"failed": 1})

    def test_complete_runs_are_samples_and_reached_stages_count(self):
        for seed, stage in enumerate((0, 1, 2, 3, 3)):
            self.write(str(seed), record(seed=seed, paired_run_id=f"pair-{seed}", attempts=[{}, {}, {}],
                                        validation={"highest_stage": stage,
                                                    "status": "passed" if stage == 3 else "failed"}))
        group = metrics.summarize([self.root])["groups"][0]
        self.assertEqual(group["n"], 5)
        self.assertEqual(group["stages"]["1"]["c"], 4)
        self.assertEqual(group["stages"]["2"]["c"], 3)
        self.assertAlmostEqual(group["stages"]["3"]["pass_at_1"], 0.4)
        self.assertEqual(group["stages"]["3"]["pass_at_5"], 1)

    def test_config_fingerprints_never_pool_and_pairs_match_all_fields(self):
        self.write("base", record(mode="baseline", wall_s=4,
                                  validation={"highest_stage": 1, "status": "failed"}))
        self.write("agent", record(wall_s=10))
        self.write("wrong-config", record(mode="baseline", config_digest="different"))
        self.write("wrong-seed", record(mode="baseline", seed=2))
        self.write("wrong-pair", record(mode="baseline", seed=3, paired_run_id="other"))
        self.write("wrong-pair-agent", record(seed=3, paired_run_id="different-pair"))
        self.write("wrong-task", record(mode="baseline", task_id="uart"))
        report = metrics.summarize([self.root])
        self.assertEqual(len(report["groups"]), 4)
        pairing = report["paired_comparisons"]
        self.assertEqual(pairing["n_pairs"], 1)
        self.assertEqual(pairing["unpaired_runs"], 5)
        group = pairing["groups"][0]
        self.assertEqual(group["agent_minus_baseline_wall_s_mean"], 6)
        self.assertEqual(group["highest_stage_gain_mean"], 2)
        self.assertEqual(group["stages"]["1"]["gain_percentage_points"], 0)
        self.assertEqual(group["stages"]["3"]["gain_percentage_points"], 100)

    def test_standalone_runs_are_samples_but_never_auto_paired(self):
        self.write("agent", record(paired_run_id=""))
        baseline = record(mode="baseline")
        del baseline["paired_run_id"]
        self.write("baseline", baseline)
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 2)
        self.assertEqual(report["input"]["excluded_counts"]["invalid"], 0)
        self.assertEqual(len(report["groups"]), 2)
        self.assertTrue(all(group["n"] == 1 for group in report["groups"]))
        self.assertTrue(all(group["stages"]["3"]["pass_at_1"] == 1 for group in report["groups"]))
        self.assertEqual(report["paired_comparisons"]["n_pairs"], 0)
        self.assertEqual(report["paired_comparisons"]["unpaired_runs"], 2)
        self.assertEqual({item["reason"] for item in report["paired_comparisons"]["unpaired"]},
                         {"missing_paired_run_id"})

    def test_standalone_identity_deduplicates_missing_and_empty_pair_id(self):
        first = record(paired_run_id="")
        second = record()
        del second["paired_run_id"]
        self.write("first", first)
        self.write("copy", second)
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 0)
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_run_identity"], 2)

    def test_task_contents_cannot_pool_or_pair_despite_same_task_id(self):
        for index in range(5):
            self.write(str(index), record(task_digest=f"task-content-{index}"))
        self.write("baseline", record(mode="baseline", task_digest="another-content"))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["eligible_runs"], 6)
        self.assertEqual(len(report["groups"]), 6)
        self.assertTrue(all(group["n"] == 1 for group in report["groups"]))
        self.assertTrue(all(group["stages"]["3"]["pass_at_5"] is None for group in report["groups"]))
        self.assertEqual(report["paired_comparisons"]["n_pairs"], 0)
        self.assertIn("task_digest", report["paired_comparisons"]["matching_fields"])
        self.write("matching-baseline", record(mode="baseline", task_digest="task-content-0"))
        report = metrics.summarize([self.root])
        self.assertEqual(report["paired_comparisons"]["n_pairs"], 1)
        self.assertEqual(report["paired_comparisons"]["groups"][0]["task_digest"], "task-content-0")

    def test_repeated_seed_with_new_pair_ids_does_not_create_five_samples(self):
        for index in range(5):
            self.write(str(index), record(paired_run_id=f"different-pair-{index}", seed=7))
        self.write("unique", record(paired_run_id="unique", seed=8))
        self.write("baseline", record(mode="baseline", paired_run_id="different-pair-0", seed=7))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_sampling_seed"], 5)
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_run_identity"], 0)
        self.assertEqual(report["input"]["eligible_runs"], 2)
        agent = next(group for group in report["groups"] if group["mode"] == "agent")
        self.assertEqual(agent["n"], 1)
        self.assertIsNone(agent["stages"]["3"]["pass_at_5"])
        self.assertEqual(report["paired_comparisons"]["n_pairs"], 0)

    def test_duplicate_identity_has_reason_precedence_over_repeated_seed(self):
        self.write("first", record())
        self.write("copy", record())
        self.write("same-seed-new-pair", record(paired_run_id="other"))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_run_identity"], 2)
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_sampling_seed"], 1)
        self.assertEqual(report["input"]["eligible_runs"], 0)

    def test_overlapping_inputs_and_duplicate_records(self):
        first = self.write("first", record())
        report = metrics.summarize([self.root, first.parent, first])
        self.assertEqual(report["input"]["eligible_runs"], 1)
        self.write("copy", record())
        self.write("base", record(mode="baseline"))
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["excluded_counts"]["duplicate_run_identity"], 2)
        self.assertEqual(report["paired_comparisons"]["n_pairs"], 0)
        self.assertEqual(report["input"]["eligible_runs"], 1)

    def test_wall_clock_and_final_stage_timing_missing_is_not_zero(self):
        self.write("one", record(wall_s=10, validation={"highest_stage": 1, "status": "failed", "stages": [
            {"name": "compile", "status": "passed", "wall_s": 2},
            {"name": "simulate", "status": "failed", "wall_s": 3},
            {"name": "synthesize", "status": "unavailable", "wall_s": 0}]}))
        self.write("two", record(seed=2, paired_run_id="pair-2", wall_s=20))
        report = metrics.summarize([self.root])
        self.assertEqual(report["overall"]["wall_s"], {"mean": 15, "p50": 15, "p95": 19.5})
        stage = report["overall"]["stages"]["1"]
        self.assertEqual(stage["final_validation_wall_s_samples"], 1)
        self.assertEqual(stage["final_validation_wall_s_mean"], 2)
        self.assertIsNone(report["overall"]["stages"]["3"]["final_validation_wall_s_mean"])

    def test_malformed_and_nonfinite_data_excluded(self):
        missing_task_digest = record()
        del missing_task_digest["task_digest"]
        invalids = [record(wall_s=float("nan")), record(wall_s=-1), record(wall_s=10**400),
                    record(seed=True),
                    record(config_digest=""), record(schema_version=True),
                    missing_task_digest, record(task_digest=""), record(paired_run_id=None),
                    record(validation={"highest_stage": True, "status": "passed"}),
                    record(validation={"highest_stage": 9, "status": "passed"}), []]
        for index, invalid in enumerate(invalids):
            self.write(str(index), invalid)
        malformed = self.root / "bad" / "result.json"
        malformed.parent.mkdir()
        malformed.write_text("{oops", encoding="utf-8")
        report = metrics.summarize([self.root])
        self.assertEqual(report["input"]["excluded_counts"]["invalid"], len(invalids) + 1)
        self.assertEqual(report["groups"], [])
        self.assertIsNone(report["overall"]["wall_s"]["p95"])
        json.dumps(report, allow_nan=False)

    def test_diagnostics_include_exclusions_without_changing_performance_samples(self):
        self.write("passed", record(stop_reason="passed", attempts=[{}], selected_attempt=1))
        self.write("generation-error", record(seed=2, stop_reason="error", error="RuntimeError: offline",
                   attempts=[], validation={"status": "unverified", "highest_stage": 0}))
        self.write("environment", record(seed=3, stop_reason="unavailable", attempts=[{}], selected_attempt=1,
                   validation={"status": "unavailable", "highest_stage": 0}))
        self.write("failed", record(seed=4, stop_reason="attempt_budget", attempts=[{}, {}, {}],
                   selected_attempt=1, validation={"status": "failed", "highest_stage": 0}))
        for folder in ("duplicate-a", "duplicate-b"):
            self.write(folder, record(seed=5, stop_reason="repeated_candidate", attempts=[{}, {}],
                                     selected_attempt=1))
        self.write("mock-agent", record(evaluation_kind="mock", stop_reason="passed",
                                       attempts=[{}, {}], selected_attempt=2))
        self.write("mock-baseline", record(mode="baseline", evaluation_kind="mock", error="",
                                          stop_reason="single_generation", attempts=[{}], selected_attempt=1))
        report = metrics.summarize([self.root, self.root / "passed"])
        self.assertEqual(report["input"]["eligible_runs"], 2)
        self.assertEqual(report["overall"]["n"], 2)
        self.assertEqual(report["groups"][0]["stages"]["3"]["pass_at_1"], 0.5)
        diagnostics = report["diagnostics"]
        self.assertFalse(diagnostics["performance_metric"])
        self.assertIn("not pass rates", diagnostics["scope"])
        self.assertEqual(diagnostics["parsed_records"], 8)
        self.assertEqual(diagnostics["unreadable_result_files"], 0)
        groups = {(item["mode"], item["evaluation_kind"]): item for item in diagnostics["groups"]}
        self.assertEqual(set(groups), {("agent", "real"), ("agent", "mock"), ("baseline", "mock")})
        real = groups["agent", "real"]
        self.assertEqual(real["n_records"], 6)
        self.assertEqual(real["stop_reason_counts"], {"passed": 1, "error": 1, "unavailable": 1,
                                                    "attempt_budget": 1, "repeated_candidate": 2})
        self.assertEqual(real["validation_status_counts"], {"passed": 3, "failed": 1,
                                                          "unverified": 1, "unavailable": 1})
        self.assertEqual(real["records_with_error"], 1)
        self.assertEqual(real["attempt_count"], {"records_with_list": 6, "total": 9, "mean": 1.5,
                                               "distribution": {"0": 1, "1": 2, "2": 2, "3": 1},
                                               "invalid_entries": 0})
        self.assertEqual(real["selected_attempt_counts"], {"1": 5, "<missing>": 1})
        self.assertEqual(groups["agent", "mock"]["selected_attempt_counts"], {"2": 1})
        self.assertEqual(groups["baseline", "mock"]["records_with_error"], 0)

    def test_diagnostics_handle_malformed_missing_and_unreadable_records(self):
        self.write("bad-types", record(mode=[], evaluation_kind={}, stop_reason={}, validation=[],
                                       error={}, attempts={}, selected_attempt=True))
        self.write("bad-fields", record(seed=2, stop_reason=[], error=False,
                   validation={"status": []}, attempts=[None, {}, "bad"], selected_attempt=0))
        self.write("bad-fields-two", record(seed=3, error=7, attempts=None, selected_attempt=-1))
        self.write("legacy", record(seed=4))
        self.write("not-object", [])
        self.write("missing", {})
        unreadable = self.root / "unreadable" / "result.json"
        unreadable.parent.mkdir()
        unreadable.write_bytes(b"\xff\xfe")
        report = metrics.summarize([self.root])
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["parsed_records"], 6)
        self.assertEqual(diagnostics["unreadable_result_files"], 1)
        self.assertEqual(diagnostics["non_object_records"], 1)
        self.assertEqual(sum(item["n_records"] for item in diagnostics["groups"]), 5)
        groups = {(item["mode"], item["evaluation_kind"]): item for item in diagnostics["groups"]}
        invalid = groups["<invalid>", "<invalid>"]
        self.assertEqual(invalid["validation_status_counts"], {"<invalid>": 1})
        self.assertEqual(invalid["stop_reason_counts"], {"<invalid>": 1})
        self.assertEqual(invalid["selected_attempt_counts"], {"<invalid>": 1})
        self.assertEqual(invalid["attempt_count"]["distribution"], {"<invalid>": 1})
        self.assertEqual(invalid["records_with_invalid_error"], 1)
        real = groups["agent", "real"]
        self.assertEqual(real["records_with_error"], 0)
        self.assertEqual(real["records_with_invalid_error"], 2)
        self.assertEqual(real["attempt_count"]["distribution"], {"3": 1, "<invalid>": 1, "<missing>": 1})
        self.assertEqual(real["attempt_count"]["invalid_entries"], 2)
        self.assertEqual(real["selected_attempt_counts"], {"<invalid>": 2, "<missing>": 1})
        self.assertEqual(groups["<missing>", "<missing>"]["validation_status_counts"], {"<missing>": 1})
        json.dumps(report, allow_nan=False)

    def test_empty_diagnostics_have_no_invented_attempts_or_groups(self):
        report = metrics.summarize([self.root])
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["parsed_records"], 0)
        self.assertEqual(diagnostics["groups"], [])
        self.assertEqual(report["overall"]["n"], 0)

    def test_other_tracks_and_inconsistent_stage_claims_are_rejected(self):
        self.write("legacy-rtl", record())
        self.write("explicit-rtl", record(track="rtl", seed=2))
        invalids = [record(track="hls", validation={"status": "failed", "highest_stage": 2}),
                    record(track="hls", evaluation_kind="mock"), record(track=None), record(track=[]),
                    record(validation={"status": "passed", "highest_stage": 2}),
                    record(validation={"status": "failed", "highest_stage": 3})]
        for index, invalid in enumerate(invalids):
            self.write(str(index), invalid)
        report = metrics.summarize([self.root])
        self.assertEqual(report["track"], "rtl")
        self.assertEqual(report["input"]["eligible_runs"], 2)
        self.assertEqual(report["input"]["excluded_counts"]["invalid"], len(invalids))
        self.assertEqual(report["overall"]["stages"]["3"]["reached_fraction"], 1)
        self.assertEqual(report["diagnostics"]["parsed_records"], 8)

    def test_missing_input_is_error(self):
        with self.assertRaises(ValueError):
            metrics.summarize([self.root / "does-not-exist"])


if __name__ == "__main__":
    unittest.main()
