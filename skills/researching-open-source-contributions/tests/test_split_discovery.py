"""One target repository, several discover runs (#3): the static reads and the searches split over runs.

A REST discover (static reads + searches) of one repository can take more than the
candidate-reads budget of 60, so the workflow plans the split before the run (``status``)
and adopts the runs together (``adopt-discovery`` with several pairs of one repository),
merging them into one discovery. Everything here is offline: the production verifier
runs over the fixture's fake GitHub world, and no model is called.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_oss_workflow as base  # noqa: E402  (installs the offline guards first)

import copy  # noqa: E402
import json  # noqa: E402
import unittest  # noqa: E402

fx = base.fx
wf = base.wf
vt = fx.verifier_tests
PAT_A, PAT_B = fx.PATTERN_ID, "PAT-2"
CLUES_A = [fx.PATTERN_CLUE, "jittered backoff", "retry delay cap", "exponential retry", "sleep jitter"]
CLUES_B = ["docs options heading", "rule options table", "readme install"]
OTHER_HEAD = "5" * 40


def split_analysis(corpus):
    """The fixture analysis with a second pattern: 5 + 3 = 8 clues over two patterns."""
    analysis = fx.synthetic_analysis(corpus)
    first = analysis["patterns"][0]
    first["search_clues"] = list(CLUES_A)
    first["pattern_history"][-1]["conclusion"]["search_clues"] = list(CLUES_A)
    second = copy.deepcopy(first)
    second["pattern_id"] = PAT_B
    second["description"] = fx.SYNTHETIC + " pattern: rule documents carry an options heading."
    second["search_clues"] = list(CLUES_B)
    second["pattern_history"][-1]["conclusion"].update(pattern_id=PAT_B, description=second["description"],
                                                       search_clues=list(CLUES_B))
    analysis["patterns"].append(second)
    for record in analysis["records"]:
        record["analysis"]["pattern_ids"].append(PAT_B)
        record["analysis_history"][-1]["conclusion"]["pattern_ids"].append(PAT_B)
    return analysis


def rest_replies(repository, head, documents):
    """README.md, the docs/rules listing and ``documents`` rule files at ``head``, as the REST reads request them."""
    path = fx.vc._request_path
    replies = {path("/repos/%s/contents/README.md" % repository, {"ref": head}):
               vt.rest_file("README.md", vt.REST_README, repository=repository)}
    names = ["rule-%02d.md" % n for n in range(1, documents + 1)]
    replies[path("/repos/%s/contents/docs/rules" % repository, {"ref": head})] = vt.fixture_response(
        [vt.rest_entry("docs/rules", name, repository=repository) for name in names])
    for name in names:
        replies[path("/repos/%s/contents/docs/rules/%s" % (repository, name), {"ref": head})] = \
            vt.rest_file("docs/rules/" + name, vt.REST_SORT_THINGS.replace("sort-things", name[:-3]), repository=repository)
    return replies


def search_replies(repository, clues):
    path = fx.vc._request_path
    replies = {}
    for clue in clues:
        for kind in ("issue", "pr"):
            for state in ("open", "closed"):
                query = 'repo:%s "%s" is:%s is:%s' % (repository, clue, kind, state)
                replies[path("/search/issues", {"q": query})] = vt.fixture_response(
                    {"total_count": 0, "incomplete_results": False, "items": []})
        replies.setdefault(path("/search/code", {"q": 'repo:%s "%s"' % (repository, clue)}),
                           vt.fixture_response({"total_count": 0, "incomplete_results": False, "items": []}))
    return replies


class SplitWorld(fx.VerifierWorld):
    """The fixture world plus the REST contents and the eight clues' searches."""

    def __init__(self, documents=15, heads=(fx.HEAD_SHA,)):
        super().__init__()
        for key, reply in search_replies(fx.REPO, CLUES_A + CLUES_B).items():
            self.replies.setdefault(key, reply)  # the fixture's own code-search hit for PATTERN_CLUE stays
        for head in heads:
            self.replies.update(rest_replies(fx.REPO, head, documents))

    def move_head(self, head):
        self.replies[fx.vc._request_path("/repos/%s/commits/main" % fx.REPO, None)] = vt.fixture_response({"sha": head})


class SplitFlow(base.Flow):
    def __init__(self, test, request=None, documents=15, heads=(fx.HEAD_SHA,)):
        super().__init__(test, request)
        self.verifier = SplitWorld(documents, heads)
        self.target = fx.read_json(self.request_path)["target"]
        self.runs = 0
        (self.producer / "verify").mkdir()  # where the base flow's locus search writes

    def analysis_file(self, name="analysis.json", **kwargs):
        path = self.producer / name
        fx.write_json(path, split_analysis(fx.read_json(self.gen("collect", "corpus.json"))))
        return path

    def discover_run(self, *extra, budget=None, code=0):
        """One discover run of the split; ``extra`` narrows it (patterns, clue cap, static reads)."""
        self.runs += 1
        out = self.producer / ("run-%02d" % self.runs)
        out.mkdir()
        got, _, stderr = self.verifier.main([
            "discover", "--analysis", str(self.gen("analysis", "analysis.json")), "--repo", fx.REPO,
            "--max-candidates-per-repo", str(self.target["max_candidates_per_repo"]),
            "--max-candidates-total", str(self.target["max_candidates_total"]),
            "--output", str(out / "discovery.json"), "--manifest", str(out / "manifest.json"),
            "--request-budget", str(budget or self.target["discover_request_budget"]), *extra])
        self.test.assertEqual(code, got, stderr)
        return out / "discovery.json", out / "manifest.json"

    def static_run(self, *patterns, clue_cap=5, **kwargs):
        extra = [a for p in patterns for a in ("--pattern", p)] + ["--max-clues-per-pattern", str(clue_cap),
                                                                   "--static-reads", "rest"]
        return self.discover_run(*extra, **kwargs)

    def search_run(self, *patterns, clue_cap=5, **kwargs):
        extra = [a for p in patterns for a in ("--pattern", p)] + ["--max-clues-per-pattern", str(clue_cap)]
        return self.discover_run(*extra, **kwargs)

    def adopt(self, pairs, *extra, code=0):
        argv = ["adopt-discovery"]
        for discovery, manifest in pairs:
            argv += ["--discovery", str(discovery), "--manifest", str(manifest)]
        return self.ok(*argv, *extra, code=code)

    def discovery(self):
        return fx.read_json(self.gen("discovery", "discovery.json"))

    def unchanged(self, before):
        after = self.state()
        self.test.assertEqual(before["stages"], after["stages"])
        self.test.assertEqual(before["ledger"], after["ledger"])
        self.test.assertEqual("rejected", after["events"][-1]["result"])
        return after["events"][-1]["detail"]


class PlanTests(unittest.TestCase):
    def test_status_counts_the_run_before_it_and_proposes_the_split_when_the_budget_is_short(self):
        flow = SplitFlow(self)
        flow.through_analysis()
        steps = flow.status()["next"]
        # 26 repository requests + 5 x 8 clues = 66 against 60: one plain run per pattern.
        self.assertEqual([("discovery", "external-network-step", False, 1), ("discovery", "external-network-step", False, 2)],
                         [(s["stage"], s["kind"], s["executed"], s["run"]) for s in steps])
        self.assertEqual([[PAT_A], [PAT_B]], [[a for i, a in enumerate(s["argv"]) if s["argv"][i - 1] == "--pattern"] for s in steps])
        self.assertEqual([(51, 60, True), (41, 60, True)],
                         [(s["plan"]["requests"], s["plan"]["request_budget"], s["plan"]["fits"]) for s in steps])
        self.assertIn("once per run (2), together in one command", steps[0]["then"])
        self.assertTrue(all("--static-reads" not in s["argv"] for s in steps))
        report = (flow.run_dir / "report.md")
        flow.ok("report")
        self.assertIn("--pattern %s" % PAT_B, report.read_text(encoding="utf-8"))

    def test_a_run_that_fits_is_one_step_with_its_count(self):
        # The base fixture's one pattern with one clue: 26 + 5 = 31 fits one run, so one step without a run index.
        flow = base.Flow(self)
        flow.through_analysis()
        steps = flow.status()["next"]
        self.assertEqual(1, len(steps))
        self.assertEqual((True, 26 + 5, 0), (steps[0]["plan"]["fits"], steps[0]["plan"]["requests"]["total"],
                                             steps[0]["plan"]["shortfall"]))
        self.assertEqual("fits: one run finishes the static reads and every search", steps[0]["plan"]["lines"][-1])
        self.assertNotIn("run", steps[0])
        self.assertEqual("adopt-discovery --discovery ... --manifest ...", steps[0]["then"])


class SplitAdoptionTests(unittest.TestCase):
    def test_s2_a_static_run_and_a_search_run_are_adopted_as_one_complete_discovery_with_records(self):
        flow = SplitFlow(self)
        flow.through_analysis()
        static = flow.static_run(PAT_B)          # facts + README + listing + 15 documents + 3 clues x 5
        search = flow.search_run(PAT_A)          # facts + 5 clues x 5
        for path, _ in (static, search):
            self.assertEqual("complete", fx.read_json(path)["status"])
        flow.adopt([static, search])
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual(("complete", []), (generation["status"], generation["reasons"]))
        self.assertEqual((2, 0, 0), (generation["details"]["records"], generation["details"]["failed_scopes"],
                                     generation["details"]["skipped_by_cap"]))
        self.assertEqual([(1, [PAT_B], "rest", "complete"), (2, [PAT_A], "none", "complete")],
                         [(r["index"], r["pattern_ids"], r["static_reads"], r["status"]) for r in generation["details"]["split_runs"]])
        self.assertNotIn("discoveries", generation["details"])  # single-document steps follow, not per-bundle ones
        merged = flow.discovery()
        self.assertEqual(([fx.REPO], [PAT_A, PAT_B], "rest", ["docs/rules"], "complete"),
                         (merged["inputs"]["repositories"], merged["inputs"]["pattern_ids"], merged["inputs"]["static_reads"],
                          merged["inputs"]["static_dirs"], merged["status"]))
        records = {r["pattern_id"]: r for r in merged["records"]}
        self.assertEqual({PAT_A, PAT_B}, set(records))
        for record in records.values():
            self.assertTrue(record["duplicate_search"]["complete"])
            self.assertEqual([], record["duplicate_search"]["unused_clues"])
            self.assertTrue(record["code_search"]["available"])
            self.assertEqual(("rest-contents", fx.HEAD_SHA, True, True), (
                record["static_search"]["mode"], record["static_search"]["ref"], record["static_search"]["available"],
                record["static_search"]["complete"]))
            self.assertEqual(16, len(record["static_search"]["files"]))
            self.assertEqual(record["static_search"], merged["static_reads"][0]["static_search"])
        self.assertEqual(20, len(records[PAT_A]["duplicate_search"]["queries"]))
        self.assertEqual(12, len(records[PAT_B]["duplicate_search"]["queries"]))
        # The search run's record carries the static hits now: README installs without eslint.
        self.assertIn("readme-install-without-eslint", [h["rule"] for h in records[PAT_A]["static_search"]["hits"]])
        self.assertEqual("remote+static", records[PAT_A]["evidence_status"])  # the fixture code search hits PAT-1's clue
        self.assertEqual("static-only", records[PAT_B]["evidence_status"])
        self.assertEqual(("split-runs", [{"pattern_id": PAT_A, "discovery": 2, "searched_in": [2]},
                                         {"pattern_id": PAT_B, "discovery": 1, "searched_in": [1]}], []),
                         (merged["workflow_merge"]["kind"], merged["workflow_merge"]["chosen"], merged["workflow_merge"]["head_moved"]))
        # Both runs enter the ledger; the originals stay under bundles/.
        self.assertEqual(2, len([e for e in flow.state()["ledger"] if e["command"] == "discover"]))
        self.assertTrue(flow.gen("discovery", "bundles/02/discovery.json").exists())
        # The single-document steps follow on the merged discovery through to candidates.
        assessment, locus, record = flow.status()["next"]
        self.assertEqual(("assessment", "locus-search", "record"), (assessment["stage"], locus["stage"], record["stage"]))
        assessment_path = flow.assessment()
        locus_path, locus_manifest = flow.locus_search(assessment_path)
        flow.ok("adopt-locus-search", "--locus-search", str(locus_path), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment_path), "--origin", "synthetic-fixture")
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        self.assertEqual({PAT_A, PAT_B}, {c["pattern_ids"][0] for c in candidates["records"]})
        # The synthetic assessments stay unverified, so the run's outcome is insufficient; the point here is that
        # the split discovery carried the searches to complete and the record step made a candidate per pattern.
        self.assertEqual(("insufficient", "complete", 2), (flow.status()["outcome"], flow.status()["completeness"],
                                                           len(candidates["records"])))

    def test_a_pattern_the_static_run_carried_with_a_smaller_clue_cap_keeps_its_fuller_search(self):
        flow = SplitFlow(self)
        flow.through_analysis()
        reduced = flow.static_run(PAT_A, clue_cap=3)
        full = flow.search_run(PAT_A)
        other = flow.search_run(PAT_B)
        flow.adopt([reduced, full, other])
        merged = flow.discovery()
        self.assertEqual("complete", merged["status"])
        self.assertEqual([{"pattern_id": PAT_A, "discovery": 2, "searched_in": [1, 2]},
                          {"pattern_id": PAT_B, "discovery": 3, "searched_in": [3]}], merged["workflow_merge"]["chosen"])
        record = next(r for r in merged["records"] if r["pattern_id"] == PAT_A)
        self.assertEqual(([], 20, "rest-contents"), (record["duplicate_search"]["unused_clues"],
                                                    len(record["duplicate_search"]["queries"]), record["static_search"]["mode"]))
        # Adopted in another order the choice is the same.
        flow.adopt([other, full, reduced], "--replace")
        self.assertEqual([2, 1], [c["discovery"] for c in flow.discovery()["workflow_merge"]["chosen"]])

    def test_a_head_that_moved_between_the_runs_keeps_the_merge_partial(self):
        flow = SplitFlow(self, heads=(fx.HEAD_SHA, OTHER_HEAD))
        flow.through_analysis()
        search = flow.search_run(PAT_A)
        flow.verifier.move_head(OTHER_HEAD)
        static = flow.static_run(PAT_B)
        flow.adopt([static, search], code=3)
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual("partial", generation["status"])
        self.assertIn("head moved between the split runs for: " + PAT_A, generation["reasons"])
        merged = flow.discovery()
        moved = next(r for r in merged["records"] if r["pattern_id"] == PAT_A)
        self.assertEqual((fx.HEAD_SHA, OTHER_HEAD), (moved["head_sha"], moved["static_search"]["ref"]))
        self.assertIn(wf.HEAD_MOVED_BETWEEN_RUNS, moved["warnings"])
        self.assertEqual([PAT_A], merged["workflow_merge"]["head_moved"])

    def test_counterexamples_change_nothing(self):
        flow = SplitFlow(self)
        flow.through_analysis()
        static = flow.static_run(PAT_B)
        search = flow.search_run(PAT_A)
        before = flow.state()
        flow.adopt([static, flow.static_run(PAT_A, clue_cap=3)], code=2)
        self.assertIn("more than one split run carries the REST static reads", flow.unchanged(before))
        # A run of another repository is the several-repositories path, and that repository is not the target's.
        other_world = fx.VerifierWorld([fx.REPO_B])
        out = flow.producer / "other"
        out.mkdir()
        code, _, stderr = other_world.main([
            "discover", "--analysis", str(flow.gen("analysis", "analysis.json")), "--repo", fx.REPO_B, "--pattern", PAT_A,
            "--max-clues-per-pattern", "1", "--output", str(out / "discovery.json"), "--manifest", str(out / "manifest.json"),
            "--request-budget", "60"])
        self.assertEqual(0, code, stderr)
        flow.adopt([search, (out / "discovery.json", out / "manifest.json")], code=2)
        self.assertIn("covers a repository outside target.repositories", flow.unchanged(before))
        # The per-run budgets must still fit the request's total.
        tight = SplitFlow(self, fx.base_request(budget__total_api_requests=300))
        tight.through_analysis()
        tight.collector_requests = 0
        pairs = [tight.static_run(PAT_B), tight.search_run(PAT_A)]
        state = tight.state()
        # The collection already used part of the total; two runs of 60 on top pass 300 only if the ledger says so.
        used = sum(e["requests"] for e in state["ledger"])
        if used + 120 <= 300:
            tight.adopt(pairs)  # fits: adopted
        else:
            tight.adopt(pairs, code=2)
            self.assertIn("over budget.total_api_requests", tight.unchanged(state))
        # A split is adopted only for a target of exactly that repository.
        multi = SplitFlow(self, fx.base_request(target__repositories=[fx.REPO, fx.REPO_B]))
        multi.through_analysis()
        pairs = [multi.static_run(PAT_B), multi.search_run(PAT_A)]
        state = multi.state()
        multi.adopt(pairs, code=2)
        self.assertIn("adopted only for a target of exactly that repository", multi.unchanged(state))

    def test_a_budget_that_ended_in_the_searches_is_replanned_from_the_documents_it_read(self):
        # 25 rule documents and the floor budget 58: the reads finish (15 + 27 = 42 in the fixture world), the
        # first pattern's 25 search requests pass 58, so no record is made and the stage is no-candidates (#266).
        flow = SplitFlow(self, fx.base_request(target__discover_request_budget=58), documents=25)
        flow.through_analysis()
        discovery, manifest = flow.discover_run("--static-reads", "rest", code=3)
        document = fx.read_json(discovery)
        self.assertEqual(("partial", [], 2, 26), (document["status"], document["records"], len(document["failed_scopes"]),
                                                  len(document["static_reads"][0]["static_search"]["files"])))
        flow.adopt([(discovery, manifest)], code=3)
        self.assertEqual("no-candidates", wf.active(flow.state(), "discovery")["status"])
        steps = flow.status()["next"]
        self.assertTrue(steps, "no-candidates after budget-exhausted plans the split instead of stopping")
        self.assertIn("the plan below counts the 25 rule documents that run read", steps[0]["detail"])
        self.assertIn("budget-exhausted", steps[0]["detail"])
        # 26 + 1 + 1 + 25 = 53 leaves one clue beside the reads; the patterns are searched in full in plain runs.
        self.assertEqual([([PAT_A], 1, "rest", 58), ([PAT_A], 5, "none", 51), ([PAT_B], 5, "none", 41)],
                         [([a for i, a in enumerate(s["argv"]) if s["argv"][i - 1] == "--pattern"],
                           int(s["argv"][s["argv"].index("--max-clues-per-pattern") + 1]),
                           "rest" if "--static-reads" in s["argv"] else "none", s["plan"]["requests"]) for s in steps])
        self.assertTrue(all("--replace" in s["then"] for s in steps))
        # Done as planned, the split replaces the empty checkpoint with a complete one.
        runs = [flow.static_run(PAT_A, clue_cap=1, budget=58), flow.search_run(PAT_A, budget=58), flow.search_run(PAT_B, budget=58)]
        flow.adopt(runs, "--replace")
        generation = wf.active(flow.state(), "discovery")
        self.assertEqual(("complete", 2), (generation["status"], generation["details"]["records"]))
        self.assertEqual(("assessment", "locus-search", "record"), tuple(s["stage"] for s in flow.status()["next"]))


if __name__ == "__main__":
    unittest.main()
