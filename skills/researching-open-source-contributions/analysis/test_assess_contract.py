"""One source for the field types of a candidate assessment (#266, defect 1) and the static reads of a discovery.

The verifier's ``record`` gate checks every assessment field it records; the 2026-10-05 second assess answer wrote
``reproduction.public_steps`` as one string, passed the strict R3 and failed only at the record gate (A3). Now
assess_contract.py holds the field types once: the assess prompt states them, R3 checks them, and the sync test
below runs the verifier's own checks (read only, never edited) over every field with a value of every JSON type and
requires the same verdict. assess-inputs also reads the discovery's top-level ``static_reads`` (#266, defect 2):
static reads of a repository without a discovery record are not assessable (the record gate refuses such a
combination) and stay in the report only. No model, VM, token or network is used.

  /opt/homebrew/bin/python3 -B -m unittest skills/researching-open-source-contributions/analysis/test_assess_contract.py
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_assess as base  # noqa: E402  (installs the offline guards first)

fx, vc, stage, stage_inputs, verify_e2e_result = base.fx, base.vc, base.stage, base.stage_inputs, base.verify_e2e_result
import assess_contract  # noqa: E402
import machine_settings as machine  # noqa: E402

OBSERVED_AT = "2026-10-05T00:00:00Z"
HEAD = "a" * 40
RECORD = {"pattern_id": "PAT-001", "repository": "acme/widget", "repository_node_id": "R_1", "head_sha": HEAD,
          "observed_at": OBSERVED_AT, "repository_checks": {}, "duplicate_search": {}, "policy_files": [],
          "evidence_status": "static"}


def baseline() -> dict:
    """An unverified assessment the verifier's shape and record checks accept, with its optional fields."""
    item = vc._missing_assessment(RECORD)
    item.update(verified_at=OBSERVED_AT, verified_base_sha=HEAD)
    return item


def executed_baseline() -> dict:
    """The same with execution evidence, so that every execution field is present."""
    item = baseline()
    item["execution_evidence"] = {
        "approval": {"granted_by": "user", "granted_at": OBSERVED_AT, "scope": "run: npm test"},
        "command": "npm test", "isolation": "vm", "runtime": "node 22", "network_policy": "none", "exit_code": 0,
        "output_location": "out.log", "output_excerpt": None}
    item["reproduction"] = {"method": "executed", "evidence_links": [], "public_steps": None}
    return item


def verifier_labels(item: dict) -> list:
    """The paths the verifier's own checks name for this assessment (shape check, then the record gate)."""
    labels = [label.split(":", 1)[0][len("a."):] for label in vc._assessment_shape_violations(item, "a")]
    if labels:                     # record reads the shape first and refuses before its gate (_read_assessment)
        return labels
    return [label.split(":", 1)[0] for label in vc._assessment_gate_violations(item, RECORD, None)]


def related(label: str, path: str) -> bool:
    return label == path or path.startswith(label + ".") or label.startswith(path + ".")


def verifier_refuses(item: dict, path: str) -> bool:
    return any(related(label, path) for label in verifier_labels(item))


def contract_refuses(item: dict, path: str) -> bool:
    return any(related(problem.split(":", 1)[0][len("a."):], path)
               for problem in assess_contract.type_problems(item, "a", vc))


def get(item: dict, path: str):
    for part in path.split("."):
        item = item[part]
    return item


def put(item: dict, path: str, value) -> dict:
    item = deepcopy(item)
    parts = path.split(".")
    parent = item
    for part in parts[:-1]:
        parent = parent[part]
    parent[parts[-1]] = value
    return item


def values_of_every_type(current, pattern_sample):
    """One value of each JSON type a field could be given: the field's own value where it is of that type (so a
    value rule such as an enum or a pattern still holds), else a plain one."""
    string = current if isinstance(current, str) else (pattern_sample or "x")
    strings = current if isinstance(current, list) and all(isinstance(v, str) for v in current) else ["x"]
    return {"string": string, "integer": 7, "boolean": True, "null": None, "string array": strings,
            "number array": [1], "object": {}}



class SyncWithTheVerifier(unittest.TestCase):
    """S2: the contract and the verifier refuse the same JSON types for every field the verifier checks."""

    def test_the_baselines_pass_both(self):
        for item in (baseline(), executed_baseline()):
            self.assertEqual([], verifier_labels(item))
            self.assertEqual([], assess_contract.type_problems(item, "a", vc))

    def test_every_field_of_every_type_gets_the_verifiers_verdict(self):
        rows = assess_contract.field_types(vc)
        checked = 0
        for path, kind, pattern in rows:
            for item in (baseline(), executed_baseline()):
                try:
                    current = get(item, path)
                except (KeyError, TypeError):
                    continue
                for label, value in values_of_every_type(current, assess_contract.PATTERN_SAMPLES.get(pattern)).items():
                    mutated = put(item, path, value)
                    if (path, label) in assess_contract.CROSS_FIELD and item["reproduction"]["method"] == "executed":
                        self.assertTrue(verifier_refuses(mutated, path))          # the rule the table leaves out
                        continue
                    with self.subTest(path=path, value=label):
                        self.assertEqual(verifier_refuses(mutated, path), contract_refuses(mutated, path),
                                         (verifier_labels(mutated), assess_contract.type_problems(mutated, "a", vc)))
                    checked += 1
        self.assertGreater(checked, 500)

    def test_every_field_the_verifier_checks_is_in_the_contract(self):
        """Every leaf of the baselines that the verifier refuses some type for is a contract row."""
        paths = {path for path, _, _ in assess_contract.field_types(vc)}
        for item in (baseline(), executed_baseline()):
            def leaves(value, prefix=""):
                if isinstance(value, dict):
                    for key, child in value.items():
                        yield from leaves(child, prefix + key + ".")
                yield prefix.rstrip(".")
            for path in leaves(item):
                if not path:
                    continue
                refused = any(verifier_refuses(put(item, path, value), path)
                              for value in values_of_every_type(get(item, path), None).values())
                if refused:
                    self.assertIn(path, paths)

    def test_the_closed_objects_refuse_a_missing_or_an_extra_key_as_the_verifier_does(self):
        for path in assess_contract.closed_objects(vc):
            for item in (baseline(), executed_baseline()):
                try:
                    current = get(item, path)
                except (KeyError, TypeError):
                    continue
                if not isinstance(current, dict):
                    continue
                for label, value in (("extra key", dict(current, extra=1)),
                                     ("missing key", {k: v for k, v in list(current.items())[1:]})):
                    mutated = put(item, path, value)
                    with self.subTest(path=path, change=label):
                        self.assertTrue(verifier_refuses(mutated, path))
                        self.assertTrue(contract_refuses(mutated, path))


class PromptAndStrictCheck(base.AssessCase):
    def test_s2_the_prompt_states_every_field_type_of_the_contract(self):
        _, inputs = self.inputs()
        text = (inputs / "assess" / "task.txt").read_text(encoding="utf-8")
        self.assertNotIn("{{", text)
        seen = set()
        for path, kind, _ in assess_contract.field_types(vc):
            parts = path.split(".")
            if parts[0] in ("readiness_checks", "policy_checks") and len(parts) > 1:
                parts[1] = "<키>"
            path = ".".join(parts)
            if path in seen:            # one line per folded key path (program_rules: its note)
                continue
            seen.add(path)
            with self.subTest(path):
                self.assertIn("`%s`: %s" % (path, assess_contract.KIND_KO[kind]), text)
        self.assertIn('"public_steps": ["', text)                     # a string array example

    def test_a_string_public_steps_fails_r3_with_its_type(self):
        item = base.assessment(self.world, reproduction={"method": "static", "evidence_links": [self.world.code_hit],
                                                         "public_steps": "SYNTHETIC: read the locus."})
        code, items, doc = self.strict(base.document(self.world, item))
        self.assertEqual((1, "invalid"), (code, doc["result_class"]))
        self.assert_status(items, {"R3-report-schema": "FAIL", "A3-record-gate": "BLOCKED"})
        self.assertIn("assessments[0].reproduction.public_steps: expected string array or null (got string)",
                      items["R3-report-schema"]["detail"])
        fixed = deepcopy(item)
        fixed["reproduction"]["public_steps"] = ["SYNTHETIC: read the locus."]
        code, items, _ = self.strict(base.document(self.world, fixed))
        self.assert_status(items, {"R3-report-schema": "PASS"})


EVIDENCE_R2 = machine.VM_LOGS / "a78-candidate-discovery-r2-20261005T140504Z"


@unittest.skipUnless((EVIDENCE_R2 / "assess-ledger/assess/result/report.md").is_file(),
                     "the 2026-10-05 second assess call is only on the machine that made it")
class SecondAssessAnswer(unittest.TestCase):
    """S1: the second real assess answer (not adopted; the evidence is only read) now fails R3 for the type."""

    def test_the_string_public_steps_fail_r3_not_only_the_record_gate(self):
        call = EVIDENCE_R2 / "assess-ledger/assess"
        argv = ["--run-dir", str(call), "--discovery", str(EVIDENCE_R2 / "D1-eslint-json/unpacked/discovery.json"),
                "--prompt", str(EVIDENCE_R2 / "in-assess/assess/task.txt"), "--answer-kind", "assess",
                "--input-scope", str(EVIDENCE_R2 / "in-assess/assess.scope.json")]
        code, stdout, stderr = fx.workflow.run_main(verify_e2e_result.main, argv)
        doc = json.loads(stdout)
        items = {item["id"]: item for item in doc["items"]}
        self.assertEqual((1, "invalid"), (code, doc["result_class"]))
        self.assertEqual("FAIL", items["R3-report-schema"]["status"])
        self.assertEqual("BLOCKED", items["A3-record-gate"]["status"])
        detail = items["R3-report-schema"]["detail"]
        for index in range(4):
            self.assertIn("assessments[%d].reproduction.public_steps: expected string array or null (got string)"
                          % index, detail)
        self.assertEqual(4, detail.count("expected"))          # nothing else in that answer is of a wrong type
        recorded = json.loads((call / "verdict.json").read_text(encoding="utf-8"))
        self.assertEqual("PASS", {i["id"]: i for i in recorded["items"]}["R3-report-schema"]["status"])  # then


class StaticReads(base.AssessCase):
    """assess-inputs and the discovery's top-level static_reads (#269 shape)."""

    def discovery_with(self, entries, records=None, failed=()):
        document = fx.read_json(self.world.discovery)
        if records is not None:
            document["records"] = records
        document["failed_scopes"] = document["failed_scopes"] + list(failed)
        document["static_reads"] = entries
        path = self.root / ("discovery-%d.json" % len(list(self.root.glob("discovery-*.json"))))
        fx.write_json(path, document)
        return path

    def entry(self, repository, hits=1, head="b" * 40):
        static = vc._empty_rest_static(head, ["docs/rules"])
        static.update(available=True, complete=True,
                      hits=[{"path": "docs/rules/x.md", "line": 3, "kind": "options-heading-missing"}] * hits)
        return {"repository": repository, "head_sha": head, "static_search": static}

    def record_entry(self):
        record = self.world.record
        static = deepcopy(record.get("static_search")) or vc._empty_rest_static(record["head_sha"], [])
        return {"repository": record["repository"], "head_sha": record["head_sha"], "static_search": static}

    def assess(self, discovery, name, code=0):
        got, summary, stderr = stage("assess-inputs", "--analysis", str(self.world.analysis), "--discovery",
                                     str(discovery), "--out", str(self.root / name))
        self.assertEqual(code, got, stderr)
        return summary, stderr

    def test_the_static_reads_of_a_recorded_repository_must_be_its_records_copy(self):
        record = deepcopy(self.world.record)
        record["static_search"] = self.record_entry()["static_search"]
        summary, _ = self.assess(self.discovery_with([self.record_entry()], records=[record]), "ok")
        self.assertEqual([], summary["static_reads_not_assessable"])
        other = self.record_entry()
        other["static_search"] = dict(other["static_search"], skipped_files=99)
        _, stderr = self.assess(self.discovery_with([other], records=[record]), "bad", code=2)
        self.assertIn("static_reads", stderr)
        self.assertFalse((self.root / "bad").exists())

    def test_static_hits_of_a_repository_without_a_record_are_reported_not_assessed(self):
        record = deepcopy(self.world.record)
        record["static_search"] = self.record_entry()["static_search"]
        failed = [{"pattern_id": fx.PATTERN_ID, "repository": "acme/other", "reason": "budget-exhausted"}]
        discovery = self.discovery_with([self.record_entry(), self.entry("acme/other", hits=3)], records=[record],
                                        failed=failed)
        summary, _ = self.assess(discovery, "ok")
        self.assertEqual([{"repository": "acme/other", "head_sha": "b" * 40, "hits": 3, "files": 0,
                           "error": None, "reason": "no discovery record: the record gate refuses its combinations"}],
                         summary["static_reads_not_assessable"])
        self.assertEqual([{"pattern_id": fx.PATTERN_ID, "repository": fx.REPO}], summary["combinations"])
        text = (self.root / "ok" / "assess" / "task.txt").read_text(encoding="utf-8")
        self.assertNotIn("b" * 40, text)                       # nothing of acme/other's reads is in the prompt
        scope = json.loads((self.root / "ok" / "assess.scope.json").read_text(encoding="utf-8"))
        self.assertNotIn("acme/other", json.dumps(scope["combinations"]))

    def test_static_hits_alone_are_refused_and_named_for_the_report(self):
        failed = [{"pattern_id": fx.PATTERN_ID, "repository": fx.REPO, "reason": "budget-exhausted"}]
        discovery = self.discovery_with([self.entry(fx.REPO, hits=30)], records=[], failed=failed)
        _, stderr = self.assess(discovery, "none", code=2)
        self.assertIn("no discovery record to assess", stderr)
        self.assertIn("static_reads of %s (30 hits) stay in the discovery for the report" % fx.REPO, stderr)
        self.assertFalse((self.root / "none").exists())


if __name__ == "__main__":
    unittest.main()
