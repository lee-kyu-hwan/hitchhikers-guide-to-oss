"""After the locus search, record re-judges duplicates mechanically; no model re-assessment stage exists
(workflow contract, Re-judgment after the locus search; hitchhikers-guide-to-oss#5 M2).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s <skill>/tests -p test_locus_rejudgment.py -v
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import offline_fixture as fx  # noqa: E402  (installs the guards first)

import unittest  # noqa: E402

from test_oss_workflow import Flow  # noqa: E402

wf = fx.workflow
PRIOR_PR = fx.REPO_URL + "/pull/77"


def prior_pull_request(state):
    return {"number": 77, "title": fx.SYNTHETIC + ": the same change, merged earlier", "state": state,
            "html_url": PRIOR_PR, "created_at": "2026-08-01T00:00:00Z", "closed_at": "2026-08-02T00:00:00Z",
            "pull_request": {"url": fx.REPO_URL + "/pulls/77"}, "state_reason": None}


class LocusRejudgmentTests(unittest.TestCase):
    def _flow_with_prior_pr(self, state="closed"):
        """The locus search returns one pull request for every locus clue; discover's own search returned nothing."""
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        for clue in fx.vc._locus_clues(fx.LOCUS):
            query = 'repo:%s "%s" is:pr is:%s' % (fx.REPO, clue, state)
            flow.verifier.replies[fx.vc._request_path("/search/issues", {"q": query})] = fx.verifier_tests.fixture_response(
                {"total_count": 1, "incomplete_results": False, "items": [prior_pull_request(state)]})
        return flow

    def test_a_pull_request_returned_for_every_locus_clue_turns_the_candidate_duplicate(self):
        flow = self._flow_with_prior_pr()
        # The assessment was made before the locus search and saw no duplicate.
        assessment = flow.assessment()
        self.assertEqual([], fx.read_json(assessment)["assessments"][0]["duplicate_verdict"]["matched_items"])
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        candidate, = fx.read_json(flow.gen("record", "candidates.json"))["records"]
        self.assertEqual(("duplicate", "duplicate-closed-other"), (candidate["status"], candidate["status_reason"]))
        self.assertEqual([PRIOR_PR], candidate["verification_history"][-1]["evidence"]["duplicate_verdict"]["matched_items"])
        self.assertTrue(any(gap.startswith("locus-duplicate: " + PRIOR_PR) for gap in candidate["blocking_gaps"]), candidate["blocking_gaps"])
        # The adopted assessment bytes are untouched: the re-judgment lives in the verifier's record output only.
        self.assertEqual(fx.sha256_file(assessment), wf.active(flow.state(), "record")["inputs"]["assessment"]["sha256"])
        status = flow.status()
        self.assertEqual(("no-candidates", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("### CAN-001 — duplicate / duplicate-closed-other", report)
        self.assertIn("- Class: decisive non-actionable (synthetic fixture)", report)
        self.assertIn("locus-duplicate: " + PRIOR_PR, report)

    def test_an_open_pull_request_is_duplicate_open_and_a_ready_claim_is_held_too(self):
        flow = self._flow_with_prior_pr(state="open")
        assessment = flow.assessment(status="reproduced", status_reason="none", static_reproduction=True)
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        candidate, = fx.read_json(flow.gen("record", "candidates.json"))["records"]
        self.assertEqual(("duplicate", "duplicate-open"), (candidate["status"], candidate["status_reason"]))
        self.assertEqual("no-candidates", flow.status()["outcome"])

    def test_the_contract_states_that_record_re_judges_without_a_model_call(self):
        contract = (fx.SKILL_DIR / "references" / "workflow-contract.md").read_text(encoding="utf-8")
        section = contract.split("## Re-judgment after the locus search", 1)[1].split("\n## ", 1)[0]
        for needle in ("`locus-duplicate`", "no model call", "`duplicate-closed-other`", "`duplicate-open`",
                       "locus-duplicate-undetermined", "never rewrites an assessment"):
            self.assertIn(needle, section)


if __name__ == "__main__":
    unittest.main()
