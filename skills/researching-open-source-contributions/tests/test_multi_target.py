"""Several target repositories in one run (adopt-discovery and adopt-locus-search repeated, record per repository).

The candidate-reads entry reads one repository per run, so a request whose
target names several repositories arrives as one discovery per repository, one
assessment and locus search per discovery, and one verifier record per
repository that the workflow merges. The synthetic discoveries below come from
the production verifier over the fixture's fake GitHub world.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_oss_workflow as base  # noqa: E402  (installs the offline guards first)

import json  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402

fx = base.fx
wf = base.wf
TARGETS = [fx.REPO, fx.REPO_B, fx.REPO_C]


def edit_json(path, change):
    document = fx.read_json(path)
    change(document)
    fx.write_json(path, document)


def multi_request(**changes):
    changes.setdefault("target__repositories", list(TARGETS))
    return fx.base_request(**changes)


class MultiFlow(base.Flow):
    def __init__(self, test, request=None):
        super().__init__(test, request or multi_request())
        self.verifier = fx.VerifierWorld(TARGETS)
        self.target = fx.read_json(self.request_path)["target"]

    def discover_repo(self, repository, budget=None):
        out = self.producer / ("verify-" + repository.split("/")[1])
        out.mkdir(exist_ok=True)
        code, _, stderr = self.verifier.main([
            "discover", "--analysis", str(self.gen("analysis", "analysis.json")), "--repo", repository,
            "--max-candidates-per-repo", str(self.target["max_candidates_per_repo"]),
            "--max-candidates-total", str(self.target["max_candidates_total"]),
            "--output", str(out / "discovery.json"), "--manifest", str(out / "manifest.json"),
            "--request-budget", str(budget or self.target["discover_request_budget"])])
        self.test.assertEqual(0, code, stderr)
        return out / "discovery.json", out / "manifest.json"

    def adopt_discoveries(self, pairs, *extra, code=0):
        argv = ["adopt-discovery"]
        for discovery, manifest in pairs:
            argv += ["--discovery", str(discovery), "--manifest", str(manifest)]
        return self.ok(*argv, *extra, code=code)

    def bundles(self):
        return wf.active(self.state(), "discovery")["details"]["discoveries"]

    def bundle_path(self, repository, name="discovery.json"):
        index = next(b["index"] for b in self.bundles() if b["repository"] == repository)
        return self.gen("discovery", "bundles/%02d/%s" % (index, name))

    def assessment_for(self, repository, *, include_capped=False, name=None, **kwargs):
        discovery_path = self.bundle_path(repository)
        discovery = fx.read_json(discovery_path)
        capped = {(c["repository"], c["pattern_id"]) for c in wf.active(self.state(), "discovery")["details"]["capped"]}
        corpus = fx.read_json(self.gen("collect", "corpus.json"))
        urls = sorted(r["pull_request"]["url"] for r in corpus["records"])
        records = [r for r in discovery["records"] if include_capped or (r["repository"], r["pattern_id"]) not in capped]
        path = self.producer / (name or "assessment-" + repository.split("/")[1] + ".json")
        fx.write_json(path, {"schema_version": "1.0.0", "discovery_sha256": fx.sha256_file(discovery_path),
                             "assessments": [fx.synthetic_assessment(r, urls, **kwargs) for r in records]})
        return path

    def locus_for(self, repository, assessment):
        out = self.producer / ("verify-" + repository.split("/")[1])
        manifest = out / "locus-manifest.json"
        shutil.copyfile(self.bundle_path(repository, "manifest.json"), manifest)
        code, _, stderr = self.verifier.main([
            "locus-search", "--discovery", str(self.bundle_path(repository)), "--assessment", str(assessment),
            "--output", str(out / "locus-search.json"), "--manifest", str(manifest),
            "--request-budget", str(self.target["locus_request_budget"])])
        self.test.assertEqual(0, code, stderr)
        return out / "locus-search.json", manifest

    def adopt_loci(self, pairs, *extra, code=0):
        argv = ["adopt-locus-search"]
        for locus, manifest in pairs:
            argv += ["--locus-search", str(locus), "--manifest", str(manifest)]
        return self.ok(*argv, *extra, code=code)

    def record(self, assessments, *extra, code=0):
        argv = ["record"]
        for assessment in assessments:
            argv += ["--assessment", str(assessment)]
        return self.ok(*argv, "--origin", "synthetic-fixture", *extra, code=code)

    def through_discovery(self, repositories=None, code=0):
        self.through_analysis()
        pairs = [self.discover_repo(r) for r in (repositories or TARGETS)]
        self.adopt_discoveries(pairs, code=code)
        return pairs

    def through_locus(self, code=0, **assessment):
        self.through_discovery(code=code)
        assessments = {r: self.assessment_for(r, **assessment) for r in TARGETS}
        self.adopt_loci([self.locus_for(r, assessments[r]) for r in TARGETS], code=code)
        return assessments

    def unchanged(self, before):
        after = self.state()
        self.test.assertEqual(before["stages"], after["stages"])
        self.test.assertEqual(before["ledger"], after["ledger"])
        self.test.assertEqual("rejected", after["events"][-1]["result"])


class MultiDiscoveryTests(unittest.TestCase):
    def test_s1_three_discoveries_are_adopted_as_one_merged_discovery(self):
        flow = MultiFlow(self)
        pairs = flow.through_discovery()
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual(("complete", []), (generation["status"], generation["reasons"]))
        merged = fx.read_json(flow.gen("discovery", "discovery.json"))
        sources = [fx.read_json(d) for d, _ in pairs]
        self.assertEqual(sum(len(d["records"]) for d in sources), len(merged["records"]))
        self.assertEqual(TARGETS, merged["inputs"]["repositories"])
        self.assertEqual([r["repository"] for r in merged["records"]], TARGETS)
        self.assertEqual(("complete", [], []), (merged["status"], merged["skipped_by_cap"], merged["failed_scopes"]))
        # The verifier's own discovery document check accepts the merged document.
        fx.vc._validate_discovery_document(merged)
        # Originals are kept byte for byte and bound under discovery-NN / manifest-NN.
        for index, (discovery, manifest) in enumerate(pairs, 1):
            self.assertEqual(discovery.read_bytes(), flow.gen("discovery", "bundles/%02d/discovery.json" % index).read_bytes())
            self.assertEqual(fx.sha256_file(discovery), generation["inputs"]["discovery-%02d" % index]["sha256"])
            self.assertEqual(fx.sha256_file(manifest), generation["inputs"]["manifest-%02d" % index]["sha256"])
        self.assertEqual(TARGETS, [b["repository"] for b in generation["details"]["discoveries"]])
        self.assertEqual(3, [e["command"] for e in flow.state()["ledger"]].count("discover"))
        # The same bytes again are a reuse, not a new generation.
        flow.adopt_discoveries(pairs)
        self.assertEqual(1, len(flow.state()["stages"]["discovery"]["generations"]))
        self.assertEqual("reused", flow.state()["events"][-1]["result"])

    def test_discoveries_merge_in_target_order_whatever_the_argument_order(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in reversed(TARGETS)]
        flow.adopt_discoveries(pairs)
        merged = fx.read_json(flow.gen("discovery", "discovery.json"))
        self.assertEqual(TARGETS, merged["inputs"]["repositories"])
        self.assertEqual(TARGETS, [r["repository"] for r in merged["records"]])

    def test_s2_a_missing_target_repository_is_rejected(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in TARGETS[:2]]
        before = flow.state()
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("target repositories without a discovery: " + fx.REPO_C, result[2])
        flow.unchanged(before)

    def test_repositories_outside_or_repeated_or_together_are_rejected(self):
        flow = MultiFlow(self, multi_request(target__repositories=TARGETS[:2]))
        flow.through_analysis()
        a, b = flow.discover_repo(fx.REPO), flow.discover_repo(fx.REPO_B)
        before = flow.state()
        self.assertIn("is in more than one discovery", flow.adopt_discoveries([a, b, a], code=2)[2])
        outside = flow.discover_repo(fx.REPO_C)
        self.assertIn("outside target.repositories: " + fx.REPO_C, flow.adopt_discoveries([a, b, outside], code=2)[2])
        out = flow.producer / "verify-both"
        out.mkdir()
        code, _, stderr = flow.verifier.main([
            "discover", "--analysis", str(flow.gen("analysis", "analysis.json")), "--repo", fx.REPO, "--repo", fx.REPO_B,
            "--output", str(out / "discovery.json"), "--manifest", str(out / "manifest.json"), "--request-budget", "60"])
        self.assertEqual(0, code, stderr)
        both = (out / "discovery.json", out / "manifest.json")
        self.assertIn("must name exactly one repository", flow.adopt_discoveries([both, b], code=2)[2])
        flow.unchanged(before)

    def test_s3_a_repository_budget_other_than_the_target_budget_is_rejected(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(fx.REPO, budget=61)] + [flow.discover_repo(r) for r in TARGETS[1:]]
        before = flow.state()
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("discovery 1: request_budget 61 differs from target.discover_request_budget 60", result[2])
        flow.unchanged(before)

    def test_per_run_budgets_over_the_total_budget_are_rejected(self):
        # Three per-run limits of 60 are 180; a total of 150 cannot hold them even though far fewer were used.
        flow = MultiFlow(self, multi_request(budget__total_api_requests=150))
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in TARGETS]
        before = flow.state()
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("per-run discover request budgets sum to 180, over budget.total_api_requests 150", result[2])
        flow.unchanged(before)

    def test_requests_used_over_the_total_budget_block_under_the_ledger_rule(self):
        probe = MultiFlow(self)
        probe.through_analysis()
        used = fx.read_json(probe.discover_repo(fx.REPO)[1])["runs"][-1]["budget"]["requests_consumed"]
        collected = probe.collector_requests
        # Limits of `used` each sum to 3*used <= the total, but collection plus discovery used more.
        self.assertGreater(collected, 0)
        self.assertLessEqual(collected, 3 * used)
        flow = MultiFlow(self, multi_request(target__discover_request_budget=used, budget__total_api_requests=3 * used))
        flow.through_analysis()
        flow.adopt_discoveries([flow.discover_repo(r) for r in TARGETS], code=4)
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual("blocked", generation["status"])
        self.assertIn("api-budget-exceeded:%d>%d" % (collected + 3 * used, 3 * used), generation["reasons"])

    def test_other_analysis_patterns_or_verifier_revision_are_rejected(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in TARGETS]
        before = flow.state()
        discovery, manifest = pairs[2]

        def rebind(field, value):
            def change(document):
                document["inputs"][field] = value
            edit_json(discovery, change)
            edit_json(manifest, lambda m: m["runs"][-1]["inputs"].__setitem__(field, value))

        original = (discovery.read_bytes(), manifest.read_bytes())
        rebind("analysis_sha256", "0" * 64)
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("discovery 3: inputs.analysis_sha256 is not the active analysis checkpoint", result[2])
        discovery.write_bytes(original[0]), manifest.write_bytes(original[1])
        rebind("pattern_ids", [])
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("discoveries differ in inputs other than the repository: pattern_ids", result[2])
        discovery.write_bytes(original[0]), manifest.write_bytes(original[1])
        edit_json(discovery, lambda d: d["generated_by"].__setitem__("revision", "sha256:" + "1" * 64))
        result = flow.adopt_discoveries(pairs, code=2)
        self.assertIn("discoveries differ in verifier revision", result[2])
        flow.unchanged(before)

    def test_s4_one_partial_discovery_makes_the_merged_discovery_and_the_run_partial(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in TARGETS]
        edit_json(pairs[1][0], lambda d: d.__setitem__("status", "partial"))
        flow.adopt_discoveries(pairs, code=3)
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual("partial", generation["status"])
        self.assertIn("discovery 2 (%s): partial" % fx.REPO_B, generation["reasons"])
        self.assertEqual("partial", fx.read_json(flow.gen("discovery", "discovery.json"))["status"])
        status = flow.status()
        self.assertEqual(("awaiting-input", "partial"), (status["outcome"], status["completeness"]))

    def test_one_failed_discovery_fails_the_merged_discovery(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        pairs = [flow.discover_repo(r) for r in TARGETS]
        edit_json(pairs[0][0], lambda d: d.__setitem__("status", "failed"))
        flow.adopt_discoveries(pairs, code=4)
        self.assertEqual("failed", wf.active(flow.state(), "discovery")["status"])
        self.assertEqual(("failed", "none"), (flow.status()["outcome"], flow.status()["completeness"]))

    def test_the_target_caps_are_applied_again_to_the_merged_discovery(self):
        # One pattern per repository is three combinations; a total cap of 2 keeps the first two in target order.
        flow = MultiFlow(self, multi_request(target__max_candidates_total=2))
        flow.through_discovery(code=3)
        generation = wf.active(flow.state(), "discovery")
        merged = fx.read_json(flow.gen("discovery", "discovery.json"))
        self.assertEqual("partial", generation["status"])
        self.assertEqual(TARGETS[:2], [r["repository"] for r in merged["records"]])
        self.assertEqual([{"repository": fx.REPO_C, "pattern_id": fx.PATTERN_ID, "reason": "total-cap"}], merged["skipped_by_cap"])
        self.assertEqual([{"discovery": 3, "repository": fx.REPO_C, "pattern_id": fx.PATTERN_ID, "section": "records"}],
                         generation["details"]["capped"])
        self.assertIn("merged caps moved 1 combinations to skipped_by_cap", generation["reasons"])
        fx.vc._validate_discovery_document(merged)

    def test_status_plans_one_discover_entry_run_per_target_repository(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        steps = flow.status()["next"]
        self.assertEqual([("discovery", r, False) for r in TARGETS], [(s["stage"], s["repository"], s["executed"]) for s in steps])
        for step, repository in zip(steps, TARGETS):
            self.assertEqual(["--repo", repository], [step["argv"][i] for i in (4, 5)])
            self.assertEqual(1, step["argv"].count("--repo"))
            self.assertIn("repeated once per repository", step["then"])


class MultiLocusAndRecordTests(unittest.TestCase):
    def test_locus_searches_and_records_per_repository_merge_into_one_candidate_set(self):
        flow = MultiFlow(self)
        flow.through_discovery()
        steps = flow.status()["next"]
        self.assertEqual(["assessment"] * 3 + ["locus-search"] * 3 + ["record"], [s["stage"] for s in steps])
        for step, repository in zip(steps[:3], TARGETS):
            self.assertIn(str(flow.bundle_path(repository).relative_to(flow.run_dir)), step["argv"])
        for step, repository in zip(steps[3:6], TARGETS):
            self.assertEqual(repository, step["repository"])
            self.assertIn(str(flow.bundle_path(repository).relative_to(flow.run_dir)), step["argv"])
        self.assertIn("once per repository", steps[6]["command"])
        assessments = {r: flow.assessment_for(r) for r in TARGETS}
        loci = [flow.locus_for(r, assessments[r]) for r in TARGETS]
        flow.adopt_loci(loci)
        locus = wf.active(flow.state(), "locus-search")
        self.assertEqual("complete", locus["status"])
        self.assertEqual([fx.sha256_file(assessments[r]) for r in TARGETS],
                         [item["assessment_sha256"] for item in locus["details"]["locus_searches"]])
        self.assertEqual(["record"], [s["stage"] for s in flow.status()["next"]])
        flow.record([assessments[r] for r in reversed(TARGETS)])
        record = wf.active(flow.state(), "record")
        self.assertEqual("complete", record["status"])
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        self.assertEqual(["CAN-001", "CAN-002", "CAN-003"], [c["candidate_id"] for c in candidates["records"]])
        self.assertEqual(TARGETS, [c["repository"] for c in candidates["records"]])
        self.assertEqual(["VER-CAN-00%d-1" % n for n in (1, 2, 3)],
                         [c["verification_history"][0]["verification_id"] for c in candidates["records"]])
        self.assertEqual([], fx.vc.validate_candidates_document(candidates))
        for index in range(1, 4):  # each repository's verifier record output is kept as it came
            self.assertEqual(["CAN-001"], [c["candidate_id"] for c in
                                          fx.read_json(flow.gen("record", "bundles/%02d/candidates.json" % index))["records"]])
        status = flow.status()
        self.assertEqual(("insufficient", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Candidate reads by repository", report)
        for repository in TARGETS:
            self.assertIn("`%s`" % repository, report)

    def test_locus_searches_must_cover_each_discovery_once_and_bind_to_it(self):
        flow = MultiFlow(self)
        flow.through_discovery()
        assessments = {r: flow.assessment_for(r) for r in TARGETS}
        loci = [flow.locus_for(r, assessments[r]) for r in TARGETS]
        before = flow.state()
        self.assertIn("no locus search for discovery 3 (%s)" % fx.REPO_C, flow.adopt_loci(loci[:2], code=2)[2])
        self.assertIn("locus search 3 is bound to discovery 1, already searched by locus search 1",
                      flow.adopt_loci([loci[0], loci[1], loci[0]], code=2)[2])
        edit_json(loci[2][0], lambda d: d.__setitem__("discovery_sha256", "0" * 64))
        self.assertIn("locus search 3 is not bound to an adopted discovery", flow.adopt_loci(loci, code=2)[2])
        self.assertIn("adopt one --locus-search per discovery",
                      flow.ok("adopt-locus-search", "--locus-search", str(loci[0][0]), "--manifest", str(loci[0][1]), code=2)[2])
        flow.unchanged(before)

    def test_locus_search_budget_other_than_the_target_budget_is_rejected(self):
        flow = MultiFlow(self)
        flow.through_discovery()
        assessments = {r: flow.assessment_for(r) for r in TARGETS}
        loci = [flow.locus_for(r, assessments[r]) for r in TARGETS]
        edit_json(loci[1][1], lambda m: m["runs"][-1]["budget"].__setitem__("request_limit", 41))
        before = flow.state()
        result = flow.adopt_loci(loci, code=2)
        self.assertIn("locus search 2: request budget 41 differs from target.locus_request_budget 40", result[2])
        flow.unchanged(before)

    def test_record_needs_each_repository_assessment_its_locus_search_used(self):
        flow = MultiFlow(self)
        assessments = flow.through_locus()
        before = flow.state()
        self.assertIn("no assessment for discovery 2 (%s)" % fx.REPO_B,
                      flow.record([assessments[fx.REPO], assessments[fx.REPO_C]], code=2)[2])
        other = flow.assessment_for(fx.REPO_B, name="other-assessment.json")
        edit_json(other, lambda d: d["assessments"][0].__setitem__("summary", fx.SYNTHETIC + ": another answer."))
        self.assertIn("assessment 2 is not the assessment that locus search 2 used",
                      flow.record([assessments[fx.REPO], other, assessments[fx.REPO_C]], code=2)[2])
        self.assertIn("adopt one --assessment per discovery",
                      flow.record([assessments[fx.REPO]], code=2)[2])
        flow.unchanged(before)

    def test_capped_combinations_stay_out_of_assessments_loci_and_candidates(self):
        flow = MultiFlow(self, multi_request(target__max_candidates_total=2))
        flow.through_discovery(code=3)
        steps = flow.status()["next"]
        self.assertIn("leave out the combinations the merged caps skipped: %s %s" % (fx.REPO_C, fx.PATTERN_ID), steps[2]["detail"])
        capped = flow.assessment_for(fx.REPO_C, include_capped=True, name="capped-assessment.json")
        locus, manifest = flow.locus_for(fx.REPO_C, capped)
        assessments = {r: flow.assessment_for(r) for r in TARGETS}
        loci = [flow.locus_for(r, assessments[r]) for r in TARGETS[:2]]
        before = flow.state()
        result = flow.adopt_loci(loci + [(locus, manifest)], code=2)
        self.assertIn("locus search 3 searches combinations the merged caps skipped: %s %s" % (fx.REPO_C, fx.PATTERN_ID), result[2])
        flow.unchanged(before)
        loci.append(flow.locus_for(fx.REPO_C, assessments[fx.REPO_C]))
        flow.adopt_loci(loci, code=0)
        flow.record([assessments[r] for r in TARGETS], code=3)
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        # The third repository's verifier record still holds a placeholder for the capped combination; the merge drops it.
        self.assertEqual(TARGETS[:2], [c["repository"] for c in candidates["records"]])
        self.assertEqual(1, len(fx.read_json(flow.gen("record", "bundles/03/candidates.json"))["records"]))
        self.assertEqual("partial", wf.active(flow.state(), "record")["status"])

    def test_a_reproduced_candidate_in_one_repository_is_a_candidates_outcome(self):
        flow = MultiFlow(self)
        flow.through_discovery()
        assessments = {r: flow.assessment_for(r, **({"status": "reproduced", "status_reason": "none",
                                                       "static_reproduction": True} if r == fx.REPO_B else {}))
                       for r in TARGETS}
        flow.adopt_loci([flow.locus_for(r, assessments[r]) for r in TARGETS])
        flow.record([assessments[r] for r in TARGETS])
        status = flow.status()
        self.assertEqual(("candidates", "complete"), (status["outcome"], status["completeness"]))
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        self.assertEqual(["unverified", "reproduced", "unverified"], [c["status"] for c in candidates["records"]])


class SingleTargetUnchangedTests(unittest.TestCase):
    def test_s6_one_discovery_keeps_the_single_document_path(self):
        flow = base.Flow(self)
        flow.full()
        state = flow.state()
        for stage in ("discovery", "locus-search", "record"):
            self.assertNotIn("bundles", {path.split("/")[0] for path in wf.active(state, stage)["outputs"]})
        self.assertEqual({"discovery", "manifest"}, set(wf.active(state, "discovery")["inputs"]))

    def test_one_discovery_for_a_multi_repository_target_stays_partial_not_refused(self):
        flow = MultiFlow(self)
        flow.through_analysis()
        flow.adopt_discoveries([flow.discover_repo(fx.REPO)], code=3)
        self.assertIn("target repositories not discovered: " + ", ".join(sorted(TARGETS[1:])),
                      wf.active(flow.state(), "discovery")["reasons"])


if __name__ == "__main__":
    unittest.main()
