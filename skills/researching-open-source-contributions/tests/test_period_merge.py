"""Collections of other windows and outcomes of one request adopted as one corpus.

A period longer than one collector window (31 days) arrives as one collection per
window, per repository, per outcome. The synthetic windows are re-labelled
collector runs (offline_fixture.derive_window).
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_merge_collections as merge  # noqa: E402  (installs the offline guards first)

import hashlib  # noqa: E402
import json  # noqa: E402
import unittest  # noqa: E402

fx = merge.fx
wf = merge.wf

START, MID, END = "2026-09-01T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-08T00:00:00Z"
BOTH = ["merged", "closed-unmerged"]


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class PeriodCase(merge.MergeCase):
    def setUp(self):
        self.request = fx.base_request(source__outcome=list(BOTH))

    def windows(self, request=None, **layout):
        """One collector run to derive windows from, and the flow that adopts them."""
        flow = self.started(request or self.request)
        _, _, first = flow.collect()
        return flow, first

    def window(self, flow, base, name, **kwargs):
        return fx.derive_window(base, flow.producer / name, name, **kwargs)

    def rest(self, flow, base, outcome="merged"):
        """A second, valid collection, so the case under test is judged as a set (one collection is judged alone)."""
        return self.window(flow, base, "rest", interval=(MID, END), outcome=outcome, keep=[1], shift=700)

    def full_set(self, flow, base):
        return [
            self.window(flow, base, "m1", interval=(START, MID), outcome="merged", keep=[0]),
            self.window(flow, base, "m2", interval=(MID, END), outcome="merged", keep=[1]),
            self.window(flow, base, "c1", interval=(START, END), outcome="closed-unmerged", keep=[0], shift=500),
        ]


class AdoptPeriodTests(PeriodCase):
    def test_windows_and_outcomes_of_one_repository_become_one_corpus(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        result = self.adopt(flow, bundles)
        self.assertEqual("complete", json.loads(result[1])["status"])
        corpus = fx.read_json(flow.gen("collect", "corpus.json"))
        self.assertEqual(["PR-001", "PR-002", "PR-003"], [r["pr_id"] for r in corpus["records"]])
        self.assertEqual(["merged", "merged", "closed-unmerged"],
                         [r["pull_request"]["normalized_state"] for r in corpus["records"]])
        self.assertEqual(0, wf.run_main(fx.siblings.validate_corpus.main, [str(flow.gen("collect", "corpus.json"))])[0])
        merged = fx.read_json(flow.gen("collect", "manifest.json"))["workflow_merge"]
        self.assertEqual([(START, MID, "merged"), (MID, END, "merged"), (START, END, "closed-unmerged")],
                         [(c["interval"]["start_at"], c["interval"]["end_at"], c["outcome"]) for c in merged["bundles"]])
        self.assertEqual(54, merged["request_count_total"])
        self.assertEqual([18, 18, 18], [e["requests"] for e in flow.state()["ledger"]])
        flow.ok("digest")

    def test_the_last_window_may_be_shorter_and_windows_may_be_many(self):
        request = fx.base_request(source__outcome="merged")
        flow, base = self.windows(request)
        edges = [START, "2026-09-03T00:00:00Z", "2026-09-06T00:00:00Z", END]
        bundles = [self.window(flow, base, "w%d" % n, interval=(edges[n], edges[n + 1]), outcome="merged",
                               keep=[n % 2]) for n in range(3)]
        self.assertEqual("complete", json.loads(self.adopt(flow, bundles)[1])["status"])
        self.assertEqual(3, len(flow.status()["stages"]["collect"]["collections"]))

    def test_status_and_report_show_each_collections_window_and_outcome(self):
        flow, base = self.windows()
        self.adopt(flow, self.full_set(flow, base))
        collections = flow.status()["stages"]["collect"]["collections"]
        self.assertEqual(["merged", "merged", "closed-unmerged"], [c["outcome"] for c in collections])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("merged + closed-unmerged", report)
        self.assertIn("2026-09-04T00:00:00Z", report)
        self.assertIn("| Interval (UTC) | Outcome |", report)

    def test_several_repositories_each_need_their_own_cover(self):
        request = fx.base_request(source__repositories=[fx.REPO, fx.REPO_B], source__outcome="merged")
        flow, base = self.windows(request)
        bundles = [
            self.window(flow, base, "a1", interval=(START, MID), outcome="merged", keep=[0]),
            self.window(flow, base, "a2", interval=(MID, END), outcome="merged", keep=[1]),
            fx.derive_window(base, flow.producer / "b1", "b1", repository=fx.REPO_B, interval=(START, END), outcome="merged"),
        ]
        self.assertEqual("complete", json.loads(self.adopt(flow, bundles)[1])["status"])
        self.reject_gap = None
        flow2, base2 = self.windows(request)
        partial = [self.window(flow2, base2, "a1", interval=(START, MID), outcome="merged", keep=[0]),
                   fx.derive_window(base2, flow2.producer / "b1", "b1", repository=fx.REPO_B, interval=(START, END),
                                    outcome="merged")]
        self.reject(flow2, partial, "not covered", fx.REPO, "interval")


class RejectPeriodTests(PeriodCase):
    def test_a_gap_in_the_windows_is_refused(self):
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        bundles = [self.window(flow, base, "m1", interval=(START, "2026-09-03T00:00:00Z"), outcome="merged", keep=[0]),
                   self.window(flow, base, "m2", interval=(MID, END), outcome="merged", keep=[1])]
        self.reject(flow, bundles, "not covered", "interval", "2026-09-03T00:00:00Z", MID)

    def test_a_window_that_stops_before_the_end_or_starts_after_the_start_is_refused(self):
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        late = [self.window(flow, base, "m1", interval=("2026-09-02T00:00:00Z", MID), outcome="merged", keep=[0]),
                self.rest(flow, base)]
        self.reject(flow, late, "not covered", "interval", START)
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        early = [self.window(flow, base, "m1", interval=(START, "2026-09-07T00:00:00Z"), outcome="merged", keep=[0]),
                 self.window(flow, base, "m2", interval=("2026-09-07T00:00:00Z", "2026-09-07T12:00:00Z"),
                             outcome="merged", keep=[1])]
        self.reject(flow, early, "not covered", "interval", "2026-09-07T12:00:00Z")

    def test_overlapping_windows_of_one_repository_and_outcome_are_refused(self):
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        bundles = [self.window(flow, base, "m1", interval=(START, "2026-09-05T00:00:00Z"), outcome="merged", keep=[0]),
                   self.window(flow, base, "m2", interval=(MID, END), outcome="merged", keep=[1])]
        self.reject(flow, bundles, "more than one collection", "overlapping", fx.REPO)

    def test_a_window_outside_the_request_interval_is_refused(self):
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        bundles = [self.window(flow, base, "m1", interval=("2026-08-30T00:00:00Z", END), outcome="merged", keep=[0]),
                   self.rest(flow, base)]
        self.reject(flow, bundles, "outside source.interval")

    def test_an_outcome_nobody_covered_is_refused(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)[:2]
        self.reject(flow, bundles, "not covered", "closed-unmerged")

    def test_an_outcome_outside_the_request_is_refused(self):
        flow, base = self.windows(fx.base_request(source__outcome="merged"))
        bundles = [self.window(flow, base, "c1", interval=(START, END), outcome="closed-unmerged", keep=[0]),
                   self.window(flow, base, "m1", interval=(START, END), outcome="merged", keep=[1], shift=700)]
        self.reject(flow, bundles, "outcome", "closed-unmerged")
        flow, base = self.windows()
        both = [self.window(flow, base, "a", interval=(START, END), outcome="all", keep=[0]),
                self.window(flow, base, "m", interval=(START, END), outcome="merged", keep=[1], shift=700)]
        self.reject(flow, both, "outcome", "all")

    def test_a_record_whose_state_does_not_fit_its_collections_outcome_is_refused(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        merge.edit_json(bundles[2]["corpus"], lambda c: c["records"][0]["pull_request"].update(normalized_state="merged"))
        self.reject(flow, bundles, "does not fit", "closed-unmerged")

    def test_one_collection_of_the_whole_interval_still_cannot_stand_for_two_outcomes(self):
        flow, base = self.windows()
        result = flow.adopt_collection(base, code=2)
        self.assertIn("scope-conflict", result[2])

    def test_the_same_pull_request_in_two_windows_is_refused(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        original = fx.read_json(bundles[0]["corpus"])["records"][0]

        def reuse(corpus):
            corpus["records"][0].update(pull_request_node_id=original["pull_request_node_id"],
                                        record_key=original["record_key"])
        merge.edit_json(bundles[1]["corpus"], reuse)
        self.reject(flow, bundles, "duplicate pull request", original["pull_request_node_id"])

    def test_a_filter_or_run_or_collector_that_differs_is_refused_as_before(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        merge.edit_json(bundles[0]["manifest"], lambda m: m["records"][-1].update(docs_only="title"))
        self.reject(flow, bundles, "docs_only")
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        shared = fx.read_json(bundles[0]["manifest"])["records"][0]["run_id"]
        merge.edit_json(bundles[1]["manifest"], lambda m: m["records"][0].update(run_id=shared))
        self.reject(flow, bundles, "run_id")
        flow, base = self.windows()
        bundles = self.full_set(flow, base)
        merge.edit_json(bundles[1]["manifest"], lambda m: m["generated_by"].update(version="other"))
        self.reject(flow, bundles, "manifest generated_by")

    def test_records_of_one_manifest_must_agree_on_the_window_and_outcome(self):
        flow, base = self.windows()
        bundles = self.full_set(flow, base)

        def split(manifest):
            extra = json.loads(json.dumps(manifest["records"][-1]))
            extra["run_id"] = "run-" + "f" * 20
            extra["interval"]["end_at"] = "2026-09-03T00:00:00Z"
            manifest["records"].append(extra)
        merge.edit_json(bundles[0]["manifest"], split)
        self.reject(flow, bundles, "interval")


class RequestTests(PeriodCase):
    def init(self, outcome):
        flow = merge.base.Flow(self, fx.base_request(source__outcome=outcome))
        return flow.cli("init", "--request", str(flow.request_path))

    def test_the_outcome_list_form_is_exactly_merged_and_closed_unmerged(self):
        for ok in (["merged", "closed-unmerged"], ["closed-unmerged", "merged"], "all", "merged", "closed-unmerged"):
            with self.subTest(ok):
                self.assertEqual(0, self.init(ok)[0])
        for bad in ([], ["merged"], ["all", "merged"], ["merged", "merged"], ["merged", "closed-unmerged", "all"],
                    ["merged", 1], "open", {"merged": True}):
            with self.subTest(bad):
                result = self.init(bad)
                self.assertEqual(2, result[0])
                self.assertIn("source.outcome", result[2])

    def test_plan_data_names_the_outcome_choices_and_the_window_limit(self):
        flow = self.started()
        step = flow.status()["next"][0]
        self.assertIn("<merged|closed-unmerged>", step["argv"])
        self.assertIn("one run per repository, outcome and window", step["then"])


class RegressionTests(merge.MergeCase):
    """Collections of the same interval and outcome keep their stored bytes."""

    GOLDEN = {
        "corpus.json": "495b3c2785e9b3b2827c12f045b87cc877710e9ddd57f7c3f1924cea35ed8ede",
        # The collector stamps real run times into its manifest; those two keys are left out.
        "manifest": "fa8c0c4659c9645ec98feff09f7569073da63273936e6cd500e9737f7be1a4fb",
        "details": "794c1143237fae3bd75f7ab64cd84f115bbd4930684282b12692284c6f3d4272",
        "producer": "f49bd05d54d004fa4e59fa91c0914e0f79b30086b9a087ce42b57b276e0c3a04",
        "reasons": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    }

    def test_equal_interval_collections_are_stored_byte_for_byte_as_before(self):
        flow, first, second = self.two()
        self.adopt(flow, [first, second])
        generation = flow.state()["stages"]["collect"]["generations"][0]
        self.assertEqual(self.GOLDEN["corpus.json"], generation["outputs"]["corpus.json"])
        manifest = fx.read_json(flow.gen("collect", "manifest.json"))
        for record in manifest["records"]:
            record.pop("started_at"), record.pop("completed_at")
        self.assertEqual(self.GOLDEN["manifest"], sha(manifest))
        for key in ("details", "producer", "reasons"):
            self.assertEqual(self.GOLDEN[key], sha(generation[key]), key)
        self.assertEqual(["corpus-01", "corpus-02", "inventory-01", "inventory-02", "manifest-01", "manifest-02"],
                         sorted(generation["inputs"]))
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("| Interval (UTC) | Outcome |", report)
        self.assertNotIn("interval", flow.status()["stages"]["collect"]["collections"][0])

    def test_a_scalar_all_request_still_takes_all_collections(self):
        flow, first, second = self.two()
        self.assertEqual("all", flow.request["source"]["outcome"] if hasattr(flow, "request") else "all")
        self.assertEqual("complete", json.loads(self.adopt(flow, [first, second])[1])["status"])


if __name__ == "__main__":
    unittest.main()
