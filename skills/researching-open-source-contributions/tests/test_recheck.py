"""The recheck stage: the verifier's recheck adopted after record (workflow contract, rule 10; hitchhikers-guide-to-oss#5 M6).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s <skill>/tests -p test_recheck.py -v
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import offline_fixture as fx  # noqa: E402  (installs the guards first)

import json  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402

from test_oss_workflow import Flow  # noqa: E402

wf = fx.workflow
MOVED_HEAD = "5" * 40


class RecheckFlow(Flow):
    def recheck(self, name="recheck", *, head=None, budget=None, candidates=(), org_repo=True):
        """Run the verifier's recheck on the active record, as the isolated process would.

        ``org_repo`` gives the owner's ``.github`` repository (no policy files) so the policy re-read is
        complete; without it the verifier warns ``policy-source-absent`` and the run is partial.
        """
        out = self.producer / name
        out.mkdir(exist_ok=True)
        reply, path = fx.verifier_tests.fixture_response, fx.vc._request_path
        if head is not None:
            self.verifier.replies[path("/repos/" + fx.REPO + "/commits/main", None)] = reply({"sha": head})
        if org_repo:
            org = fx.OWNER + "/.github"
            self.verifier.replies[path("/repos/" + org, None)] = reply(fx.repository_payload(org))
            for policy_path in fx.vc.POLICY_PATHS:
                self.verifier.replies[path("/repos/" + org + "/contents/" + policy_path, {"ref": "main"})] = reply(
                    {"message": "Not Found"}, status=404)
        manifest = out / "manifest.json"
        shutil.copyfile(self.gen("record", "manifest.json"), manifest)
        argv = ["recheck", "--candidates", str(self.gen("record", "candidates.json")),
                "--output", str(out / "candidates.json"), "--manifest", str(manifest),
                "--request-budget", str(budget or fx.base_request()["target"]["locus_request_budget"])]
        for candidate in candidates:
            argv += ["--candidate", candidate]
        code, stdout, stderr = self.verifier.main(argv)
        self.test.assertIn(code, (0, 3), stderr)
        self.test.assertEqual([], self.verifier.replies.unmatched)
        return out / "candidates.json", manifest

    def adopt_recheck(self, paths, *extra, code=0):
        return self.ok("adopt-recheck", "--candidates", str(paths[0]), "--manifest", str(paths[1]), *extra, code=code)

    def candidates(self, stage):
        return fx.read_json(self.gen(stage, "candidates.json"))["records"]


class RecheckStageTests(unittest.TestCase):
    def test_an_unchanged_head_keeps_every_status_and_binds_the_active_record(self):
        flow = RecheckFlow(self)
        flow.full()
        before = flow.status()
        self.assertEqual(("insufficient", "not-run", []), (before["outcome"], before["stages"]["recheck"]["status"], before["next"]))
        paths = flow.recheck()
        flow.adopt_recheck(paths)
        status = flow.status()
        stage = status["stages"]["recheck"]
        self.assertEqual(("complete", "gen-001", [], 1), (stage["status"], stage["active"], stage["reasons"], stage["generations"]))
        generation = wf.active(flow.state(), "recheck")
        record = wf.active(flow.state(), "record")
        self.assertEqual(record["outputs"]["candidates.json"], generation["inputs"]["record"]["sha256"])
        self.assertEqual(fx.sha256_file(paths[0]), generation["inputs"]["candidates"]["sha256"])
        self.assertEqual([("CAN-001", "unverified", "unverified", False)],
                         [(c["candidate_id"], c["before"], c["after"], c["next_recheck_required"]) for c in generation["details"]["rechecked"]])
        self.assertEqual(("insufficient", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        # The recheck run entered the ledger once; the discover and locus-search runs of the copied manifest did not re-enter.
        ledger = flow.state()["ledger"]
        self.assertEqual(1, len([e for e in ledger if e["command"] == "recheck"]))
        self.assertEqual(1, len([e for e in ledger if e["command"] == "locus-search"]))
        self.assertEqual("recheck", [e for e in ledger if e["command"] == "recheck"][0]["stage"])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Recheck", report)
        self.assertIn("| CAN-001 | unverified | unverified / insufficient-evidence |", report)
        self.assertIn("| recheck | complete | gen-001 |", report)
        # The same bytes again are a reuse; a different candidates file needs --replace.
        flow.adopt_recheck(paths)
        self.assertEqual("reused", flow.state()["events"][-1]["result"])
        self.assertEqual(1, len(flow.state()["stages"]["recheck"]["generations"]))

    def test_a_moved_head_makes_the_candidate_stale_and_the_outcome_no_candidates(self):
        flow = RecheckFlow(self)
        flow.full()
        paths = flow.recheck(head=MOVED_HEAD)
        flow.adopt_recheck(paths)
        status = flow.status()
        self.assertEqual(("no-candidates", "complete"), (status["outcome"], status["completeness"]))
        self.assertEqual(["CAN-001: unverified -> stale (base-moved)"], status["stages"]["recheck"]["reasons"])
        self.assertEqual([("stale", "base-moved")], [(c["status"], c["status_reason"]) for c in flow.candidates("recheck")])
        # The record generation is untouched: the recheck is a new generation of its own stage.
        self.assertEqual([("unverified", "insufficient-evidence")], [(c["status"], c["status_reason"]) for c in flow.candidates("record")])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### CAN-001 — stale / base-moved", report)
        self.assertIn("- Class: decisive non-actionable (synthetic fixture)", report)
        self.assertIn("after recheck gen-001", report)

    def test_an_actionable_candidate_plans_a_recheck_before_any_proposal(self):
        flow = RecheckFlow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment(status="reproduced", status_reason="none", static_reproduction=True)
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual("candidates", status["outcome"])
        step, = status["next"]
        self.assertEqual(("recheck", "external-network-step", False), (step["stage"], step["kind"], step["executed"]))
        self.assertEqual(["verify_candidates.py", "recheck", "--candidates", "stages/record/gen-001/candidates.json"], step["argv"][:4])
        self.assertIn("--request-budget", step["argv"])
        self.assertIn("1 actionable", step["detail"])
        self.assertIn("adopt-recheck", step["then"])
        flow.ok("report")
        self.assertIn("`recheck` (external-network-step, executed=False)", (flow.run_dir / "report.md").read_text(encoding="utf-8"))
        paths = flow.recheck()
        flow.adopt_recheck(paths)
        status = flow.status()
        self.assertEqual(("candidates", []), (status["outcome"], status["next"]))
        self.assertEqual([("reproduced", "none")], [(c["status"], c["status_reason"]) for c in flow.candidates("recheck")])

    def test_rechecked_candidates_that_do_not_continue_the_record_are_a_scope_conflict(self):
        flow = RecheckFlow(self)
        flow.full()
        paths = flow.recheck()
        before = (flow.run_dir / "state.json").read_bytes()
        forged = flow.producer / "forged.json"
        document = fx.read_json(paths[0])
        document["records"][0]["verification_history"][0]["observed_at"] = "2026-09-09T00:00:00Z"
        fx.write_json(forged, document)
        code, _, stderr = flow.cli("adopt-recheck", "--candidates", str(forged), "--manifest", str(paths[1]))
        self.assertEqual(2, code, stderr)
        self.assertIn("scope-conflict", stderr)
        self.assertIn("existing history changed", stderr)
        # A manifest without a recheck run for these candidates is refused too.
        code, _, stderr = flow.cli("adopt-recheck", "--candidates", str(paths[0]), "--manifest", str(flow.gen("record", "manifest.json")))
        self.assertEqual(2, code, stderr)
        self.assertIn("no recheck run", stderr)
        # A record of other candidates: the rechecked file must name exactly the record's candidates.
        fewer = flow.producer / "fewer.json"
        document = fx.read_json(paths[0])
        document["records"] = []
        fx.write_json(fewer, document)
        code, _, stderr = flow.cli("adopt-recheck", "--candidates", str(fewer), "--manifest", str(paths[1]))
        self.assertEqual(2, code, stderr)
        self.assertIn("scope-conflict", stderr)
        after = flow.state()
        self.assertEqual(None, after["stages"]["recheck"]["active"])
        self.assertEqual([], after["stages"]["recheck"]["generations"])
        self.assertFalse((flow.run_dir / "stages" / "recheck").exists() and any((flow.run_dir / "stages" / "recheck").iterdir()))
        self.assertEqual(3, len([e for e in after["events"] if e["result"] == "rejected"]))
        self.assertEqual(json.loads(before)["ledger"], after["ledger"])

    def test_a_recheck_budget_over_the_request_budget_is_refused(self):
        flow = RecheckFlow(self)
        flow.full()
        paths = flow.recheck(budget=fx.base_request()["budget"]["total_api_requests"] + 1)
        code, _, stderr = flow.cli("adopt-recheck", "--candidates", str(paths[0]), "--manifest", str(paths[1]))
        self.assertEqual(2, code, stderr)
        self.assertIn("scope-conflict", stderr)
        self.assertIn("budget.total_api_requests", stderr)

    def test_a_new_record_invalidates_the_recheck_and_a_null_target_skips_it(self):
        flow = RecheckFlow(self)
        flow.full()
        flow.adopt_recheck(flow.recheck())
        assessment = flow.assessment(status="not-applicable", status_reason="reproduction-failed")
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest), "--replace")
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual("not-run", status["stages"]["recheck"]["status"])
        self.assertEqual(["locus-search replaced by gen-002"], status["stages"]["recheck"]["invalidated"])
        self.assertEqual("no-candidates", status["outcome"])
        self.assertEqual("recheck", wf.CANDIDATE_STAGES[-1])
        null = RecheckFlow(self, request=fx.base_request(target=None))
        null.through_analysis()
        self.assertEqual("skipped", null.status()["stages"]["recheck"]["status"])
        code, _, stderr = null.cli("adopt-recheck", "--candidates", str(null.request_path), "--manifest", str(null.request_path))
        self.assertEqual(2, code)
        self.assertIn("target-unspecified", stderr)

    def test_a_verifier_warning_leaves_the_recheck_partial_with_the_warning_as_reason(self):
        flow = RecheckFlow(self)
        flow.full()
        paths = flow.recheck(org_repo=False)
        flow.adopt_recheck(paths, code=3)
        status = flow.status()
        self.assertEqual(("partial", "insufficient", "partial"), (status["stages"]["recheck"]["status"], status["outcome"], status["completeness"]))
        self.assertEqual(["recheck warnings: policy-source-absent:%s/.github" % fx.OWNER], status["stages"]["recheck"]["reasons"][:1])
        self.assertIn("recheck run partial (verifier status)", status["stages"]["recheck"]["reasons"][1])

    def test_contract_and_skill_describe_the_stage(self):
        contract = (fx.SKILL_DIR / "references" / "workflow-contract.md").read_text(encoding="utf-8")
        skill = (fx.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`adopt-recheck --candidates R --manifest M`", contract)
        self.assertIn("adopt-recheck", skill)
        self.assertIn("## Re-judgment after the locus search", contract)


if __name__ == "__main__":
    unittest.main()
