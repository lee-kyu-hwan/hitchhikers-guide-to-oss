"""Several collections of one request adopted as one corpus (adopt-collection, repeated bundles).

One isolated collection entry covers one repository, so a request that names
several repositories arrives as several collections. The synthetic bundles
below are re-labelled collector runs (offline_fixture.derive_bundle).
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_oss_workflow as base  # noqa: E402  (installs the offline guards first)

import json  # noqa: E402
import os  # noqa: E402
import unittest  # noqa: E402

fx = base.fx
wf = base.wf


def edit_json(path, change):
    document = fx.read_json(path)
    change(document)
    fx.write_json(path, document)


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.request = fx.base_request(source__repositories=[fx.REPO, fx.REPO_B])

    def started(self, request=None):
        flow = base.Flow(self, request or self.request)
        flow.ok("init", "--request", str(flow.request_path))
        return flow

    def two(self, request=None):
        flow = self.started(request)
        _, _, first = flow.collect()
        second = fx.derive_bundle(first, flow.producer / "second", fx.REPO_B, "b")
        return flow, first, second

    def adopt(self, flow, bundles, *extra, code=0, inventories=True):
        argv = ["adopt-collection"]
        for paths in bundles:
            argv += ["--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"])]
            if inventories:
                argv += ["--inventory", str(paths["inventory"])]
        return flow.ok(*argv, *extra, code=code)

    def reject(self, flow, bundles, *needles, **kwargs):
        before = flow.state()
        result = self.adopt(flow, bundles, code=2, **kwargs)
        self.assertIn("scope-conflict", result[2])
        for needle in needles:
            self.assertIn(needle, result[2])
        after = flow.state()
        self.assertEqual((before["stages"]["collect"], before["ledger"]), (after["stages"]["collect"], after["ledger"]))
        return result


class AdoptTests(MergeCase):
    def test_two_collections_become_one_corpus(self):
        flow, first, second = self.two()
        result = self.adopt(flow, [first, second])
        self.assertEqual({"generation": 1, "reasons": [], "stage": "adopt-collection", "status": "complete"},
                         json.loads(result[1]))

        corpus = fx.read_json(flow.gen("collect", "corpus.json"))
        records = corpus["records"]
        self.assertEqual([fx.REPO, fx.REPO, fx.REPO_B, fx.REPO_B], [r["repository"]["full_name"] for r in records])
        self.assertEqual(["PR-001", "PR-002", "PR-003", "PR-004"], [r["pr_id"] for r in records])
        self.assertEqual(4, len({r["pull_request_node_id"] for r in records}))
        self.assertEqual(0, wf.run_main(fx.siblings.validate_corpus.main, [str(flow.gen("collect", "corpus.json"))])[0])

        generation = flow.state()["stages"]["collect"]["generations"][0]
        self.assertEqual(["corpus-01", "corpus-02", "inventory-01", "inventory-02", "manifest-01", "manifest-02"],
                         sorted(generation["inputs"]))
        for index, paths in enumerate((first, second), 1):
            for kind in ("corpus", "manifest", "inventory"):
                entry = generation["inputs"]["%s-%02d" % (kind, index)]
                self.assertEqual(fx.sha256_file(paths[kind]), entry["sha256"])
                self.assertEqual(os.path.abspath(paths[kind]), entry["source_path"])
                kept = flow.gen("collect", "bundles/%02d/%s" % (index, paths[kind].name))
                self.assertEqual(paths[kind].read_bytes(), kept.read_bytes())
        self.assertEqual("complete", generation["status"])

    def test_merged_manifest_keeps_every_run_and_adds_up_requests_and_budgets(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        merged = fx.read_json(flow.gen("collect", "manifest.json"))
        runs = [r["run_id"] for r in fx.read_json(first["manifest"])["records"]] \
            + [r["run_id"] for r in fx.read_json(second["manifest"])["records"]]
        self.assertEqual("2.0.0", merged["schema_version"])
        self.assertEqual(runs, [r["run_id"] for r in merged["records"]])
        info = merged["workflow_merge"]
        self.assertEqual(2, info["collections"])
        self.assertEqual(36, info["request_count_total"])
        self.assertEqual(400, info["request_budget_total"])
        self.assertEqual([fx.REPO, fx.REPO_B], [c["repositories"][0] for c in info["bundles"]])
        self.assertEqual(["PR-001", "PR-002", "PR-001", "PR-002"], [m["from"] for m in info["pr_id_map"]])
        self.assertEqual(["PR-001", "PR-002", "PR-003", "PR-004"], [m["to"] for m in info["pr_id_map"]])
        state = flow.state()
        self.assertEqual([18, 18], [e["requests"] for e in state["ledger"]])
        self.assertEqual(runs, [e["run_id"] for e in state["ledger"]])

    def test_order_is_kept(self):
        flow, first, second = self.two()
        self.adopt(flow, [second, first])
        corpus = fx.read_json(flow.gen("collect", "corpus.json"))
        self.assertEqual([fx.REPO_B, fx.REPO_B, fx.REPO, fx.REPO], [r["repository"]["full_name"] for r in corpus["records"]])
        generation = flow.state()["stages"]["collect"]["generations"][0]
        self.assertEqual(fx.sha256_file(second["corpus"]), generation["inputs"]["corpus-01"]["sha256"])

    def test_digest_runs_over_the_merged_corpus(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        flow.ok("digest")
        digest = flow.state()["stages"]["digest"]["generations"][0]
        self.assertEqual("complete", digest["status"])
        self.assertEqual(flow.state()["stages"]["collect"]["generations"][0]["outputs"]["corpus.json"],
                         digest["inputs"]["corpus"]["sha256"])

    def test_status_and_report_name_the_collections(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        collections = flow.status()["stages"]["collect"]["collections"]
        self.assertEqual(2, len(collections))
        self.assertEqual({fx.REPO: 2}, collections[0]["records_by_repository"])
        self.assertEqual({fx.REPO_B: 2}, collections[1]["records_by_repository"])
        self.assertEqual([18, 18], [c["request_count"] for c in collections])
        self.assertEqual([200, 200], [c["request_budget"] for c in collections])
        self.assertEqual("complete", collections[0]["status"])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("adopted from 2 collections", report)
        self.assertIn("4 records", report)
        for collection in collections:
            self.assertIn(collection["run_ids"][0], report)
        self.assertIn(fx.REPO_B, report)

    def test_the_same_bundles_are_reused(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        before = flow.state()
        self.adopt(flow, [first, second])
        after = flow.state()
        self.assertEqual(1, len(after["stages"]["collect"]["generations"]))
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual("reused", after["events"][-1]["result"])

    def test_partial_and_failed_collections_set_the_status(self):
        flow = self.started()
        run, _, first = flow.collect(failing_files=(102,))
        self.assertEqual(3, run.exit_code)
        second = fx.derive_bundle(first, flow.producer / "second", fx.REPO_B, "b")
        self.adopt(flow, [first, second], code=3)
        stage = flow.status()["stages"]["collect"]
        self.assertEqual("partial", stage["status"])
        self.assertTrue(stage["reasons"][0].startswith("collection 1"), stage["reasons"])
        flow.ok("digest")

        failing = self.started()
        run, _, first = failing.collect(search_status=422)
        self.assertEqual(4, run.exit_code)
        second = fx.derive_bundle(first, failing.producer / "second", fx.REPO_B, "b")
        self.adopt(failing, [first, second], code=4)
        self.assertEqual("failed", failing.status()["outcome"])
        self.assertEqual(2, failing.cli("digest")[0])


class RegressionTests(MergeCase):
    """One collection behaves exactly as before the repeated form existed."""

    def test_single_adoption_keeps_its_checkpoint_shape_and_bytes(self):
        flow = self.started(fx.base_request())
        _, _, paths = flow.collect()
        result = flow.adopt_collection(paths)
        self.assertEqual({"generation": 1, "reasons": [], "stage": "adopt-collection", "status": "complete"},
                         json.loads(result[1]))
        generation = flow.state()["stages"]["collect"]["generations"][0]
        self.assertEqual(["details", "generation", "inputs", "outputs", "producer", "reasons", "recorded_at",
                          "request_sha256", "status", "tool_revisions"], sorted(generation))
        self.assertEqual(["capped_repositories", "records", "repository_status", "validator"],
                         sorted(generation["details"]))
        self.assertEqual(["generated_by", "run_id"], sorted(generation["producer"]))
        self.assertEqual(["corpus", "inventory", "manifest"], sorted(generation["inputs"]))
        self.assertEqual(["corpus.json", "inventory.md", "manifest.json"], sorted(generation["outputs"]))
        for kind, name in (("corpus", "corpus.json"), ("manifest", "manifest.json"), ("inventory", "inventory.md")):
            self.assertEqual(fx.sha256_file(paths[kind]), generation["inputs"][kind]["sha256"])
            self.assertEqual(fx.sha256_file(paths[kind]), generation["outputs"][name])
        self.assertEqual(["active", "generations", "invalidated", "reasons", "status"], sorted(flow.status()["stages"]["collect"]))
        flow.ok("report")
        self.assertNotIn("adopted from", (flow.run_dir / "report.md").read_text(encoding="utf-8"))

    def test_one_collection_still_has_to_cover_every_repository(self):
        flow = self.started()
        _, _, first = flow.collect()
        result = flow.adopt_collection(first, code=2)
        self.assertIn("scope-conflict", result[2])
        self.assertIn("differ from source.repositories", result[2])


class RejectTests(MergeCase):
    def test_a_repository_no_collection_covers_is_refused(self):
        request = fx.base_request(source__repositories=[fx.REPO, fx.REPO_B, fx.REPO_C])
        flow, first, second = self.two(request)
        self.reject(flow, [first, second], "not covered", fx.REPO_C)

    def test_a_repository_in_two_collections_is_refused(self):
        flow, first, _ = self.two()
        again = fx.derive_bundle(first, flow.producer / "again", fx.REPO, "x")
        self.reject(flow, [first, again], "more than one collection", fx.REPO)

    def test_a_repository_outside_the_request_is_refused(self):
        flow, first, second = self.two()
        third = fx.derive_bundle(first, flow.producer / "third", fx.REPO_C, "c")
        self.reject(flow, [first, second, third], "outside source.repositories", fx.REPO_C)

    def test_the_same_pull_request_in_two_collections_is_refused(self):
        flow, first, second = self.two()
        original = fx.read_json(first["corpus"])["records"][0]

        def reuse_identity(corpus):
            corpus["records"][0]["pull_request_node_id"] = original["pull_request_node_id"]
            corpus["records"][0]["record_key"] = original["record_key"]

        edit_json(second["corpus"], reuse_identity)
        self.reject(flow, [first, second], "duplicate pull request", original["pull_request_node_id"])

    def test_a_record_from_another_collections_repository_is_refused(self):
        flow, first, second = self.two()
        edit_json(second["corpus"], lambda corpus: corpus["records"][0]["repository"].update(full_name=fx.REPO))
        self.reject(flow, [first, second], "out-of-scope repository")

    def test_a_run_id_shared_by_two_collections_is_refused(self):
        flow, first, second = self.two()
        shared = fx.read_json(first["manifest"])["records"][0]["run_id"]

        def share(manifest):
            manifest["records"][0]["run_id"] = shared

        edit_json(second["manifest"], share)
        self.reject(flow, [first, second], "run_id", "more than one collection")

    def test_differing_scope_is_refused(self):
        cases = {
            "interval": lambda r: r["interval"].update(start_at="2026-09-02T00:00:00Z"),
            "outcome": lambda r: r.update(outcome="merged"),
            "max_per_repository": lambda r: r.update(max_per_repository=3),
            "request_budget": lambda r: r.update(request_budget=100),
            "request_count": lambda r: r.update(request_count=201),
        }
        for needle, change in cases.items():
            with self.subTest(needle):
                flow, first, second = self.two()
                edit_json(second["manifest"], lambda m: change(m["records"][-1]))
                self.reject(flow, [first, second], needle)

    def test_collections_with_different_docs_filters_are_refused(self):
        flow, first, second = self.two()
        edit_json(first["manifest"], lambda m: m["records"][-1].update(docs_only="title"))
        edit_json(second["manifest"], lambda m: m["records"][-1].update(docs_only="path"))
        self.reject(flow, [first, second], "docs_only")

    def test_records_of_one_manifest_must_name_the_same_repositories(self):
        flow, first, second = self.two()

        def widen(manifest):
            extra = dict(manifest["records"][-1], run_id="run-" + "e" * 20)
            extra["repositories_requested"] = [fx.REPO_B, fx.REPO_C]
            manifest["records"].append(extra)

        edit_json(second["manifest"], widen)
        self.reject(flow, [first, second], "repositories_requested")

    def test_a_record_with_analysis_history_is_refused(self):
        flow, first, second = self.two()
        edit_json(second["corpus"], lambda corpus: corpus["records"][0].update(analysis_history=[{"note": "x"}]))
        self.reject(flow, [first, second], "analysis_history")

    def test_a_curated_tracker_takes_one_collection(self):
        flow = self.started(fx.curated_request())
        _, _, first = flow.curated_collect()
        before = flow.state()
        result = flow.ok("adopt-collection", "--corpus", str(first["corpus"]), "--manifest", str(first["manifest"]),
                         "--corpus", str(first["corpus"]), "--manifest", str(first["manifest"]), code=2)
        self.assertIn("one collection", result[2])
        self.assertEqual(before["stages"]["collect"], flow.state()["stages"]["collect"])

    def test_every_corpus_needs_its_manifest_and_inventories_are_all_or_none(self):
        flow, first, second = self.two()
        result = flow.ok("adopt-collection", "--corpus", str(first["corpus"]), "--corpus", str(second["corpus"]),
                         "--manifest", str(first["manifest"]), code=2)
        self.assertIn("pair every --corpus with one --manifest", result[2])
        result = flow.ok("adopt-collection", "--corpus", str(first["corpus"]), "--manifest", str(first["manifest"]),
                         "--corpus", str(second["corpus"]), "--manifest", str(second["manifest"]),
                         "--inventory", str(first["inventory"]), code=2)
        self.assertIn("--inventory", result[2])
        self.assertEqual([], flow.state()["stages"]["collect"]["generations"])


class ReplaceDriftAndBudgetTests(MergeCase):
    def test_a_different_bundle_set_needs_replace_and_invalidates_downstream(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        flow.ok("digest")
        ledger = flow.state()["ledger"]
        result = self.adopt(flow, [second, first], code=2)
        self.assertIn("pass --replace", result[2])
        self.assertEqual(1, len(flow.state()["stages"]["collect"]["generations"]))
        self.assertEqual("rejected", flow.state()["events"][-1]["result"])
        self.adopt(flow, [second, first], "--replace")
        state = flow.state()
        self.assertEqual(2, state["stages"]["collect"]["active"])
        self.assertIsNone(state["stages"]["digest"]["active"])
        self.assertIn("collect replaced by gen-002", state["stages"]["digest"]["generations"][0]["invalidated"])
        self.assertEqual(ledger, state["ledger"])  # the same two runs are counted once
        self.assertTrue((flow.run_dir / "stages" / "collect" / "gen-001" / "corpus.json").is_file())

    def test_a_failed_replace_keeps_the_active_checkpoint(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        edit_json(second["manifest"], lambda m: m["records"][-1].update(outcome="merged"))
        result = self.adopt(flow, [first, second], "--replace", code=2)
        self.assertIn("scope-conflict", result[2])
        self.assertEqual((1, 1), (flow.state()["stages"]["collect"]["active"], len(flow.state()["stages"]["collect"]["generations"])))
        flow.ok("digest")

    def test_a_modified_checkpoint_output_blocks_until_replaced(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        kept = flow.gen("collect", "bundles/02/corpus.json")
        kept.write_text(kept.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assertEqual("blocked", flow.status()["outcome"])
        self.assertEqual(2, flow.cli("digest")[0])
        self.adopt(flow, [first, second], "--replace")
        self.assertEqual("awaiting-input", flow.status()["outcome"])

    def test_revision_drift_blocks_until_the_stage_is_readopted(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        state = flow.state()
        wf.active(state, "collect")["tool_revisions"]["collector"] = "sha256:" + "0" * 64
        fx.write_json(flow.run_dir / "state.json", state)
        status = flow.status()
        self.assertEqual("blocked", status["outcome"])
        self.assertEqual({"collect": ["tool-revision-changed:collector"]}, status["integrity_failures"])
        self.assertEqual(2, flow.cli("digest")[0])
        self.assertIn("pass --replace", self.adopt(flow, [first, second], code=2)[2])
        self.adopt(flow, [first, second], "--replace")
        self.assertEqual({}, flow.status()["integrity_failures"])
        self.assertEqual(2, len(flow.state()["ledger"]))

    def test_the_budget_ledger_adds_up_both_collections(self):
        request = fx.base_request(source__repositories=[fx.REPO, fx.REPO_B], budget__total_api_requests=30)
        flow, first, second = self.two(request)
        self.adopt(flow, [first, second], code=4)
        status = flow.status()
        self.assertEqual("blocked", status["outcome"])
        self.assertEqual(36, status["budget"]["producer_reported_requests"])
        self.assertTrue(status["budget"]["violations"][0].startswith("api-budget-exceeded:36>30"))
        self.assertEqual(2, flow.cli("digest")[0])

    def test_time_budget_counts_each_run(self):
        request = fx.base_request(source__repositories=[fx.REPO, fx.REPO_B], budget__max_elapsed_seconds=1)
        flow, first, second = self.two(request)
        self.adopt(flow, [first, second], code=4)
        self.assertTrue(any(v.startswith("time-budget-exceeded:") for v in flow.status()["budget"]["violations"]))


if __name__ == "__main__":
    unittest.main()
