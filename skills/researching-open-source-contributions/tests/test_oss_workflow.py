"""Offline acceptance tests for the research workflow glue.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s <skill>/tests -v

The first import installs in-process network/process guards (see
offline_fixture). That guard is a test harness, not OS isolation.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import offline_fixture as fx  # noqa: E402  (installs the guards first)

import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
import urllib.request  # noqa: E402

wf = fx.workflow


class Flow:
    """One run directory plus a separate producer area standing in for the isolated process."""

    def __init__(self, test, request=None):
        self.test = test
        self.tmp = Path(tempfile.mkdtemp(prefix="oss-workflow-test-"))
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "run"
        self.producer = self.tmp / "producer"
        self.producer.mkdir()
        self.request_path = self.tmp / "request.json"
        fx.write_json(self.request_path, request or fx.base_request())
        self.verifier = fx.VerifierWorld()
        self.collector_requests = 0

    def cli(self, *argv):
        return wf.run_main(wf.main, list(argv[:1]) + ["--run-dir", str(self.run_dir), "--skills-root", str(fx.SKILLS_ROOT)]
                           + list(argv[1:]), environ={})

    def ok(self, *argv, code=0):
        result = self.cli(*argv)
        self.test.assertEqual(code, result[0], result)
        return result

    def state(self):
        return fx.read_json(self.run_dir / "state.json")

    def status(self):
        return json.loads(self.ok("status")[1])

    def gen(self, stage, name):
        state = self.state()
        return self.run_dir / "stages" / stage / ("gen-%03d" % state["stages"][stage]["active"]) / name

    # -- stages ---------------------------------------------------------------

    def collect(self, name="collect", **kwargs):
        run, opener, paths = fx.run_collector(self.producer / name, **kwargs)
        self.test.assertEqual([], opener.unmatched)
        self.collector_requests += len(opener.requests)
        return run, opener, paths

    def adopt_collection(self, paths, *extra, code=0):
        return self.ok("adopt-collection", "--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"]),
                       "--inventory", str(paths["inventory"]), *extra, code=code)

    def curated_collect(self, name="curated", **kwargs):
        run, opener, paths = fx.run_curated(self.producer / name, **kwargs)
        self.test.assertEqual([], opener.unmatched)
        self.collector_requests += len(opener.requests)
        return run, opener, paths

    def adopt_curated(self, paths, *extra, code=0):
        return self.ok("adopt-collection", "--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"]),
                       *extra, code=code)

    def analysis_file(self, name="analysis.json", **kwargs):
        path = self.producer / name
        fx.write_json(path, fx.synthetic_analysis(fx.read_json(self.gen("collect", "corpus.json")), **kwargs))
        return path

    def discover(self, *extra):
        out = self.producer / "verify"
        out.mkdir(exist_ok=True)
        code, stdout, stderr = self.verifier.main([
            "discover", "--analysis", str(self.gen("analysis", "analysis.json")), "--repo", fx.REPO,
            "--output", str(out / "discovery.json"), "--manifest", str(out / "manifest.json"),
            "--request-budget", str(fx.base_request()["target"]["discover_request_budget"]), *extra])
        self.test.assertEqual(0, code, stderr)
        return out / "discovery.json", out / "manifest.json"

    def assessment(self, **kwargs):
        discovery = fx.read_json(self.gen("discovery", "discovery.json"))
        corpus = fx.read_json(self.gen("collect", "corpus.json"))
        urls = sorted(r["pull_request"]["url"] for r in corpus["records"])
        path = self.producer / "assessment.json"
        fx.write_json(path, {"schema_version": "1.0.0", "discovery_sha256": fx.sha256_file(self.gen("discovery", "discovery.json")),
                             "assessments": [fx.synthetic_assessment(r, urls, **kwargs) for r in discovery["records"]]})
        return path

    def locus_search(self, assessment):
        out = self.producer / "verify"
        manifest = out / "locus-manifest.json"
        shutil.copyfile(self.gen("discovery", "manifest.json"), manifest)
        code, stdout, stderr = self.verifier.main([
            "locus-search", "--discovery", str(self.gen("discovery", "discovery.json")), "--assessment", str(assessment),
            "--output", str(out / "locus-search.json"), "--manifest", str(manifest),
            "--request-budget", str(fx.base_request()["target"]["locus_request_budget"])])
        self.test.assertEqual(0, code, stderr)
        return out / "locus-search.json", manifest

    def through_analysis(self):
        self.ok("init", "--request", str(self.request_path))
        _, _, paths = self.collect()
        self.adopt_collection(paths)
        self.ok("digest")
        self.ok("adopt-analysis", "--analysis", str(self.analysis_file()), "--origin", "synthetic-fixture")
        return paths

    def full(self):
        paths = self.through_analysis()
        discovery, manifest = self.discover()
        self.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = self.assessment()
        locus, locus_manifest = self.locus_search(assessment)
        self.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        self.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        self.ok("report")
        return paths


class GuardTests(unittest.TestCase):
    def test_guards_block_network_processes_and_tokens(self):
        for label in ("socket.create_connection", "Popen._execute_child", "OpenerDirector.open", "os.posix_spawn"):
            self.assertIn(label, fx.INSTALLED_GUARDS)
        for name in fx.TOKEN_ENVIRONMENT:
            self.assertNotIn(name, os.environ)
        with self.assertRaises(fx.OfflineViolation):
            socket.create_connection(("api.github.com", 443), timeout=1)
        with self.assertRaises(fx.OfflineViolation):
            subprocess.run(["gh", "--version"], check=False)
        with self.assertRaises(fx.OfflineViolation):
            urllib.request.urlopen("https://api.github.com/versions", timeout=1)
        default_client = fx.collector.GhApiClient(budget=fx.collector.RequestBudget(1), token_provider=lambda: None)
        with self.assertRaises(fx.OfflineViolation):
            default_client.get_json("/versions")

    def test_unrouted_requests_fail_instead_of_becoming_404(self):
        opener = fx.CollectorOpener({})
        client = fx.collector.GhApiClient(api_version=fx.API_VERSION, budget=fx.collector.RequestBudget(1),
                                          sleeper=fx.collector_tests.FakeSleep(), clock=lambda: fx.EPOCH,
                                          opener=opener, token_provider=lambda: None)
        with self.assertRaises(fx.MissingFixture):
            client.get_json("/repos/" + fx.OWNER + "/unrouted")
        world = fx.VerifierWorld()
        world.replies.clear()
        client = fx.vc.GhApiClient(sleeper=fx.collector_tests.FakeSleep(), clock=lambda: fx.EPOCH,
                                   budget=fx.vc.RequestBudget(1), opener=world.opener)
        with self.assertRaises(fx.MissingFixture):
            client.get_json("/repos/" + fx.OWNER + "/unrouted")

    def test_workflow_source_has_no_network_or_process_entry_point(self):
        source = (fx.SKILL_DIR / "scripts" / "oss_workflow.py").read_text(encoding="utf-8")
        for forbidden in ("import socket", "import subprocess", "import urllib", "http.client", "os.system", "Popen"):
            self.assertNotIn(forbidden, source)


class GateTextTests(unittest.TestCase):
    """The skill text must not claim more than the research launcher has shown."""

    def documents(self):
        paths = (fx.SKILL_DIR / "SKILL.md", fx.SKILL_DIR / "references" / "workflow-contract.md")
        return {path.name: " ".join(path.read_text(encoding="utf-8").split()) for path in paths}

    def test_documents_name_the_one_open_launch_and_what_stays_closed(self):
        required = (
            "plan data with `executed: false`", "not the isolated command", "inside the Linux VM",
            "the user's approval and the coordinator's instruction",
            "one measured combination of profile, model, and effort", "model launch exits 3",
            "no OS-level evidence that the boundaries hold under it",
            "a setsid-escaped descendant survives cleanup", "Keychain isolation is unverified",
            "fake success manifest from a probe is not a model result",
        )
        for name, text in self.documents().items():
            for sentence in required:
                self.assertIn(sentence, text, name)
        skill = self.documents()["SKILL.md"]
        self.assertIn("**Closed or unverified.** `auto`", skill)
        for closed in ("staged runs (deep-research, Agent, search)", "a model run with a relay", "the whole macOS path",
                       "No isolated entry has made the candidate reads (discovery, locus-search) against real data",
                       "the curated collector has no isolated entry",
                       "no path returns an assessment from an isolated model"):
            self.assertGreater(skill.index(closed), skill.index("**Closed or unverified.**"), closed)
        self.assertIn("General Issue sources (`issues`) are `pending-unsupported`. An isolated Issue collection entry "
                      "exists, but its output is not connected to this workflow", skill)

    def test_contract_gate_table_keeps_closed_paths_closed(self):
        contract = (fx.SKILL_DIR / "references" / "workflow-contract.md").read_text(encoding="utf-8")
        section = contract.split("## Isolation gate", 1)[1]
        rows = {cells[0]: cells[1] for cells in
                ([cell.strip() for cell in line.strip().strip("|").split("|")] for line in section.splitlines()
                 if line.startswith("| ")) if len(cells) == 2}
        expected = {
            "`auto`": "closed", "staged runs (deep-research, Agent, search)": "closed", "model run with a relay": "closed",
            "macOS, every launch": "closed", "candidate reads (discover, locus-search)": "no isolated entry",
            "curated tracker collection": "no isolated entry", "assessment from an isolated model": "no path",
        }
        for path, state in expected.items():
            self.assertTrue(rows[path].startswith(state), (path, rows[path]))
        opened = [path for path, state in rows.items() if state.startswith("open")]
        self.assertEqual(1, len(opened), opened)
        self.assertIn("one measured launch", opened[0])
        self.assertIn("no relay", opened[0])

    def test_documents_carry_no_stale_claim_or_short_lived_value(self):
        stale = ("exits 3 before any process starts", "exercised only with a non-AI probe",
                 "only been exercised with a non-AI probe", "new research is `blocked`", "#189", "#192")
        commit_hash = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
        for name, text in self.documents().items():
            for claim in stale:
                self.assertNotIn(claim, text, name)
            # The measured profile and model are named by the launcher, never here.
            self.assertNotIn("claude-", text, name)
            self.assertIsNone(commit_hash.search(text), name)
            self.assertNotIn("/Users/", text, name)
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", self.documents()["SKILL.md"]))

    def test_the_boundaries_the_skill_forbids_stay_forbidden(self):
        skill = self.documents()["SKILL.md"]
        for rule in ("All actual research runs only inside a verified, separately isolated research process",
                     "is not permission to call them from this host", "Make no GitHub writes",
                     "such a lookup is not part of the recorded budget and is not allowed",
                     "They open no network connection and start no process",
                     "neither evaluates nor bypasses the research launcher's gate"):
            self.assertIn(rule, skill)
        self.assertIn("Direct research from the host session, GitHub writes, and lookups outside the recorded budget "
                      "stay forbidden", self.documents()["workflow-contract.md"])


class EndToEndTests(unittest.TestCase):
    def test_fixture_flow_connects_production_collector_analyzer_and_verifier(self):
        flow = Flow(self)
        flow.full()
        state = flow.state()
        self.assertEqual({"collect": "complete", "digest": "complete", "analysis": "complete", "discovery": "complete",
                          "locus-search": "complete", "record": "complete"},
                         {stage: wf.active(state, stage)["status"] for stage in wf.STAGES})
        status = flow.status()
        # The synthetic candidate stays unverified, so the honest outcome is insufficient.
        self.assertEqual(("insufficient", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        # Budget: every fake-opener request counted once, although the verifier manifest was adopted twice.
        verifier_requests = len(flow.verifier.calls)
        self.assertEqual(flow.collector_requests + verifier_requests, status["budget"]["producer_reported_requests"])
        self.assertEqual(0, status["budget"]["external_requests_by_this_workflow"])
        self.assertTrue(status["budget"]["producer_requests_simulated"])
        self.assertEqual(["collect", "discover", "locus-search"], [e["command"] for e in state["ledger"]])
        # Checkpoints bind each stage to its upstream bytes.
        analysis = wf.active(state, "analysis")
        self.assertEqual(wf.active(state, "collect")["outputs"]["corpus.json"], analysis["inputs"]["corpus"]["sha256"])
        discovery = fx.read_json(flow.gen("discovery", "discovery.json"))
        self.assertEqual(analysis["outputs"]["analysis.json"], discovery["inputs"]["analysis_sha256"])
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        self.assertEqual(["unverified"], [c["status"] for c in candidates["records"]])
        self.assertEqual({fx.PATTERN_ID: fx.validate_corpus._canonical_revision()}, candidates["records"][0]["pattern_revisions"])
        self.assertTrue(flow.gen("digest", "extract/prompts").is_dir())
        # The verifier record step ran offline inside the workflow.
        self.assertEqual("record", fx.read_json(flow.gen("record", "manifest.json"))["runs"][-1]["command"])
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        for text in ("Outcome: `insufficient`", "## Synthetic fixture candidates (not contribution candidates)",
                     "Evidence base SHA `%s`" % fx.HEAD_SHA, "External requests sent by this workflow: 0",
                     "offline fixture responses, not real API calls", "## PR cases behind each pattern",
                     fx.pull_url(101), "Remaining judgment (unconfirmed readiness): ", "| Session model / effort | unknown / unknown |",
                     "origin `synthetic-fixture`", "do not prove that a model ran",
                     "No OS isolation, credential, or launcher gate is proven here",
                     "This workflow only adopts outputs that already exist and runs synthetic fixtures",
                     "New research belongs to the research launcher's Linux VM entries, with the user's approval",
                     "have no isolated path verified against real data yet",
                     "an isolated Issue collection entry exists, but the Issue corpus is not a pull request corpus"):
            self.assertIn(text, report)
        self.assertIn("- Class: undecided (synthetic fixture)", report)
        self.assertNotIn("Candidates (assessment origin claimed: ai)", report)

    def _through_locus_search(self, **assessment):
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        path = flow.assessment(**assessment)
        locus, locus_manifest = flow.locus_search(path)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        return flow, path

    def _report(self, flow):
        flow.ok("report")
        return (flow.run_dir / "report.md").read_text(encoding="utf-8")

    def test_reproduced_with_reproduction_evidence_is_a_candidates_outcome(self):
        flow, assessment = self._through_locus_search(status="reproduced", status_reason="none", static_reproduction=True)
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual(("candidates", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        candidates = fx.read_json(flow.gen("record", "candidates.json"))["records"]
        self.assertEqual([("reproduced", "static", "confirmed")],
                         [(c["status"], c["reproduction"]["method"], c["readiness_checks"]["reproduction_evidence"]["result"])
                          for c in candidates])
        self.assertTrue(candidates[0]["reproduction"]["evidence_links"])
        report = self._report(flow)
        # An actionable class in a fixture run is still not a contribution candidate.
        for text in ("Outcome: `candidates`", "## Synthetic fixture candidates (not contribution candidates)",
                     "### CAN-001 — reproduced / none", "- Class: actionable (synthetic fixture)"):
            self.assertIn(text, report)
        self.assertNotIn("Candidates (assessment origin claimed: ai)", report)

    def test_only_decisive_non_actionable_candidates_is_a_no_candidates_outcome(self):
        flow, assessment = self._through_locus_search(status="not-applicable", status_reason="reproduction-failed")
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual(("no-candidates", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        self.assertEqual("complete", wf.active(flow.state(), "record")["status"])
        self.assertEqual(["not-applicable"], [c["status"] for c in fx.read_json(flow.gen("record", "candidates.json"))["records"]])
        report = self._report(flow)
        for text in ("Outcome: `no-candidates`", "### CAN-001 — not-applicable / reproduction-failed",
                     "- Class: decisive non-actionable (synthetic fixture)"):
            self.assertIn(text, report)

    def test_reproduced_without_reproduction_evidence_is_refused_and_never_a_candidates_outcome(self):
        flow, assessment = self._through_locus_search(status="reproduced", status_reason="none")
        result = flow.cli("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        self.assertEqual(2, result[0], result)
        for label in ("reproduction.method", "reproduction.evidence_links", "readiness_checks.reproduction_evidence"):
            self.assertIn(label, result[2])
        state = flow.state()
        self.assertIsNone(state["stages"]["record"]["active"])
        record_dir = flow.run_dir / "stages" / "record"
        self.assertEqual([], sorted(p.name for p in record_dir.iterdir()) if record_dir.exists() else [])
        status = flow.status()
        self.assertEqual("awaiting-input", status["outcome"])
        self.assertEqual(("record", "rejected"), (status["last_rejection"]["command"], status["last_rejection"]["result"]))
        report = self._report(flow)
        self.assertIn("No candidate record yet.", report)
        self.assertNotIn("Outcome: `candidates`", report)
        self.assertNotIn("- Class: actionable", report)

    def test_status_before_collection_prints_plan_data_only(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        status = flow.status()
        self.assertEqual("awaiting-input", status["outcome"])
        step = status["next"][0]
        self.assertEqual(("collect", "external-network-step", False), (step["stage"], step["kind"], step["executed"]))
        self.assertIn("not evaluated", step["note"])
        for text in ("plan data; executed=false", "not the isolated command",
                     "only through the research launcher's Linux VM entries, with the user's approval"):
            self.assertIn(text, step["note"])
        # Recent-closed collection has an isolated entry that made real reads; the note claims no more than that.
        self.assertNotIn("No isolated entry has run this step", step["note"])
        self.assertIn("--request-budget", step["argv"])

    def test_digest_then_waiting_for_ai_analysis(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        flow.ok("digest")
        step = flow.status()["next"][0]
        self.assertEqual(("analysis", "awaiting-ai"), (step["stage"], step["kind"]))
        for text in ("never run in this session", "one measured read-only launch", "every other model launch is closed",
                     "Adopting an existing AI output offline is allowed"):
            self.assertIn(text, step["detail"])
        self.assertNotIn("gate is unverified", step["detail"])


class ResultKindTests(unittest.TestCase):
    def test_no_pattern_analysis_is_an_insufficient_result(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        flow.ok("adopt-analysis", "--analysis", str(flow.analysis_file(with_pattern=False)), "--origin", "synthetic-fixture", code=3)
        status = flow.status()
        self.assertEqual(("insufficient", "no-patterns", []), (status["outcome"], status["stages"]["analysis"]["status"], status["next"]))
        result = flow.cli("adopt-discovery", "--discovery", str(paths["corpus"]), "--manifest", str(paths["manifest"]))
        self.assertEqual(2, result[0])
        flow.ok("report")
        self.assertIn("No pattern: the analysis is an insufficient result", (flow.run_dir / "report.md").read_text(encoding="utf-8"))

    def test_partial_collection_is_usable_but_marked_partial(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        run, _, paths = flow.collect(failing_files=(102,))
        self.assertEqual(3, run.exit_code)
        flow.adopt_collection(paths, code=3)
        status = flow.status()
        self.assertEqual(("partial", "partial"), (status["stages"]["collect"]["status"], status["completeness"]))
        flow.ok("digest")

    def test_failed_collection_stops_the_flow(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        run, _, paths = flow.collect(search_status=422)
        self.assertEqual(4, run.exit_code)
        flow.adopt_collection(paths, code=4)
        status = flow.status()
        self.assertEqual("failed", status["outcome"])
        self.assertEqual("failed", status["next"][0]["kind"])
        self.assertEqual(2, flow.cli("digest")[0])
        flow.ok("report")
        self.assertIn("Outcome: `failed`", (flow.run_dir / "report.md").read_text(encoding="utf-8"))

    def test_missing_target_repository_is_partial_not_complete(self):
        request = fx.base_request(target__repositories=[fx.REPO, fx.OTHER_TARGET])
        flow = Flow(self, request)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest), code=3)
        stage = flow.status()["stages"]["discovery"]
        self.assertEqual("partial", stage["status"])
        self.assertIn("target repositories not discovered: " + fx.OTHER_TARGET, stage["reasons"])

    def test_budget_overrun_blocks(self):
        flow = Flow(self, fx.base_request(budget__total_api_requests=5))
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths, code=4)
        status = flow.status()
        self.assertEqual("blocked", status["outcome"])
        self.assertTrue(status["budget"]["violations"][0].startswith("api-budget-exceeded:"))
        self.assertEqual(2, flow.cli("digest")[0])

    def test_time_budget_overrun_blocks(self):
        flow = Flow(self, fx.base_request(budget__max_elapsed_seconds=1))
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths, code=4)
        self.assertTrue(any(v.startswith("time-budget-exceeded:") for v in flow.status()["budget"]["violations"]))

    def test_unsupported_sources_stay_pending(self):
        # The Issue corpus is not a pull request corpus: its skill forbids the analyzer handoff.
        request = fx.base_request()
        request["source"] = {"kind": "issues", "repositories": [fx.REPO]}
        flow = Flow(self, request)
        flow.ok("init", "--request", str(flow.request_path))
        status = flow.status()
        self.assertEqual("pending-unsupported", status["outcome"])
        self.assertEqual("pending-unsupported", status["next"][0]["kind"])
        _, _, paths = flow.collect()
        self.assertEqual(2, flow.adopt_collection(paths, code=2)[0])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("General Issue sources (`issues`) are not connected (pending-unsupported)", report)
        self.assertNotIn("curated-tracker sources are not connected", report)


class ReuseAndResumeTests(unittest.TestCase):
    def test_same_inputs_are_revalidated_and_reused(self):
        flow = Flow(self)
        paths = flow.through_analysis()
        before = flow.state()
        flow.adopt_collection(paths)
        flow.ok("digest")
        flow.ok("adopt-analysis", "--analysis", str(flow.producer / "analysis.json"), "--origin", "synthetic-fixture")
        after = flow.state()
        for stage in ("collect", "digest", "analysis"):
            self.assertEqual(1, len(after["stages"][stage]["generations"]))
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual(["reused"] * 3, [e["result"] for e in after["events"][-3:]])

    def test_resume_continues_from_the_last_valid_checkpoint(self):
        flow = Flow(self)
        flow.through_analysis()
        # A new process only has the run directory; status names the next step.
        step = flow.status()["next"][0]
        self.assertEqual(("discovery", "external-network-step", False), (step["stage"], step["kind"], step["executed"]))
        self.assertIn("No isolated entry has run this step against real data yet", step["note"])
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment, locus = flow.status()["next"]
        self.assertEqual(("assessment", "awaiting-ai"), (assessment["stage"], assessment["kind"]))
        self.assertIn("No isolated path returns an assessment yet", assessment["detail"])
        self.assertEqual(("locus-search", False), (locus["stage"], locus["executed"]))
        self.assertIn("No isolated entry has run this step against real data yet", locus["note"])

    def test_changed_input_needs_replace_and_invalidates_downstream(self):
        flow = Flow(self)
        flow.through_analysis()
        ledger = flow.state()["ledger"]
        _, _, second = flow.collect(name="collect-2", captured_at="2026-09-11T00:00:00Z")
        result = flow.adopt_collection(second, code=2)
        self.assertIn("pass --replace", result[2])
        state = flow.state()
        self.assertEqual((1, ledger), (len(state["stages"]["collect"]["generations"]), state["ledger"]))
        self.assertEqual("rejected", state["events"][-1]["result"])
        flow.adopt_collection(second, "--replace")
        state = flow.state()
        self.assertEqual(2, state["stages"]["collect"]["active"])
        self.assertTrue((flow.run_dir / "stages" / "collect" / "gen-001" / "corpus.json").is_file())
        self.assertIsNone(state["stages"]["analysis"]["active"])
        self.assertIn("collect replaced by gen-002", state["stages"]["analysis"]["generations"][0]["invalidated"])
        # The ledger is never reset: both collector runs stay counted.
        self.assertEqual(len(ledger) + 1, len(state["ledger"]))
        self.assertEqual(2, flow.cli("adopt-discovery", "--discovery", str(second["corpus"]), "--manifest", str(second["manifest"]))[0])

    def test_failed_replace_keeps_the_existing_checkpoint(self):
        flow = Flow(self)
        flow.through_analysis()
        before = flow.state()
        bad = flow.analysis_file("bad.json", quote_override="this sentence is absent from the corpus")
        self.assertEqual(2, flow.cli("adopt-analysis", "--analysis", str(bad), "--origin", "synthetic-fixture", "--replace")[0])
        after = flow.state()
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual(["gen-001"], sorted(p.name for p in (flow.run_dir / "stages" / "analysis").iterdir()))

    def test_modified_checkpoint_output_blocks_until_replaced(self):
        flow = Flow(self)
        paths = flow.through_analysis()
        corpus = flow.gen("collect", "corpus.json")
        corpus.write_text(corpus.read_text(encoding="utf-8") + " ", encoding="utf-8")
        status = flow.status()
        self.assertEqual("blocked", status["outcome"])
        self.assertEqual({"collect": ["corpus.json"]}, status["integrity_failures"])
        self.assertEqual(2, flow.cli("digest")[0])
        flow.adopt_collection(paths, "--replace")
        self.assertEqual("awaiting-input", flow.status()["outcome"])


class RejectionTests(unittest.TestCase):
    def test_new_scope_needs_a_new_run_directory(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        flow.ok("init", "--request", str(flow.request_path))
        fx.write_json(flow.request_path, fx.base_request(source__max_per_repository=3))
        self.assertIn("scope-conflict", flow.cli("init", "--request", str(flow.request_path))[2])
        (flow.run_dir / "request.json").write_text("{}", encoding="utf-8")
        self.assertIn("request.json changed", flow.cli("status")[2])

    def test_collection_outside_the_request_scope_is_rejected(self):
        flow = Flow(self, fx.base_request(source__interval={"start_at": "2026-08-01T00:00:00Z", "end_at": fx.INTERVAL[1]}))
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        result = flow.adopt_collection(paths, code=2)
        self.assertIn("scope-conflict", result[2])
        self.assertIsNone(flow.state()["stages"]["collect"]["active"])
        self.assertEqual([], flow.state()["ledger"])

    def test_discovery_with_other_budget_or_analysis_is_rejected(self):
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover("--max-candidates-total", "7")
        self.assertIn("caps differ", flow.cli("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))[2])
        tampered = flow.producer / "tampered-discovery.json"
        document = fx.read_json(discovery)
        document["inputs"]["analysis_sha256"] = "0" * 64
        fx.write_json(tampered, document)
        self.assertIn("not the active analysis", flow.cli("adopt-discovery", "--discovery", str(tampered), "--manifest", str(manifest))[2])

    def test_invalid_analysis_and_live_synthetic_origin_are_rejected(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        bad = flow.analysis_file("bad.json", quote_override="this sentence is absent from the corpus")
        result = flow.cli("adopt-analysis", "--analysis", str(bad), "--origin", "synthetic-fixture")
        self.assertEqual(2, result[0])
        self.assertIn("not verbatim", result[2])

        reuse = Flow(self, fx.base_request(mode="offline-reuse"))
        reuse.ok("init", "--request", str(reuse.request_path))
        reuse.adopt_collection(paths)
        good = reuse.analysis_file()
        self.assertIn("only in offline-fixture mode", reuse.cli("adopt-analysis", "--analysis", str(good), "--origin", "synthetic-fixture")[2])
        reuse.ok("adopt-analysis", "--analysis", str(good), "--origin", "ai", "--model", "caller-said-model")
        provenance = wf.active(reuse.state(), "analysis")["provenance"]
        self.assertEqual(("ai", "caller-said-model", "unknown", True),
                         (provenance["origin"], provenance["model"], provenance["effort"], provenance["claimed_by_caller"]))

    def test_ready_claim_without_evidence_is_rejected_by_the_verifier_gate(self):
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment()
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        ready = flow.assessment(status="pr-ready", status_reason="ready")
        result = flow.cli("record", "--assessment", str(ready), "--origin", "synthetic-fixture")
        self.assertEqual(2, result[0])
        self.assertIn("readiness_checks", result[2])
        self.assertIsNone(flow.state()["stages"]["record"]["active"])

    def test_request_contract(self):
        self.assertEqual([], wf.validate_request(fx.base_request()))
        cases = {
            "mode": fx.base_request(mode="live"),
            "static_only": fx.base_request(static_only=False),
            "allow_clone": fx.base_request(target__allow_clone=True),
            "provenance": fx.base_request(provenance={"model": "x"}),
            "interval": fx.base_request(source__interval={"start_at": fx.INTERVAL[1], "end_at": fx.INTERVAL[0]}),
        }
        for label, request in cases.items():
            self.assertTrue(wf.validate_request(request), label)


class NullTargetTests(unittest.TestCase):
    """`target: null` means collection and analysis only; nothing is verified against a borrowed target."""

    def _through_digest(self):
        flow = Flow(self, fx.base_request(target=None))
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        flow.ok("digest")
        return flow, paths

    def _assert_skipped(self, status):
        for stage in wf.CANDIDATE_STAGES:
            self.assertEqual(("skipped", [wf.TARGET_UNSPECIFIED]),
                             (status["stages"][stage]["status"], status["stages"][stage]["reasons"]), stage)
        self.assertEqual([(stage, "skipped", False) for stage in wf.CANDIDATE_STAGES],
                         [(s["stage"], s["kind"], s["executed"]) for s in status["next"]])

    def _report(self, flow):
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("| Target repositories | unspecified (candidate verification skipped) |", report)
        self.assertIn("| Candidate caps | not applicable (no target) |", report)
        self.assertNotIn("`%s`" % fx.REPO, report.split("| Target repositories |", 1)[1].split("\n", 1)[0])
        self.assertIn("Not verified: no target repository was specified", report)
        return report

    def test_null_target_contract(self):
        self.assertEqual([], wf.validate_request(fx.base_request(target=None)))
        cases = {
            "empty list": fx.base_request(target__repositories=[]),
            "zero discover budget": fx.base_request(target__discover_request_budget=0),
            "zero locus budget": fx.base_request(target__locus_request_budget=0),
            "not an object": fx.base_request(target=[]),
        }
        for label, request in cases.items():
            self.assertTrue(wf.validate_request(request), label)

    def test_target_read_budgets_stay_within_the_candidate_reads_entry_cap(self):
        # A discovery or locus search is read by the candidate-reads entry, whose relay budget is at most 60
        # (MAX_REQUEST_BUDGET); adopt-discovery requires the request's budget to equal the run's, so a larger
        # request budget could never be adopted (#82, the 2026-10-05 discover: 100 != 60). init refuses it.
        self.assertEqual(60, wf.CANDIDATE_READS_MAX_REQUEST_BUDGET)
        self.assertEqual([], wf.validate_request(fx.base_request(target__discover_request_budget=60,
                                                                 target__locus_request_budget=60)))
        for key in ("discover_request_budget", "locus_request_budget"):
            with self.subTest(key):
                errors = wf.validate_request(fx.base_request(**{"target__" + key: 61}))
                self.assertTrue(any(("target." + key) in error and "60" in error and "candidate-reads" in error
                                    for error in errors), errors)

    def test_null_target_without_patterns_is_insufficient_and_skips_candidates(self):
        flow, paths = self._through_digest()
        flow.ok("adopt-analysis", "--analysis", str(flow.analysis_file(with_pattern=False)), "--origin", "synthetic-fixture", code=3)
        status = flow.status()
        self.assertEqual(("insufficient", "complete"), (status["outcome"], status["completeness"]))
        self._assert_skipped(status)
        result = flow.cli("adopt-discovery", "--discovery", str(paths["corpus"]), "--manifest", str(paths["manifest"]))
        self.assertEqual(2, result[0])
        self.assertIn("target-unspecified", result[2])
        report = self._report(flow)
        self.assertIn("Outcome: `insufficient`", report)
        self.assertIn("| discovery | skipped | - | - | no target repository was specified", report)

    def test_null_target_with_patterns_is_analysis_only_never_a_candidate_verdict(self):
        flow, _ = self._through_digest()
        flow.ok("adopt-analysis", "--analysis", str(flow.analysis_file()), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual("analysis-only", status["outcome"])
        self.assertNotIn(status["outcome"], ("candidates", "no-candidates", "awaiting-input"))
        self._assert_skipped(status)
        # A real verifier output for some repository still cannot enter a run that named no target.
        discovery, manifest = flow.discover()
        for argv in (("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest)),
                     ("adopt-locus-search", "--locus-search", str(discovery), "--manifest", str(manifest)),
                     ("record", "--assessment", str(discovery), "--origin", "synthetic-fixture")):
            result = flow.cli(*argv)
            self.assertEqual(2, result[0], argv)
            self.assertIn("target-unspecified", result[2], argv)
        state = flow.state()
        self.assertEqual([None, None, None], [state["stages"][s]["active"] for s in wf.CANDIDATE_STAGES])
        report = self._report(flow)
        self.assertIn("Outcome: `analysis-only`", report)
        self.assertNotIn("## Synthetic fixture candidates", report)
        self.assertNotIn("## Candidates (assessment origin claimed: ai)", report)

    def test_object_target_keeps_candidate_verification(self):
        flow = Flow(self)
        flow.through_analysis()
        status = flow.status()
        self.assertEqual("awaiting-input", status["outcome"])
        self.assertEqual(("discovery", "external-network-step"), (status["next"][0]["stage"], status["next"][0]["kind"]))
        self.assertEqual("not-run", status["stages"]["discovery"]["status"])


class CuratedTrackerTests(unittest.TestCase):
    """curated-tracker: the production curated collector over the local fake opener, then the shared stages."""

    def _init(self, **changes):
        flow = Flow(self, fx.curated_request(**changes))
        flow.ok("init", "--request", str(flow.request_path))
        return flow

    def test_curated_fixture_flow_connects_production_curated_collector_analyzer_and_verifier(self):
        flow = self._init()
        step = flow.status()["next"][0]
        self.assertEqual(("collect", "external-network-step", False), (step["stage"], step["kind"], step["executed"]))
        self.assertEqual(["collect_curated_contribution_prs.py", "collect", "--tracker-repo", fx.TRACKER,
                          "--issue", str(fx.TRACKER_ISSUE), "--max-prs", "5", "--request-budget", "200"], step["argv"][:10])
        self.assertIn("not evaluated", step["note"])
        self.assertIn("No isolated entry has run this step against real data yet", step["note"])
        run, opener, paths = flow.curated_collect()
        self.assertEqual(0, run.exit_code)
        self.assertEqual(2, flow.adopt_curated(paths, "--inventory", str(paths["corpus"]), code=2)[0])
        flow.adopt_curated(paths)
        flow.ok("digest")
        flow.ok("adopt-analysis", "--analysis", str(flow.analysis_file()), "--origin", "synthetic-fixture")
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment()
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        flow.ok("report")
        state = flow.state()
        self.assertEqual(["complete"] * len(wf.STAGES), [wf.active(state, stage)["status"] for stage in wf.STAGES])
        status = flow.status()
        self.assertEqual(("insufficient", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        self.assertEqual(flow.collector_requests + len(flow.verifier.calls), status["budget"]["producer_reported_requests"])
        self.assertEqual([len(opener.requests)], [e["requests"] for e in state["ledger"] if e["command"] == "collect"])
        corpus = fx.read_json(flow.gen("collect", "corpus.json"))
        self.assertEqual([fx.pull_url(101), fx.pull_url(102)], sorted(r["pull_request"]["url"] for r in corpus["records"]))
        collect = wf.active(state, "collect")
        self.assertEqual("collecting-curated-contribution-prs", collect["producer"]["generated_by"]["name"])
        self.assertIn("curated", collect["tool_revisions"])
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        for text in ("| Tracker Issue | `%s#%d` |" % (fx.TRACKER, fx.TRACKER_ISSUE),
                     "| Allowed pull request repositories | `%s` |" % fx.REPO, "| Pull request cap (max_prs) | 5 |",
                     "Outcome: `insufficient`", "## Synthetic fixture candidates (not contribution candidates)",
                     "General Issue sources (`issues`) are not connected (pending-unsupported)",
                     "tracker association is not upstream role evidence"):
            self.assertIn(text, report)
        self.assertNotIn("curated-tracker sources are not connected", report)
        self.assertNotIn("| Interval (UTC) |", report)

    def test_curated_request_contract(self):
        self.assertEqual([], wf.validate_request(fx.curated_request()))
        mixed = fx.curated_request()
        mixed["source"]["kind"] = ["curated-tracker", "issues"]
        extra = fx.curated_request()
        extra["source"]["interval"] = {"start_at": fx.INTERVAL[0], "end_at": fx.INTERVAL[1]}
        missing = fx.curated_request()
        del missing["source"]["max_prs"]
        cases = {
            "issue number": fx.curated_request(source__tracker={"repository": fx.TRACKER, "issue_number": 0}),
            "tracker repository": fx.curated_request(source__tracker={"repository": "not a repo", "issue_number": 7}),
            "tracker keys": fx.curated_request(source__tracker={"repository": fx.TRACKER}),
            "empty allow list": fx.curated_request(source__repositories=[]),
            "cap": fx.curated_request(source__max_prs=True),
            "budget": fx.curated_request(source__request_budget=0),
            "mixed kinds": mixed, "interval key": extra, "missing key": missing,
        }
        for label, request in cases.items():
            self.assertTrue(wf.validate_request(request), label)

    def test_curated_manifest_with_other_tracker_cap_or_budget_is_rejected(self):
        cases = {
            "tracker": ({"source__tracker": {"repository": fx.TRACKER, "issue_number": 8}}, "differs from source.tracker"),
            "tracker repository": ({"source__tracker": {"repository": fx.OWNER + "/other-tracker", "issue_number": 7}},
                                   "differs from source.tracker"),
            "cap": ({"source__max_prs": 4}, "max_prs differs"),
            "budget": ({"source__request_budget": 150}, "request_budget differs"),
        }
        for label, (changes, message) in cases.items():
            with self.subTest(label):
                flow = self._init(**changes)
                _, _, paths = flow.curated_collect()
                result = flow.adopt_curated(paths, code=2)
                self.assertIn("scope-conflict", result[2])
                self.assertIn(message, result[2])
                self.assertIsNone(flow.state()["stages"]["collect"]["active"])
                self.assertEqual([], flow.state()["ledger"])
        # A recent-closed collection is not a curated one, even for the same repository.
        flow = self._init()
        _, _, paths = flow.collect()
        result = flow.adopt_curated(paths, code=2)
        self.assertIn("is not tracker-issue-comments", result[2])
        self.assertIn("is not collecting-curated-contribution-prs", result[2])

    def test_curated_pull_requests_outside_the_allow_list_are_rejected(self):
        for external, message in (("hydrated", "corpus record from out-of-scope repository"),
                                  ("missing", "hydrated pull request outside source.repositories")):
            with self.subTest(external):
                flow = self._init()
                _, _, paths = flow.curated_collect(external=external)
                result = flow.adopt_curated(paths, code=2)
                self.assertIn("scope-conflict", result[2])
                self.assertIn(message, result[2])
                self.assertIn(fx.EXTERNAL, result[2])
                self.assertIsNone(flow.state()["stages"]["collect"]["active"])
        # Only the request widens the allow list; then the same collection is adopted.
        flow = self._init(source__repositories=[fx.REPO, fx.EXTERNAL])
        _, _, paths = flow.curated_collect(external="hydrated")
        flow.adopt_curated(paths)
        self.assertEqual(3, wf.active(flow.state(), "collect")["details"]["records"])

    def test_curated_partial_hydration_is_usable_but_marked_partial(self):
        flow = self._init()
        run, _, paths = flow.curated_collect(failing_files=(102,))
        self.assertEqual(3, run.exit_code)
        flow.adopt_curated(paths, code=3)
        status = flow.status()
        self.assertEqual(("partial", "partial"), (status["stages"]["collect"]["status"], status["completeness"]))
        self.assertIn("hydration gap: %s#102" % fx.REPO, status["stages"]["collect"]["reasons"])
        collect = wf.active(flow.state(), "collect")
        self.assertEqual("partial", collect["details"]["repository_status"]["%s#102" % fx.REPO])
        flow.ok("digest")

    def test_curated_numbers_must_be_exact_integers(self):
        flow = self._init(source__max_prs=1)
        _, _, paths = flow.curated_collect(max_prs=1)
        manifest = fx.read_json(paths["manifest"])
        for field, value in (("max_prs", True), ("request_count", False), ("tracker", {"repository": fx.TRACKER, "issue_number": 7.0})):
            with self.subTest(field):
                tampered = json.loads(json.dumps(manifest))
                tampered["records"][-1][field] = value
                path = flow.producer / ("tampered-%s.json" % field)
                fx.write_json(path, tampered)
                result = flow.adopt_curated({"corpus": paths["corpus"], "manifest": path}, code=2)
                self.assertIn("scope-conflict", result[2])
                self.assertNotIn("Traceback", result[2])
        flow.adopt_curated(paths)

    def test_curated_corpus_must_come_from_the_requested_tracker(self):
        flow = self._init()
        _, _, paths = flow.curated_collect()
        corpus = fx.read_json(paths["corpus"])
        other = json.loads(json.dumps(corpus))
        for record in other["records"]:
            for entry in record["sources"]:
                entry["tracker"].update(issue_number=8, api_url=entry["tracker"]["api_url"][:-1] + "8",
                                        html_url=entry["tracker"]["html_url"][:-1] + "8")
        unknown_run = json.loads(json.dumps(corpus))
        unknown_run["records"][0]["sources"][0]["observations"][0]["run_id"] = "run-from-elsewhere"
        mixed = json.loads(json.dumps(corpus))
        mixed["records"][0]["sources"].append({"source_key": "search:x", "collection_method": "recent-closed-search"})
        for label, document, message in (("other tracker", other, "names another tracker source"),
                                         ("unknown run", unknown_run, "is not in the manifest"),
                                         ("mixed source", mixed, "has a source that is not tracker-issue-comments")):
            with self.subTest(label):
                path = flow.producer / ("corpus-%s.json" % label.replace(" ", "-"))
                fx.write_json(path, document)
                result = flow.adopt_curated({"corpus": path, "manifest": paths["manifest"]}, code=2)
                self.assertIn(message, result[2])
                self.assertIsNone(flow.state()["stages"]["collect"]["active"])

    def test_malformed_curated_manifests_fail_cleanly_and_keep_the_checkpoint(self):
        flow = self._init()
        _, _, paths = flow.curated_collect(failing_files=(102,))
        flow.adopt_curated(paths, code=3)
        before = flow.state()
        manifest = fx.read_json(paths["manifest"])
        cases = {
            "failed scope number": lambda r: r["failed_scopes"][0].pop("number"),
            "reference counts type": lambda r: r["reference_counts"].update(excluded_by_cap="1"),
            "reference counts container": lambda r: r.update(reference_counts=[]),
            "hydration outcomes container": lambda r: r.update(hydration_outcomes={"repository": fx.REPO}),
            "failed scope item": lambda r: r["failed_scopes"].append("hydration"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                tampered = json.loads(json.dumps(manifest))
                change(tampered["records"][-1])
                path = flow.producer / ("malformed-%s.json" % label.replace(" ", "-"))
                fx.write_json(path, tampered)
                result = flow.adopt_curated({"corpus": paths["corpus"], "manifest": path}, "--replace", code=2)
                self.assertIn("scope-conflict", result[2])
                self.assertNotIn("Traceback", result[2])
                after = flow.state()
                self.assertEqual(before["stages"], after["stages"])
                self.assertEqual(before["ledger"], after["ledger"])

    def test_malformed_curated_identifiers_are_rejected_without_a_type_error(self):
        flow = self._init()
        _, _, paths = flow.curated_collect()
        flow.adopt_curated(paths)
        before = flow.state()
        corpus, manifest = fx.read_json(paths["corpus"]), fx.read_json(paths["manifest"])
        node = json.loads(json.dumps(corpus))
        node["records"][0]["sources"][0]["tracker"]["issue_node_id"] = []
        observation = json.loads(json.dumps(corpus))
        observation["records"][0]["sources"][0]["observations"][0]["run_id"] = {}
        run = json.loads(json.dumps(manifest))
        run["records"][0]["run_id"] = []
        for label, document_corpus, document_manifest, message in (
                ("issue node", node, manifest, "issue_node_id must be a non-empty string"),
                ("observation run", observation, manifest, "observation run {} is not in the manifest"),
                ("manifest run", corpus, run, "manifest run_id must be a non-empty string")):
            with self.subTest(label):
                corpus_path = flow.producer / ("id-corpus-%s.json" % label.replace(" ", "-"))
                manifest_path = flow.producer / ("id-manifest-%s.json" % label.replace(" ", "-"))
                fx.write_json(corpus_path, document_corpus)
                fx.write_json(manifest_path, document_manifest)
                result = flow.adopt_curated({"corpus": corpus_path, "manifest": manifest_path}, "--replace", code=2)
                self.assertIn("scope-conflict", result[2])
                self.assertIn(message, result[2])
                after = flow.state()
                self.assertEqual((before["stages"], before["ledger"]), (after["stages"], after["ledger"]))

    def test_curated_resume_counts_the_running_total_once(self):
        flow = self._init()
        _, first_opener, first = flow.curated_collect("first", failing_files=(102,))
        flow.adopt_curated(first, code=3)
        _, second_opener, resumed = flow.curated_collect("resumed", resume_from=first, captured_at="2026-09-10T01:00:00Z")
        manifest = fx.read_json(resumed["manifest"])
        self.assertEqual(1, len(manifest["records"]))
        self.assertEqual(len(first_opener.requests) + len(second_opener.requests), manifest["records"][0]["request_count"])
        flow.adopt_curated(resumed, "--replace")
        state = flow.state()
        collect_entries = [e for e in state["ledger"] if e["command"] == "collect"]
        self.assertEqual([len(first_opener.requests) + len(second_opener.requests)], [e["requests"] for e in collect_entries])
        self.assertEqual(flow.collector_requests, flow.status()["budget"]["producer_reported_requests"])
        self.assertEqual("complete", wf.active(state, "collect")["status"])
        # Adopting the same resumed manifest again reuses it and adds nothing.
        flow.adopt_curated(resumed)
        self.assertEqual(state["ledger"], flow.state()["ledger"])
        # An older, smaller total for the same run is a conflict, not a second count.
        result = flow.adopt_curated(first, "--replace", code=2)
        self.assertIn("conflicting request count", result[2])
        self.assertEqual(state["ledger"], flow.state()["ledger"])


class ReviewFindingTests(unittest.TestCase):
    """Coordinator review paths: revision drift, ledger integrity, symlinks, policy bytes."""

    def _through_locus(self):
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment()
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        return flow, assessment, locus, locus_manifest

    def test_revision_drift_blocks_until_the_stage_is_readopted(self):
        flow = Flow(self)
        flow.through_analysis()
        state = flow.state()
        wf.active(state, "analysis")["tool_revisions"]["analyzer"] = "sha256:" + "0" * 64
        fx.write_json(flow.run_dir / "state.json", state)
        status = flow.status()
        self.assertEqual("blocked", status["outcome"])
        self.assertEqual({"analysis": ["tool-revision-changed:analyzer"]}, status["integrity_failures"])
        discovery, manifest = flow.producer / "none.json", flow.producer / "none-manifest.json"
        result = flow.cli("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        self.assertIn("tool-revision-changed:analyzer", result[2])
        flow.ok("report")
        self.assertIn("Not rendered: blocked checkpoints", (flow.run_dir / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(2, flow.cli("adopt-analysis", "--analysis", str(flow.producer / "analysis.json"), "--origin", "synthetic-fixture")[0])
        flow.ok("adopt-analysis", "--analysis", str(flow.producer / "analysis.json"), "--origin", "synthetic-fixture", "--replace")
        self.assertEqual(("awaiting-input", {}), (flow.status()["outcome"], flow.status()["integrity_failures"]))

    def test_malformed_or_conflicting_request_counts_are_rejected(self):
        flow = Flow(self)
        flow.through_analysis()
        discovery, manifest = flow.discover()
        broken = fx.read_json(manifest)
        for value in (None, -1, True, "20"):
            broken["runs"][0]["budget"]["requests_consumed"] = value
            fx.write_json(flow.producer / "broken-manifest.json", broken)
            result = flow.cli("adopt-discovery", "--discovery", str(discovery), "--manifest", str(flow.producer / "broken-manifest.json"))
            self.assertIn("no valid request count", result[2], value)
        self.assertEqual(1, len(flow.state()["ledger"]))
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment()
        locus, locus_manifest = flow.locus_search(assessment)
        conflicting = fx.read_json(locus_manifest)
        conflicting["runs"][0]["budget"]["requests_consumed"] += 1
        fx.write_json(locus_manifest, conflicting)
        result = flow.cli("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        self.assertIn("conflicting request count", result[2])
        self.assertEqual(["collect", "discover"], [e["command"] for e in flow.state()["ledger"]])

    def test_missing_timestamps_make_a_time_budget_unverifiable(self):
        flow = Flow(self, fx.base_request(budget__max_elapsed_seconds=10 ** 9))
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        manifest = fx.read_json(paths["manifest"])
        manifest["records"][-1]["completed_at"] = None
        fx.write_json(paths["manifest"], manifest)
        flow.adopt_collection(paths, code=4)
        self.assertIn("time-budget-unverifiable:1 runs without timestamps", flow.status()["budget"]["violations"])

    def test_symlinks_never_redirect_writes_or_reads_outside_the_run(self):
        flow = Flow(self)
        flow.through_analysis()
        sentinel = flow.tmp / "outside-sentinel.md"
        sentinel.write_text("outside\n", encoding="utf-8")
        (flow.run_dir / "report.md").symlink_to(sentinel)
        self.assertIn("symlinked", flow.cli("report")[2])
        self.assertEqual("outside\n", sentinel.read_text(encoding="utf-8"))
        (flow.run_dir / "report.md").unlink()
        generation = flow.run_dir / "stages" / "digest" / "gen-001"
        outside = flow.tmp / "outside-digest"
        shutil.move(str(generation), str(outside))
        generation.symlink_to(outside, target_is_directory=True)
        status = flow.status()
        self.assertEqual({"digest": ["gen-001:not-a-real-directory-in-run"]}, status["integrity_failures"])
        moved = flow.tmp / "moved-run"
        os.rename(flow.run_dir, moved)
        os.symlink(moved, flow.run_dir, target_is_directory=True)
        self.assertIn("run directory", flow.cli("status")[2])

    def test_policy_evidence_binds_referenced_bytes(self):
        flow, assessment, _, _ = self._through_locus()
        content = flow.producer / "ai-policy.md"
        content.write_text(fx.SYNTHETIC + " assistance policy v1\n", encoding="utf-8")
        evidence = flow.producer / "policy-evidence.json"
        fx.write_json(evidence, {"schema_version": "1.0.0", "observations": [{
            "repository": fx.REPO, "policy_key": "ai_policy", "path": "docs/ai.md", "source_repository": fx.REPO,
            "source_url": None, "content_path": str(content)}]})
        record = ("record", "--assessment", str(assessment), "--origin", "synthetic-fixture", "--policy-evidence", str(evidence))
        flow.ok(*record)
        first = wf.active(flow.state(), "record")
        self.assertEqual(fx.sha256_file(content), first["inputs"]["policy_content_00"]["sha256"])
        flow.ok(*record)
        self.assertEqual(1, len(flow.state()["stages"]["record"]["generations"]))
        content.write_text(fx.SYNTHETIC + " assistance policy v2\n", encoding="utf-8")
        flow.ok(*record)
        state = flow.state()
        self.assertEqual(2, len(state["stages"]["record"]["generations"]))
        stored = flow.gen("record", "policy-evidence.json")
        bound = Path(fx.read_json(stored)["observations"][0]["content_path"])
        self.assertEqual(flow.gen("record", "policy-content/00.bin"), bound)
        self.assertTrue(bound.is_file())
        self.assertEqual(state["stages"]["record"]["generations"][-1]["inputs"]["policy_content_00"]["sha256"],
                         fx.sha256_file(bound))
        # The stored copy re-verifies on its own: the producer directory is no longer needed.
        shutil.rmtree(flow.producer / "verify")
        content.unlink()
        evidence.unlink()
        flow.ok("record", "--assessment", str(flow.gen("record", "assessment.json")), "--origin", "synthetic-fixture",
                "--policy-evidence", str(stored))
        # Rebound paths make the stored JSON different bytes, so this is a third, verified generation.
        self.assertEqual(3, len(flow.state()["stages"]["record"]["generations"]))

    def test_policy_content_must_stay_inside_the_evidence_directory(self):
        flow, assessment, _, _ = self._through_locus()
        sentinel = flow.tmp / "outside-secret.txt"
        sentinel.write_text("SENTINEL-DO-NOT-READ\n", encoding="utf-8")
        evidence_dir = flow.producer / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "link.md").symlink_to(sentinel)
        (evidence_dir / "sub").mkdir()
        for content_path in (str(sentinel), "../../outside-secret.txt", "link.md", "sub", "missing.md",
                             "https://example.invalid/policy.md"):
            evidence = evidence_dir / "policy-evidence.json"
            fx.write_json(evidence, {"schema_version": "1.0.0", "observations": [{
                "repository": fx.REPO, "policy_key": "ai_policy", "path": "docs/ai.md", "source_repository": fx.REPO,
                "source_url": None, "content_path": content_path}]})
            code, _, stderr = flow.cli("record", "--assessment", str(assessment), "--origin", "synthetic-fixture",
                                       "--policy-evidence", str(evidence))
            self.assertEqual(2, code, content_path)
            self.assertIn("content_path", stderr)
        self.assertIsNone(flow.state()["stages"]["record"]["active"])
        self.assertEqual([], list((flow.run_dir / "stages" / "record").iterdir()))
        for path in flow.run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SENTINEL-DO-NOT-READ", path.read_bytes(), path)

    def test_bad_request_files_fail_without_a_traceback(self):
        flow = Flow(self)
        flow.request_path.write_text("{not json", encoding="utf-8")
        code, _, stderr = flow.cli("init", "--request", str(flow.request_path))
        self.assertEqual((2, True), (code, stderr.startswith("error: ")))
        code, _, stderr = flow.cli("init", "--request", str(flow.tmp / "missing.json"))
        self.assertEqual((2, True), (code, stderr.startswith("error: ")))
        fx.write_json(flow.request_path, fx.base_request(source__interval={"start_at": "2026-13-01", "end_at": fx.INTERVAL[1]}))
        code, _, stderr = flow.cli("init", "--request", str(flow.request_path))
        self.assertEqual(2, code)
        self.assertIn("YYYY-MM-DDTHH:MM:SSZ", stderr)
        self.assertFalse(flow.run_dir.exists())


class SourceResolutionTests(unittest.TestCase):
    """Static staging contract only: not proof that Claude or Codex installed or invoked the skill."""

    def _stage_skill(self, home, sibling_parent):
        skill = home / ".agents" / "skills" / wf.SKILL_NAME
        (skill / "scripts").mkdir(parents=True)
        shutil.copyfile(fx.SKILL_DIR / "scripts" / "oss_workflow.py", skill / "scripts" / "oss_workflow.py")
        for skill_name, script in wf.SIBLINGS.values():
            target = sibling_parent / skill_name / script
            target.parent.mkdir(parents=True)
            target.write_text("# staged placeholder\n", encoding="utf-8")
        spec = __import__("importlib.util").util.spec_from_file_location("staged_oss_workflow_%d" % id(home), skill / "scripts" / "oss_workflow.py")
        module = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_codex_install_layout_resolves_without_request_input(self):
        home = Path(tempfile.mkdtemp(prefix="oss-workflow-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        staged = self._stage_skill(home, home / ".codex" / "skills")
        resolution = staged.resolve_siblings(None, environ={}, home=home)
        self.assertEqual(("codex-install", str(home / ".codex" / "skills")), (resolution["source"], resolution["root"]))
        explicit = staged.resolve_siblings(str(fx.SKILLS_ROOT), environ={}, home=home)
        self.assertEqual("--skills-root", explicit["source"])
        via_env = staged.resolve_siblings(None, environ={staged.ROOT_ENVIRONMENT: str(fx.SKILLS_ROOT)}, home=home)
        self.assertEqual(staged.ROOT_ENVIRONMENT, via_env["source"])

    def test_shared_layout_wins_after_siblings_move_to_agents(self):
        home = Path(tempfile.mkdtemp(prefix="oss-workflow-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        staged = self._stage_skill(home, home / ".agents" / "skills")
        resolution = staged.resolve_siblings(None, environ={}, home=home)
        self.assertEqual("shared-skill-parent", resolution["source"])
        empty = Path(tempfile.mkdtemp(prefix="oss-workflow-empty-"))
        self.addCleanup(shutil.rmtree, empty, True)
        with self.assertRaises(staged.WorkflowError):
            staged.resolve_siblings(str(empty), environ={}, home=home)


if __name__ == "__main__":
    unittest.main()
