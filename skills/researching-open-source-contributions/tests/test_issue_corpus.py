"""An Issue corpus stays outside this workflow (SKILL.md; hitchhikers-guide-to-oss#5 M4, S4 fixture).

The Issue collector's corpus (`corpus_kind: github-issue`) is deliberately not a pull request corpus. This
test fixes the boundary with a corpus in that shape: every stage refuses it as `pending-unsupported`, and the
PR analyzer's own validator rejects it, so no stage can quietly read an Issue as a pull request. Connecting
Issues to the analysis is a separate issue (see the task report), not a silent widening here.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s <skill>/tests -p test_issue_corpus.py -v
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import offline_fixture as fx  # noqa: E402  (installs the guards first)

import unittest  # noqa: E402

from test_oss_workflow import Flow  # noqa: E402

wf = fx.workflow
ISSUES_FIXTURE = fx.SKILLS_ROOT / "collecting-open-source-issues" / "tests" / "fixtures" / "synthetic-issues.json"


def issue_corpus():
    """The Issue collector's corpus shape (references/collection-contract.md) built from its synthetic fixture."""
    source = fx.read_json(ISSUES_FIXTURE)
    records = []
    for index, issue in enumerate(source["issues"], 1):
        repository = source["repositories"][issue["repository"]]
        records.append({
            "identity_status": "resolved", "record_key": "github-issue:" + issue["node_id"],
            "issue_node_id": issue["node_id"], "issue_id": "ISS-%d" % index, "item_type": "issue",
            "input_path": "general-issue",
            "repository": {"full_name": repository["full_name"], "node_id": repository["node_id"], "repository_aliases": []},
            "issue": {"number": issue["number"], "title": issue["title"], "state": issue["state"],
                      "html_url": "https://github.com/%s/issues/%d" % (issue["repository"], issue["number"])},
            "closure": {"state_reason_raw": issue.get("state_reason"), "normalized_cause": "unknown"},
            "hydration_status": "complete",
        })
    return {"schema_version": "1.0.0", "corpus_kind": "github-issue",
            "generated_by": {"name": "collecting-open-source-issues", "revision": "sha256:" + "0" * 64}, "records": records}


class IssueCorpusBoundaryTests(unittest.TestCase):
    def test_an_issue_corpus_is_refused_by_every_stage_and_by_the_pr_validator(self):
        request = fx.base_request()
        request["source"] = {"kind": "issues", "repositories": ["synthetic-lab/widget"]}
        flow = Flow(self, request)
        flow.ok("init", "--request", str(flow.request_path))
        corpus, manifest = flow.producer / "issue-corpus.json", flow.producer / "issue-manifest.json"
        fx.write_json(corpus, issue_corpus())
        fx.write_json(manifest, {"schema_version": "1.0.0", "generated_by": issue_corpus()["generated_by"], "records": []})
        self.assertGreaterEqual(len(fx.read_json(corpus)["records"]), 2)
        for argv in (("adopt-collection", "--corpus", str(corpus), "--manifest", str(manifest)), ("digest",),
                     ("adopt-analysis", "--analysis", str(corpus), "--origin", "synthetic-fixture"),
                     ("adopt-recheck", "--candidates", str(corpus), "--manifest", str(manifest))):
            code, _, stderr = flow.cli(*argv)
            self.assertEqual(2, code, (argv, stderr))
            self.assertIn("pending-unsupported: source.kind issues is not connected", stderr)
            self.assertIn("not a pull request corpus", stderr)
        state = flow.state()
        self.assertEqual([None] * len(wf.STAGES), [state["stages"][s]["active"] for s in wf.STAGES])
        self.assertFalse((flow.run_dir / "stages").exists())
        status = flow.status()
        self.assertEqual(("pending-unsupported", "none"), (status["outcome"], status["completeness"]))
        self.assertEqual([("collect", "pending-unsupported")], [(s["stage"], s["kind"]) for s in status["next"]])
        # The PR analyzer's validator rejects the Issue corpus outright: no record has pull_request.url.
        code, stdout, stderr = wf.run_main(fx.validate_corpus.main, [str(corpus)])
        self.assertNotEqual(0, code, (stdout, stderr))

    def test_skill_and_contract_name_the_boundary(self):
        skill = (fx.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        contract = (fx.SKILL_DIR / "references" / "workflow-contract.md").read_text(encoding="utf-8")
        self.assertIn("General Issue sources (`issues`) are `pending-unsupported`", skill)
        self.assertIn("`source.kind` of `issues` is accepted as a request, but the run stays\n  `pending-unsupported`", contract)


if __name__ == "__main__":
    unittest.main()
