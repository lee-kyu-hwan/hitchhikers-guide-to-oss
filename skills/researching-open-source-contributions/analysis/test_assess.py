"""The candidate assessment as one isolated model call (#259): its input, its prompt and its strict check.

S1-S4 of the issue, over the workflow's synthetic offline world: the real collector and the real verifier
``discover`` and ``locus-search`` run against fake GitHub replies (offline_fixture), and every model answer below
is a hand-written stand-in. The strict check (verify_e2e_result.py --answer-kind assess) runs in process, and it
runs the verifier's own ``record`` gate in process too, so the offline guards stay in place. No model, VM, token or
network is used.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_assess.py
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "dot_agents/skills/researching-open-source-contributions/tests"))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import offline_fixture as fx  # noqa: E402  (installs the offline guards first)
import stage_inputs  # noqa: E402
import verify_e2e_result  # noqa: E402
import machine_settings as machine  # noqa: E402

vc = fx.vc
PROMPT_CAP = 64 * 1024
CONTRIBUTING_SOURCE = fx.REPO + ":CONTRIBUTING.md"
CONTRIBUTING_SHA = hashlib.sha256(fx.CONTRIBUTING_TEXT.encode("utf-8")).hexdigest()
OTHER_SHA = hashlib.sha256(b"bytes the input never held").hexdigest()


def stage(*argv):
    code, stdout, stderr = fx.workflow.run_main(stage_inputs.main, list(argv))
    return code, (json.loads(stdout) if code == 0 else None), stderr


class World:
    """Analysis and discovery of the synthetic world, made once: collector, synthetic analysis, verifier discover."""

    def __init__(self, root: Path):
        self.root = root
        _, _, paths = fx.run_collector(root / "collect")
        corpus = fx.read_json(paths["corpus"])
        self.pr_urls = sorted(record["pull_request"]["url"] for record in corpus["records"])
        self.analysis = root / "analysis.json"
        fx.write_json(self.analysis, fx.synthetic_analysis(corpus))
        self.verifier = fx.VerifierWorld()
        self.discovery = root / "discovery.json"
        self.manifest = root / "verification-manifest.json"
        code, _, stderr = self.verifier.main([
            "discover", "--analysis", str(self.analysis), "--repo", fx.REPO, "--output", str(self.discovery),
            "--manifest", str(self.manifest), "--request-budget", "60"])
        assert code == 0, stderr
        self.discovery_sha = fx.sha256_file(self.discovery)
        self.record = fx.read_json(self.discovery)["records"][0]
        self.code_hit = fx.CODE_HIT_URL
        # A hand-reviewed policy: the CONTRIBUTING.md bytes discovery fetched, read again for CLA/DCO and AI policy.
        content = root / "contributing-reviewed.md"
        content.write_text(fx.CONTRIBUTING_TEXT, encoding="utf-8")
        self.policy_evidence = root / "policy-evidence.json"
        fx.write_json(self.policy_evidence, {"schema_version": "1.0.0", "observations": [
            {"repository": fx.REPO, "policy_key": key, "path": "CONTRIBUTING.md", "source_repository": fx.REPO,
             "source_url": fx.REPO_URL + "/blob/main/CONTRIBUTING.md", "content_path": str(content)}
            for key in ("cla_or_dco", "ai_policy")]})

    def locus_search(self, assessment: Path, out: Path) -> Path:
        manifest = out.parent / (out.stem + "-manifest.json")
        shutil.copyfile(self.manifest, manifest)
        code, _, stderr = self.verifier.main([
            "locus-search", "--discovery", str(self.discovery), "--assessment", str(assessment), "--output", str(out),
            "--manifest", str(manifest), "--request-budget", "40"])
        assert code == 0, stderr
        return out


def readiness(result="not-confirmed", links=(), note="SYNTHETIC: not assessed"):
    return {key: {"result": result, "evidence_links": list(links), "note": note} for key in vc.READINESS_KEYS}


def unreviewed_policies():
    return {key: {"found": None, "source": None, "sha256": None, "assessment": "SYNTHETIC: not reviewed",
                  "evidence_links": []} for key in vc.POLICY_CHECK_KEYS}


def assessment(world: World, **changes) -> dict:
    """A conservative stand-in answer (policy-review, policy-unknown) citing only input links."""
    item = {
        "pattern_id": fx.PATTERN_ID, "repository": fx.REPO, "locus": fx.LOCUS,
        "status": "policy-review", "status_reason": "policy-unknown", "sensitivity": "normal",
        "summary": "SYNTHETIC: the fixture locus uses a fixed delay.", "impact": "SYNTHETIC: retries bunch up.",
        "readiness_checks": readiness(),
        "duplicate_verdict": {"matched_items": [], "judgment": "SYNTHETIC: no issue or PR names the locus."},
        "ai_policy_status": "unknown", "disclosure_required": None, "private_evidence_reference": None,
        "evidence_links": [world.code_hit] + world.pr_urls[:1], "execution_evidence": None,
        "reproduction": {"method": "none", "evidence_links": [], "public_steps": None},
        "policy_checks": unreviewed_policies(), "blocking_gaps": ["SYNTHETIC: policies not reviewed"],
        "superseded_by": None,
    }
    item.update(changes)
    return item


def document(world: World, *items, discovery_sha=None) -> dict:
    return {"schema_version": "1.0.0", "discovery_sha256": discovery_sha or world.discovery_sha,
            "assessments": list(items) or [assessment(world)]}


def reviewed_policies(world: World) -> dict:
    """The policy result an issue-ready answer can claim from the input: CONTRIBUTING found, the five path keys
    absent everywhere, CLA/DCO and AI policy absent as hand-reviewed over the one found file."""
    policies = {}
    for key in vc.POLICY_CHECK_KEYS:
        policies[key] = {"found": False, "source": None, "sha256": None,
                         "assessment": "SYNTHETIC: every mapped path is absent", "evidence_links": []}
    policies["contributing"] = {"found": True, "source": CONTRIBUTING_SOURCE, "sha256": CONTRIBUTING_SHA,
                                "assessment": "SYNTHETIC: asks for an issue before large changes",
                                "evidence_links": [fx.REPO_URL + "/blob/main/CONTRIBUTING.md"]}
    for key in ("cla_or_dco", "ai_policy"):
        policies[key]["assessment"] = "SYNTHETIC: the reviewed CONTRIBUTING.md has no such policy"
    policies["program_rules"] = {"found": None, "source": None, "sha256": None,
                                 "assessment": "SYNTHETIC: no program rules input", "evidence_links": []}
    return policies


def ready_assessment(world: World, **changes) -> dict:
    links = [world.code_hit]
    item = assessment(world, status="issue-ready", status_reason="ready", ai_policy_status="allowed",
                      readiness_checks=readiness("confirmed", links, "SYNTHETIC: from the input"),
                      policy_checks=reviewed_policies(world), blocking_gaps=[],
                      reproduction={"method": "static", "evidence_links": links,
                                    "public_steps": ["SYNTHETIC: read the fixture locus."]})
    item["readiness_checks"]["rejection_still_valid"] = {"result": "not-applicable", "evidence_links": [],
                                                          "note": "SYNTHETIC: no prior rejection"}
    item.update(changes)
    return item


class AssessCase(unittest.TestCase):
    world = None

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="e2e-assess-")).resolve()
        cls.world = World(cls.base / "world")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=self.base))

    def inputs(self, *extra, name="in-assess"):
        code, summary, stderr = stage("assess-inputs", "--analysis", str(self.world.analysis), "--discovery",
                                      str(self.world.discovery), *extra, "--out", str(self.root / name))
        self.assertEqual(0, code, stderr)
        return summary, self.root / name

    # -- a collected worker result and its strict verdict -------------------

    def run_dir(self, answer, prompt: Path, name="call", manifest_edit=None) -> Path:
        run = self.root / name
        (run / "result").mkdir(parents=True)
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        (run / "result/report.md").write_text(text, encoding="utf-8")
        manifest = {"status": "succeeded", "profile": "claude-profile2", "exit_code": 0, "failures": [], "problems": [],
                    "permission_mode": "dontAsk", "requested_model": "claude-opus-5-5", "requested_effort": "medium",
                    "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                    "observed_models": ["claude-opus-5-5"],
                    "prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                    "report_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        if manifest_edit:
            manifest_edit(manifest)
        (run / "result/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return run

    def strict(self, answer, *extra, policy_evidence=False, locus_search=None, adopt=None, inputs=None,
               discovery=None, manifest_edit=None):
        if inputs is None:
            _, inputs = self.inputs(*(["--policy-evidence", str(self.world.policy_evidence)] if policy_evidence else []),
                                    name="in-assess-%d" % len(list(self.root.glob("in-assess-*"))))
        prompt = inputs / "assess" / "task.txt"
        run = self.run_dir(answer, prompt, name="call-%d" % len(list(self.root.glob("call-*"))),
                           manifest_edit=manifest_edit)
        argv = ["--run-dir", str(run), "--discovery", str(discovery or self.world.discovery), "--prompt", str(prompt),
                "--answer-kind", "assess", "--input-scope", str(inputs / "assess.scope.json"), *extra]
        if policy_evidence:
            argv += ["--policy-evidence", str(self.world.policy_evidence)]
        if locus_search is not None:
            argv += ["--locus-search", str(locus_search)]
        if adopt is not None:
            argv += ["--adopt-to", str(adopt)]
        code, stdout, stderr = fx.workflow.run_main(verify_e2e_result.main, argv)
        self.assertTrue(stdout, stderr)
        doc = json.loads(stdout)
        return code, {item["id"]: item for item in doc["items"]}, doc

    def assert_status(self, items, expected: dict):
        got = {key: items[key]["status"] for key in expected}
        self.assertEqual(expected, got, {key: items[key]["detail"] for key in expected})


class S1AssessInputs(AssessCase):
    """S1: a synthetic discovery and pattern make one assess input within 65,536 bytes and its scope file."""

    def test_one_call_within_the_cap_with_its_scope_and_call_plan(self):
        summary, out = self.inputs()
        prompt = (out / "assess" / "task.txt").read_bytes()
        self.assertLessEqual(len(prompt), PROMPT_CAP)
        self.assertEqual([{"input_dir": "assess", "bytes": len(prompt), "sha256": hashlib.sha256(prompt).hexdigest(),
                           "result_name": "assess.result.json"}], summary["calls"])
        print("\nS1 synthetic assess input: %d bytes, sha256 %s" % (len(prompt), summary["calls"][0]["sha256"]))
        plan = summary["call_plan"]
        self.assertEqual({"mode": "assess", "assess": 1, "max_model_calls": 1}, {k: plan[k] for k in ("mode", "assess", "max_model_calls")})
        self.assertEqual(json.loads((out / "stage.json").read_text(encoding="utf-8")), summary)
        self.assertEqual(("assess", 1), (summary["stage"], summary["max_model_calls"]))
        self.assertEqual(oct(os.stat(out / "assess" / "task.txt").st_mode & 0o777), oct(0o444))

        scope = json.loads((out / "assess.scope.json").read_text(encoding="utf-8"))
        self.assertEqual("assess", scope["kind"])
        self.assertEqual(self.world.discovery_sha, scope["discovery_sha256"])
        self.assertEqual(fx.sha256_file(self.world.analysis), scope["analysis_sha256"])
        self.assertEqual([{"pattern_id": fx.PATTERN_ID, "repository": fx.REPO}], scope["combinations"])
        self.assertIsNone(scope["policy_evidence_sha256"])
        for link in [self.world.code_hit, fx.REPO_URL, fx.REPO_URL + "/commit/" + fx.HEAD_SHA,
                     fx.REPO_URL + "/blob/main/CONTRIBUTING.md"] + self.world.pr_urls:
            self.assertIn(link, scope["links"])
        self.assertEqual(sorted(set(scope["links"])), scope["links"])
        self.assertEqual([{"key": key, "source": CONTRIBUTING_SOURCE, "sha256": CONTRIBUTING_SHA}
                          for key in ("contributing", "cla_or_dco", "ai_policy")], scope["policy"])

    def test_the_prompt_carries_the_inputs_as_inert_data_blocks(self):
        _, out = self.inputs()
        text = (out / "assess" / "task.txt").read_text(encoding="utf-8")
        template = (HERE / "prompts" / "assess.md").read_text(encoding="utf-8")
        # the field types come from the one table the strict check uses (assess_contract, #266)
        template = template.replace("{{assessment_field_types}}", stage_inputs.load_assess_contract().prompt_lines(vc))
        self.assertTrue(text.startswith(template))
        for title in ("판정 단위", "패턴 " + fx.PATTERN_ID, "저장소 사실 " + fx.REPO, "정책 관찰 " + fx.REPO,
                      "정책 키별 허용 값 " + fx.REPO, "discovery 관찰 %s × %s" % (fx.PATTERN_ID, fx.REPO),
                      "허용 근거 링크"):
            self.assertIn("\n=== %s ===\n" % title, text)
            self.assertIn("\n=== %s 끝 ===" % title, text)
        self.assertNotIn("손으로 확인한 정책 근거", text.split(template, 1)[1])
        self.assertIn(fx.CONTRIBUTING_TEXT.strip(), text)       # the excerpt the verifier fetched, as data
        self.assertIn(fx.PATTERN_CLUE, text)
        self.assertNotIn("pattern_history", text)

    def test_the_same_inputs_make_the_same_bytes(self):
        first, _ = self.inputs(name="a")
        second, _ = self.inputs(name="b")
        self.assertEqual(first["calls"], second["calls"])

    def test_hand_reviewed_policy_evidence_enters_the_input_and_the_scope(self):
        summary, out = self.inputs("--policy-evidence", str(self.world.policy_evidence))
        text = (out / "assess" / "task.txt").read_text(encoding="utf-8")
        self.assertIn("\n=== 손으로 확인한 정책 근거 ===\n", text)
        scope = json.loads((out / "assess.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(fx.sha256_file(self.world.policy_evidence), scope["policy_evidence_sha256"])
        self.assertEqual(summary["policy_evidence_sha256"], scope["policy_evidence_sha256"])
        self.assertIn(fx.REPO_URL + "/blob/main/CONTRIBUTING.md", scope["links"])

    def test_refused_before_anything_is_written(self):
        other = self.root / "other-analysis.json"
        other.write_bytes(self.world.analysis.read_bytes() + b"\n")
        empty = self.root / "empty-discovery.json"
        discovery = fx.read_json(self.world.discovery)
        fx.write_json(empty, dict(discovery, records=[], skipped_by_cap=[], failed_scopes=[]))
        huge = self.root / "huge-discovery.json"
        record = deepcopy(discovery["records"][0])
        record["static_search"]["read_failures"] = [{"path": "x" * 200, "operation": "read"}] * 400
        fx.write_json(huge, dict(discovery, records=[record]))
        cases = {"analysis not the one discovery read": (other, self.world.discovery, "analysis_sha256"),
                 "no discovery record": (self.world.analysis, empty, "no discovery record"),
                 "over the prompt cap": (self.world.analysis, huge, "65536")}
        for label, (analysis, discovery_path, reason) in cases.items():
            with self.subTest(label):
                out = self.root / ("refused-" + label.replace(" ", "-"))
                code, _, stderr = stage("assess-inputs", "--analysis", str(analysis), "--discovery",
                                        str(discovery_path), "--out", str(out))
                self.assertEqual(2, code)
                self.assertIn(reason, stderr)
                self.assertFalse(out.exists())


class S2AdoptableAnswers(AssessCase):
    """S2: an answer by the contract passes strict and the verifier's own record gate."""

    def test_a_conservative_answer_passes_and_is_adopted(self):
        adopt = self.root / "assessment.json"
        code, items, doc = self.strict(document(self.world), adopt=adopt)
        self.assertEqual(0, code, doc)
        self.assert_status(items, {"C1-discovery": "PASS", "R3-report-schema": "PASS", "A1-discovery-binding": "PASS",
                                   "R5-input-scope": "PASS", "A2-policy-bytes": "PASS", "A3-record-gate": "PASS",
                                   "A4-assess-rules": "PASS",
                                   "Q1-quotes-verbatim": "INFO", "P4-prompt-binding": "PASS", "M5-observed-models": "PASS"})
        self.assertEqual(("evidence", "assess", "strict"), (doc["result_class"], doc["answer_kind"], doc["limits"]))
        self.assertEqual(json.dumps(document(self.world), ensure_ascii=False), adopt.read_text(encoding="utf-8"))
        self.assertEqual(0o444, os.stat(adopt).st_mode & 0o777)

    def test_an_honest_unverified_answer_is_insufficient_not_invalid(self):
        answer = document(self.world, assessment(self.world, status="unverified", status_reason="insufficient-evidence",
                                                 evidence_links=[]))
        code, items, doc = self.strict(answer)
        self.assertEqual((0, "insufficient"), (code, doc["result_class"]), doc)
        self.assert_status(items, {"A3-record-gate": "PASS"})

    def test_a_reproduced_answer_with_its_static_reproduction_passes(self):
        links = [self.world.code_hit]
        item = assessment(self.world, status="reproduced", status_reason="none",
                          reproduction={"method": "static", "evidence_links": links,
                                        "public_steps": ["SYNTHETIC: read the fixture locus."]})
        item["readiness_checks"]["reproduction_evidence"] = {"result": "confirmed", "evidence_links": links,
                                                             "note": "SYNTHETIC: fixture code-search hit"}
        code, items, doc = self.strict(document(self.world, item))
        self.assertEqual(0, code, doc)
        self.assert_status(items, {"A3-record-gate": "PASS", "R5-input-scope": "PASS"})

    def test_an_issue_ready_answer_passes_once_its_locus_was_searched_again(self):
        answer = document(self.world, ready_assessment(self.world))
        code, items, _ = self.strict(answer, policy_evidence=True)
        self.assertEqual(1, code)
        self.assert_status(items, {"R3-report-schema": "PASS", "R5-input-scope": "PASS", "A2-policy-bytes": "PASS",
                                   "A3-record-gate": "FAIL"})
        self.assertIn("locus_search: missing locus search input", items["A3-record-gate"]["detail"])
        # The locus search reads only pattern, repository and locus of the collected answer (inert data).
        collected = self.root / "collected-assessment.json"
        collected.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        locus = self.world.locus_search(collected, self.root / "locus-search.json")
        adopt = self.root / "assessment.json"
        code, items, doc = self.strict(answer, policy_evidence=True, locus_search=locus, adopt=adopt)
        self.assertEqual(0, code, doc)
        self.assert_status(items, {"A3-record-gate": "PASS", "C2-policy-evidence": "PASS"})
        self.assertEqual(fx.sha256_file(collected), fx.sha256_file(adopt))


class S3OutsideTheInput(AssessCase):
    """S3: what the input did not hold fails, each with its own item and reason."""

    def test_a_link_the_input_did_not_hold(self):
        outside = fx.REPO_URL + "/issues/999"
        item = assessment(self.world, evidence_links=[self.world.code_hit, outside])
        code, items, doc = self.strict(document(self.world, item))
        self.assertEqual((1, "invalid"), (code, doc["result_class"]))
        self.assert_status(items, {"R5-input-scope": "FAIL", "A2-policy-bytes": "PASS", "A3-record-gate": "PASS"})
        self.assertIn(outside, items["R5-input-scope"]["detail"])
        self.assertIsNone(doc.get("adopted_to"))

    def test_every_link_field_is_in_scope(self):
        outside = "https://example.invalid/x"
        fields = {
            "readiness": lambda item: item["readiness_checks"]["present_on_head"].update(
                result="confirmed", evidence_links=[outside]),
            "policy": lambda item: item["policy_checks"]["contributing"].update(evidence_links=[outside]),
            "reproduction": lambda item: item["reproduction"].update(evidence_links=[outside]),
            "duplicate verdict": lambda item: item["duplicate_verdict"].update(matched_items=[outside]),
        }
        for label, edit in fields.items():
            with self.subTest(label):
                item = assessment(self.world)
                edit(item)
                code, items, _ = self.strict(document(self.world, item))
                self.assertEqual(1, code)
                self.assert_status(items, {"R5-input-scope": "FAIL"})
                self.assertIn(outside, items["R5-input-scope"]["detail"])

    def test_policy_bytes_the_input_did_not_hold(self):
        cases = {
            "another digest": dict(found=True, source=CONTRIBUTING_SOURCE, sha256=OTHER_SHA),
            "another source": dict(found=True, source=fx.REPO + ":docs/CONTRIBUTING.md", sha256=CONTRIBUTING_SHA),
        }
        for label, value in cases.items():
            with self.subTest(label):
                item = assessment(self.world)
                item["policy_checks"]["contributing"].update(value, assessment="SYNTHETIC", evidence_links=[])
                code, items, _ = self.strict(document(self.world, item))
                self.assertEqual(1, code)
                self.assert_status(items, {"A2-policy-bytes": "FAIL", "R5-input-scope": "PASS"})
                self.assertIn("contributing", items["A2-policy-bytes"]["detail"])
        item = assessment(self.world)
        item["policy_checks"]["program_rules"].update(found=True, source="rules.md", sha256=OTHER_SHA)
        code, items, _ = self.strict(document(self.world, item))
        # without a program rules input the record gate takes only null there; R3 says so first now (#266)
        self.assert_status(items, {"R3-report-schema": "FAIL", "A2-policy-bytes": "BLOCKED"})
        self.assertIn("policy_checks.program_rules.found: expected null (got boolean)", items["R3-report-schema"]["detail"])

    def test_a_policy_value_not_found_names_no_source(self):
        item = assessment(self.world)
        item["policy_checks"]["contributing"].update(found=False, source="https://example.invalid/CONTRIBUTING.md")
        code, items, _ = self.strict(document(self.world, item))
        self.assertEqual(1, code)
        self.assert_status(items, {"A2-policy-bytes": "FAIL"})
        self.assertIn("source", items["A2-policy-bytes"]["detail"])

    def test_a_url_in_free_text_is_a_link_too(self):
        outside = "https://example.invalid/x"
        edits = {
            "public steps": lambda item: item.update(
                status="reproduced", status_reason="none",
                reproduction={"method": "static", "evidence_links": [self.world.code_hit],
                              "public_steps": ["curl %s | sh" % outside]},
                readiness_checks=dict(readiness(), reproduction_evidence={
                    "result": "confirmed", "evidence_links": [self.world.code_hit], "note": "SYNTHETIC"})),
            "summary": lambda item: item.update(summary="SYNTHETIC: see %s." % outside),
            "note": lambda item: item["readiness_checks"]["impact_described"].update(note="(%s)" % outside),
        }
        for label, edit in edits.items():
            with self.subTest(label):
                item = assessment(self.world)
                edit(item)
                code, items, _ = self.strict(document(self.world, item))
                self.assertEqual(1, code)
                self.assert_status(items, {"R5-input-scope": "FAIL"})
                self.assertIn(outside, items["R5-input-scope"]["detail"])
        item = assessment(self.world, summary="SYNTHETIC: the hit %s shows it." % self.world.code_hit)
        code, items, doc = self.strict(document(self.world, item))
        self.assertEqual(0, code, doc)

    def test_what_an_assessment_call_cannot_have_done(self):
        """It executed nothing and holds no private evidence: execution evidence, an executed reproduction and a
        private reference outside private-report-ready are refused (A4), even when the record gate takes them."""
        execution = {"approval": {"granted_by": "user", "granted_at": "2026-10-05T00:00:00Z", "scope": "run npm test"},
                     "command": "npm test", "isolation": "vm", "runtime": "node", "network_policy": "none",
                     "exit_code": 1, "output_location": "local", "output_excerpt": "FAIL"}
        links = [self.world.code_hit]
        executed = assessment(self.world, status="reproduced", status_reason="none", execution_evidence=execution,
                              reproduction={"method": "executed", "evidence_links": links, "public_steps": None})
        executed["readiness_checks"]["reproduction_evidence"] = {"result": "confirmed", "evidence_links": links,
                                                                 "note": "SYNTHETIC"}
        cases = {"execution evidence": (executed, "execution_evidence"),
                 "private reference": (assessment(self.world, private_evidence_reference="private-note-1"),
                                       "private_evidence_reference")}
        for label, (item, reason) in cases.items():
            with self.subTest(label):
                code, items, _ = self.strict(document(self.world, item))
                self.assertEqual(1, code)
                self.assert_status(items, {"A4-assess-rules": "FAIL", "A3-record-gate": "PASS"})
                self.assertIn(reason, items["A4-assess-rules"]["detail"])

    def test_wrong_types_inside_fail_with_a_verdict_not_a_crash(self):
        edits = {
            "pattern id list": lambda item: item.update(pattern_id=["x"]),
            "readiness link object": lambda item: item["readiness_checks"]["present_on_head"].update(
                evidence_links=[{"x": 1}]),
            "reproduction link object": lambda item: item["reproduction"].update(evidence_links=[{"y": 2}]),
            "matched item list": lambda item: item["duplicate_verdict"].update(matched_items=[["a"]]),
            "policy sha list": lambda item: item["policy_checks"]["contributing"].update(found=True, sha256=["x"],
                                                                                       source="s"),
            "policy source list": lambda item: item["policy_checks"]["contributing"].update(
                found=True, source=["x"], sha256=CONTRIBUTING_SHA),
        }
        for label, edit in edits.items():
            with self.subTest(label):
                item = assessment(self.world)
                edit(item)
                code, items, doc = self.strict(document(self.world, item))
                self.assertEqual((1, "invalid"), (code, doc["result_class"]))
                failed = [key for key, value in items.items() if value["status"] in ("FAIL", "BLOCKED")]
                self.assertTrue(failed)

    def test_the_hand_reviewed_policy_must_be_the_file_the_input_held(self):
        _, inputs = self.inputs()          # built without --policy-evidence
        code, items, _ = self.strict(document(self.world), inputs=inputs, policy_evidence=True)
        self.assertEqual(1, code)
        self.assert_status(items, {"C2-policy-evidence": "FAIL"})

    def test_another_discovery_sha_is_refused(self):
        code, items, _ = self.strict(document(self.world, discovery_sha="0" * 64))
        self.assertEqual(1, code)
        self.assert_status(items, {"A1-discovery-binding": "FAIL", "A3-record-gate": "FAIL"})
        changed = self.root / "changed-discovery.json"
        changed.write_bytes(self.world.discovery.read_bytes() + b"\n")
        code, items, _ = self.strict(document(self.world), discovery=changed)
        self.assertEqual(1, code)
        self.assert_status(items, {"C1-discovery": "FAIL"})

    def test_schema_and_unit_violations_fail_r3_and_block_the_rest(self):
        missing = assessment(self.world)
        del missing["impact"]
        extra = dict(assessment(self.world), confidence="high")
        cases = {
            "missing field": document(self.world, missing),
            "unsupported field": document(self.world, extra),
            "another combination": document(self.world, assessment(self.world, repository=fx.OTHER_TARGET)),
            "no assessment": dict(document(self.world), assessments=[]),
            "top-level key": dict(document(self.world), records=[]),
            "over the cap": document(self.world, *[assessment(self.world, locus="src/retry.py f%d" % n) for n in range(11)]),
        }
        for label, answer in cases.items():
            with self.subTest(label):
                code, items, doc = self.strict(answer)
                self.assertEqual((1, "invalid"), (code, doc["result_class"]))
                self.assert_status(items, {"R3-report-schema": "FAIL", "A3-record-gate": "BLOCKED"})

    def test_the_common_worker_contract_still_applies(self):
        code, items, _ = self.strict(document(self.world), manifest_edit=lambda m: m.update(prompt_sha256="0" * 64))
        self.assertEqual(1, code)
        self.assert_status(items, {"P4-prompt-binding": "FAIL"})
        code, items, _ = self.strict("```json\n{}\n```")
        self.assert_status(items, {"R2-report-json": "FAIL"})


class S4ReadyWithoutEvidence(AssessCase):
    """S4: a ready claim without the readiness evidence the contract needs is refused by the record gate."""

    def test_ready_without_readiness_evidence(self):
        item = ready_assessment(self.world, readiness_checks=readiness())
        collected = self.root / "collected.json"
        collected.write_text(json.dumps(document(self.world, item)), encoding="utf-8")
        locus = self.world.locus_search(collected, self.root / "locus-search.json")
        code, items, _ = self.strict(document(self.world, item), policy_evidence=True, locus_search=locus)
        self.assertEqual(1, code)
        self.assert_status(items, {"R5-input-scope": "PASS", "A2-policy-bytes": "PASS", "A3-record-gate": "FAIL"})
        self.assertIn("readiness_checks.present_on_head", items["A3-record-gate"]["detail"])

    def test_ready_with_unreviewed_policies_is_refused_191(self):
        """#191 (issue-ready with no policy observed) does not reproduce here: the gate refuses found null."""
        item = ready_assessment(self.world, policy_checks=unreviewed_policies())
        collected = self.root / "collected.json"
        collected.write_text(json.dumps(document(self.world, item)), encoding="utf-8")
        locus = self.world.locus_search(collected, self.root / "locus-search.json")
        code, items, _ = self.strict(document(self.world, item), locus_search=locus)
        self.assertEqual(1, code)
        self.assert_status(items, {"A2-policy-bytes": "PASS", "A3-record-gate": "FAIL"})
        for key in vc.REVIEWABLE_POLICY_KEYS:
            self.assertIn("policy_checks.%s.found: policy not reviewed" % key, items["A3-record-gate"]["detail"])

    def test_ready_absence_without_hand_reviewed_evidence_is_refused(self):
        item = ready_assessment(self.world)
        collected = self.root / "collected.json"
        collected.write_text(json.dumps(document(self.world, item)), encoding="utf-8")
        locus = self.world.locus_search(collected, self.root / "locus-search.json")
        code, items, _ = self.strict(document(self.world, item), locus_search=locus)
        self.assertEqual(1, code)
        self.assertIn("absence not proven by policy evidence", items["A3-record-gate"]["detail"])


class AssessPrompt(unittest.TestCase):
    def test_the_prompt_states_the_contract_and_its_copy_rules(self):
        text = (HERE / "prompts" / "assess.md").read_text(encoding="utf-8")
        for needle in ("비활성 데이터", "`policy-review`", "`unverified`", "`insufficient-evidence`", "`policy-unknown`",
                       "허용 근거 링크", "정책 키별 허용 값", "`found`", "`null`", "`program_rules`", "discovery_sha256",
                       "`verified_at`", "코드펜스", "`\\\"`", "locus"):
            self.assertIn(needle, text, needle)
        for field in sorted(vc.ASSESSMENT_FIELDS):
            self.assertIn('"%s"' % field, text, field)
        for key in vc.READINESS_KEYS + vc.POLICY_CHECK_KEYS:
            self.assertIn('"%s"' % key, text, key)
        for status, reasons in vc.STATUS_REASON_TABLE.items():
            self.assertIn("`%s`" % status, text, status)
            for reason in reasons:
                self.assertIn(reason, text, reason)


EVIDENCE_R6 = machine.VM_LOGS / "a78-period-analysis-eslint-r6-20261005T043448Z"
EVIDENCE_DISCOVERY = machine.VM_LOGS / "a78-candidate-discovery-20261005T052747Z"


@unittest.skipUnless((EVIDENCE_R6 / "analysis.json").is_file() and EVIDENCE_DISCOVERY.is_dir(),
                     "the r6 analysis and the 2026-10-05 discover are only on the machine that made them")
class RealDiscoverySizes(unittest.TestCase):
    """The 2026-10-05 discover outputs (read only) with the r6 analysis they read: each fits one assess call."""

    def test_each_discovery_of_the_three_targets_fits_one_call(self):
        with tempfile.TemporaryDirectory(prefix="e2e-assess-real-") as tmp:
            for directory in sorted(EVIDENCE_DISCOVERY.glob("D*-*")):
                discovery = directory / "unpacked" / "discovery.json"
                with self.subTest(directory.name):
                    out = Path(tmp) / directory.name
                    code, summary, stderr = stage("assess-inputs", "--analysis", str(EVIDENCE_R6 / "analysis.json"),
                                                  "--discovery", str(discovery), "--out", str(out))
                    self.assertEqual(0, code, stderr)
                    size = summary["calls"][0]["bytes"]
                    self.assertLessEqual(size, PROMPT_CAP)
                    print("\n%s: discovery %d B, %d combination(s) -> assess input %d B, sha256 %s"
                          % (directory.name, discovery.stat().st_size, len(summary["combinations"]), size,
                             summary["calls"][0]["sha256"]))


if __name__ == "__main__":
    unittest.main()
