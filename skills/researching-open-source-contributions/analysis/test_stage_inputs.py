"""Local tests for stage_inputs.py over the workflow's synthetic offline fixture.

Every answer below is a hand-written stand-in for a model call. Nothing here
starts a model, opens the network, or reads a real corpus or credential: the
fixture blocks sockets and process spawning on import.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_stage_inputs.py
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# Kept before the fixture blocks process spawning: only the FIFO test uses it,
# to read in a separate process that a timeout can stop.
_REAL_EXECUTE_CHILD = subprocess.Popen._execute_child

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "dot_agents/skills/researching-open-source-contributions/tests"))
sys.path.insert(0, str(HERE))

import offline_fixture as fx  # noqa: E402  (installs the offline guards first)
import stage_inputs  # noqa: E402

LENSES = ("blind", "correctness", "maintenance", "review", "structure")
GENERATED_AT = "2026-09-28T00:00:00Z"

# The stage output limits the runs before #248 were made and adopted under (2026-09-28, the 13 PR run, eslint r2-r6):
# candidates up to max_patterns 5, verify answers up to 6,144 bytes, and no size limit on an extract answer or a
# candidate. Their evidence is reproduced byte for byte under these; under run_limits.json's own limits their larger
# answers are refused (README 3.1g).
EARLIER_LIMITS = {"MAX_CANDIDATES": 5, "VERIFY_ANSWER_MAX_BYTES": 6144, "EXTRACT_ANSWER_MAX_BYTES": None,
                  "CANDIDATE_MAX_BYTES": None}


# Stage output limits small enough for the fixture's prompts under a cap patched down to a few kilobytes.
SMALL_LIMITS = {"EXTRACT_ANSWER_MAX_BYTES": 1024, "CANDIDATE_MAX_BYTES": 256, "VERIFY_ANSWER_MAX_BYTES": 1024,
                "MAX_CANDIDATES": 1}


def use_limits(test, limits: dict) -> None:
    """Put other stage output limits in force for the rest of the test."""
    for name, value in limits.items():
        patcher = mock.patch.object(stage_inputs, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


def collect(directory: Path) -> dict:
    """The production collector over the fixture world, with the real request's cap and budget."""
    directory.mkdir()
    routes, interval = fx.collector_routes()
    client = fx.collector.GhApiClient(api_version=fx.API_VERSION, budget=fx.collector.RequestBudget(60),
                                      sleeper=fx.collector_tests.FakeSleep(), clock=lambda: fx.EPOCH,
                                      opener=fx.CollectorOpener(routes), token_provider=lambda: None)
    paths = {"corpus": directory / "corpus.json", "manifest": directory / "manifest.json",
             "inventory": directory / "inventory.md"}
    fx.collector.collect(client, [fx.REPO], interval, outcome="all", max_per_repository=3, request_budget=60,
                         output=paths["corpus"], manifest_output=paths["manifest"],
                         markdown_output=paths["inventory"], captured_at=fx.CAPTURED_AT, api_version=fx.API_VERSION)
    return paths


def entry_budget_request(source: Path, root: Path) -> Path:
    """An evidence run's request with its target read budgets lowered to the candidate-reads entry limit (#82).

    The earlier runs asked for discover 100; init now refuses a target budget over the entry's 60
    (oss_workflow.CANDIDATE_READS_MAX_REQUEST_BUDGET). The target budgets reach no stage input, so the inputs the
    tests compare stay the evidence's."""
    request = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(request.get("target"), dict):
        for key in ("discover_request_budget", "locus_request_budget"):
            request["target"][key] = min(request["target"][key], fx.workflow.CANDIDATE_READS_MAX_REQUEST_BUDGET)
    path = root / "entry-budget-request.json"
    path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    return path


def workflow(*argv) -> tuple[int, str, str]:
    return fx.workflow.run_main(fx.workflow.main, list(argv))


def stage(*argv) -> tuple[int, dict, str]:
    code, stdout, stderr = fx.workflow.run_main(stage_inputs.main, list(argv))
    return code, (json.loads(stdout) if code == 0 else None), stderr


class StagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        paths = collect(self.root / "collected")
        request = fx.base_request(target=None)
        request["mode"] = "offline-fixture"
        request["source"]["max_per_repository"] = 3
        request["source"]["request_budget"] = 60
        request["budget"]["total_api_requests"] = 60
        (self.root / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.run = str(self.root / "run")
        for argv in (("init", "--request", str(self.root / "request.json")),
                     ("adopt-collection", "--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"]),
                      "--inventory", str(paths["inventory"])),
                     ("digest",)):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)
        self.context = stage_inputs.Context(self.run)
        self.corpus = self.context.corpus
        self.key = {section["pr_id"]: key for key, section in self.context.keymap["sections"].items()}
        self.pr = {record["pull_request"]["number"]: record["pr_id"] for record in self.corpus["records"]}
        self.url = {record["pr_id"]: record["pull_request"]["url"] for record in self.corpus["records"]}
        self.answers = self.root / "answers"
        self.answers.mkdir()

    # -- stand-in answers ----------------------------------------------------

    def write(self, name, document) -> str:
        path = self.answers / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def evidence(self, number, role="supports"):
        pr_id = self.pr[number]
        return {"pr": self.key[pr_id], "role": role, "quote": fx.PULLS[number]["quote"], "claim": "synthetic claim"}

    def extract_results(self, *, bad_quote=False):
        paths = []
        for lens in LENSES:
            answer = {"lens": lens, "patterns": [], "observations": [], "rejected": []}
            if lens == "blind":
                answer["patterns"].append({
                    "title": "jittered backoff", "description": fx.SYNTHETIC, "kind": "convention",
                    "evidence": [self.evidence(101), self.evidence(102)], "applicability": [], "counterconditions": [],
                    "search_clues": [fx.PATTERN_CLUE], "expected_tests": [], "maintainer_judgment_required": [],
                    "confidence": "low", "confidence_reason": fx.SYNTHETIC})
            if lens == "review":
                quote = fx.PULLS[101]["quote"] + (" (reworded)" if bad_quote else "")
                answer["observations"].append({"pr": self.key[self.pr[101]], "note": fx.SYNTHETIC, "quote": quote})
            paths.append(self.write("extract-%s.result.json" % lens, answer))
        return paths

    def synthesis(self, *, candidates=True):
        items = [{"id": "C1", "statement": fx.SYNTHETIC + " jittered backoff", "found_by": "blind",
                  "cited_prs": [self.key[self.pr[101]], self.key[self.pr[102]]]}] if candidates else []
        return self.write("synthesis.result.json", {"candidates": items, "dropped": []})

    def verify_result(self):
        checks = [dict(pr=self.key[self.pr[n]], holds=True, quote=fx.PULLS[n]["quote"], reason="synthetic") for n in (101, 102)]
        return self.write("verify-C1.result.json", {
            "id": "C1", "verdict": "supported", "evidence_checks": checks, "counterexamples": [], "distinct_prs": 2,
            "distinct_repos": 1, "actionable": True, "actionable_reason": fx.SYNTHETIC, "confidence": "low",
            "revised_statement": fx.SYNTHETIC + " pattern: retry loops use jittered exponential backoff."})

    def final_answer(self, *, with_pattern=True, **overrides):
        records = {}
        for number, pr_id in self.pr.items():
            url = self.url[pr_id]
            records[pr_id] = {
                "change_summary": {"value": fx.SYNTHETIC + " change", "basis": "fact", "evidence_links": [url]},
                "motivation": {"value": None, "basis": "unknown", "evidence_links": []},
                "review_judgment": {"value": fx.SYNTHETIC + " approved", "basis": "fact", "evidence_links": [url]},
                "closure_reason": {"value": None, "basis": "unknown", "evidence_links": []},
                "test_evidence": [],
                "pattern_ids": ["PAT-001"] if with_pattern else [],
                "evidence_links": [url],
                "provenance_mode": "independent-reimplementation",
                "confidence": {"level": "low", "evidence": [fx.SYNTHETIC], "limitations": [fx.SYNTHETIC_LIMITATION]},
            }
        patterns = []
        if with_pattern:
            patterns.append({
                "pattern_id": "PAT-001", "description": fx.SYNTHETIC + " pattern: retry loops use jittered exponential backoff.",
                "evidence_pr_ids": [self.pr[101], self.pr[102]], "applicability": [fx.SYNTHETIC],
                "counterconditions": [fx.SYNTHETIC], "search_clues": [fx.PATTERN_CLUE], "expected_tests": [fx.SYNTHETIC],
                "maintainer_judgment_required": [fx.SYNTHETIC], "provenance_mode": "independent-reimplementation",
                "confidence": {"level": "low", "evidence": ['%s "%s"' % (self.pr[n], fx.PULLS[n]["quote"]) for n in (101, 102)],
                               "limitations": [fx.SYNTHETIC_LIMITATION]}})
        answer = {"records": records, "patterns": patterns,
                  "extraction_ledger": fx.SYNTHETIC + " digest in 1 shard; lenses all five; executor test stand-in",
                  "limitations": [fx.SYNTHETIC_LIMITATION]}
        answer.update(overrides)
        return answer

    def extract_args(self):
        return [a for path in self.extract_results() for a in ("--extract-result", path)]

    def run_stages(self, *, candidates=True):
        code, extract, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract"))
        self.assertEqual(code, 0, stderr)
        results = self.extract_results()
        args = [a for path in results for a in ("--extract-result", path)]
        code, synthesis_stage, stderr = stage("synthesis-input", "--run-dir", self.run, *args, "--out", str(self.root / "in-synthesis"))
        self.assertEqual(code, 0, stderr)
        synthesis = self.synthesis(candidates=candidates)
        code, verify_stage, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *args,
                                           "--out", str(self.root / "in-verify"))
        self.assertEqual(code, 0, stderr)
        verify = [self.verify_result()] if candidates else []
        code, final_stage, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *args,
                                          *[a for path in verify for a in ("--verify-result", path)],
                                          "--out", str(self.root / "in-final"))
        self.assertEqual(code, 0, stderr)
        return results, verify, (extract, synthesis_stage, verify_stage, final_stage)

    def assemble(self, answer, name="analysis.json"):
        return stage("assemble", "--run-dir", self.run, "--final", self.write("final.result.json", answer),
                     "--generated-at", GENERATED_AT, "--out", str(self.root / name))

    # -- tests ---------------------------------------------------------------

    def test_full_path_with_a_pattern_ends_analysis_only(self):
        results, verify, stages = self.run_stages()
        extract, synthesis_stage, verify_stage, final_stage = stages
        self.assertEqual([call["input_dir"] for call in extract["calls"]], ["extract-" + lens for lens in LENSES])
        build_prompts = self.context.siblings.build_prompts
        rule = build_prompts.COUNT_RULE.format(patterns=5, observations=20, rejected=20) \
            + build_prompts.SIZE_RULE.format(answer_bytes=7680) + build_prompts.TARGET_RULE.format(target_bytes=6500)
        for call in extract["calls"]:
            task = (self.root / "in-extract" / call["input_dir"] / "task.txt").read_text(encoding="utf-8")
            plain = (self.context.extract_prompts / (call["input_dir"] + ".md")).read_text(encoding="utf-8")
            # The digest's prompt (written without limits) plus the count and size rules of run_limits.json and the
            # size target (#280), nothing else.
            self.assertEqual(task, plain.replace(build_prompts.COUNT_RULE_ANCHOR, build_prompts.COUNT_RULE_ANCHOR + rule))
            self.assertEqual(task.count(rule), 1)
        self.assertEqual(synthesis_stage["quotes"], {"verified": 3, "total": 3})
        self.assertEqual([call["input_dir"] for call in verify_stage["calls"]], ["verify-C1"])
        tasks = [p for p in self.root.glob("in-*/*/task.txt")]
        self.assertEqual(len(tasks), 5 + 1 + 1 + 1)
        for task in tasks:
            text = task.read_text(encoding="utf-8")
            self.assertLessEqual(len(text.encode("utf-8")), stage_inputs.PROMPT_MAX_BYTES)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(str(REPO), text)
        final_text = (self.root / "in-final" / "final" / "task.txt").read_text(encoding="utf-8")
        self.assertIn(fx.PULLS[101]["quote"], final_text)

        code, summary, stderr = self.assemble(self.final_answer())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(summary["patterns"], ["PAT-001"])
        analysis = json.loads((self.root / "analysis.json").read_text(encoding="utf-8"))
        # Output identity: every corpus field survives; only analysis fields are added.
        for original, enriched in zip(self.corpus["records"], analysis["records"]):
            self.assertEqual({k: v for k, v in enriched.items() if k not in ("analysis", "analysis_history")}, original)
            self.assertEqual(enriched["analysis"]["evidence_manifest"]["completeness"],
                             original["evidence_snapshot"]["completeness"])
        self.assertTrue(analysis["limitations"][0].startswith("pattern-extraction: "))

        extraction = [a for path in results + verify for a in ("--extraction-result", path)]
        code, stdout, stderr = workflow("adopt-analysis", "--run-dir", self.run, "--analysis", str(self.root / "analysis.json"),
                                        "--origin", "synthetic-fixture", *extraction)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "complete")
        code, report_path, stderr = workflow("report", "--run-dir", self.run)
        self.assertEqual(code, 0, stderr)
        report = Path(report_path.strip()).read_text(encoding="utf-8")
        self.assertIn("Outcome: `analysis-only`", report)
        self.assertIn("unspecified (candidate verification skipped)", report)
        code, stdout, _ = workflow("status", "--run-dir", self.run)
        status = json.loads(stdout)
        self.assertEqual({s: status["stages"][s]["status"] for s in ("discovery", "locus-search", "record")},
                         {"discovery": "skipped", "locus-search": "skipped", "record": "skipped"})

    def test_no_candidate_is_an_honest_insufficient_result(self):
        results, verify, stages = self.run_stages(candidates=False)
        self.assertEqual(stages[2]["calls"], [])
        self.assertEqual(verify, [])
        code, summary, stderr = self.assemble(self.final_answer(with_pattern=False))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(summary["patterns"], [])
        extraction = [a for path in results for a in ("--extraction-result", path)]
        code, stdout, stderr = workflow("adopt-analysis", "--run-dir", self.run, "--analysis", str(self.root / "analysis.json"),
                                        "--origin", "synthetic-fixture", *extraction)
        self.assertEqual(json.loads(stdout)["status"], "no-patterns", stderr)
        code, report_path, stderr = workflow("report", "--run-dir", self.run)
        self.assertIn("Outcome: `insufficient`", Path(report_path.strip()).read_text(encoding="utf-8"))

    def test_assemble_refuses_what_the_model_must_not_decide(self):
        self.run_stages()
        pr_id = self.pr[101]
        overriding = self.final_answer()
        overriding["records"][pr_id]["files_changed"] = ["invented.py"]
        missing = self.final_answer()
        del missing["records"][pr_id]
        stolen = self.final_answer()
        stolen["patterns"][0]["source_licenses"] = [{"pr_id": pr_id, "spdx_id": "GPL-3.0"}]
        mismatch = self.final_answer()
        mismatch["records"][pr_id]["pattern_ids"] = []
        foreign = self.final_answer()
        foreign["records"][pr_id]["change_summary"]["evidence_links"] = ["https://example.invalid/x"]
        unknown = self.final_answer()
        unknown["records"][pr_id]["closure_reason"] = {"value": "rejected", "basis": "unknown", "evidence_links": []}
        extra = self.final_answer(extra=True)
        cases = {"deterministic field": overriding, "missing record": missing, "pattern deterministic": stolen,
                 "pattern_ids mismatch": mismatch, "foreign link": foreign, "unknown with value": unknown, "extra key": extra}
        for label, answer in cases.items():
            with self.subTest(label):
                code, _, stderr = self.assemble(answer, name=label.replace(" ", "-") + ".json")
                self.assertEqual(code, 2, label)
                self.assertFalse((self.root / (label.replace(" ", "-") + ".json")).exists())

    def test_reworded_quote_fails_the_existing_validator(self):
        self.run_stages()
        answer = self.final_answer()
        answer["patterns"][0]["confidence"]["evidence"][0] = '%s "%s"' % (self.pr[101], "jittered backoff that nobody wrote here")
        code, _, stderr = self.assemble(answer)
        self.assertEqual(code, 2)
        self.assertIn("validate_corpus rejected", stderr)
        self.assertFalse((self.root / "analysis.json").exists())

    def test_a_failed_extraction_quote_is_dropped_not_repaired(self):
        stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract"))
        args = [a for path in self.extract_results(bad_quote=True) for a in ("--extract-result", path)]
        code, summary, stderr = stage("synthesis-input", "--run-dir", self.run, *args, "--out", str(self.root / "in-synthesis"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(summary["quotes"], {"verified": 2, "total": 3})
        self.assertNotIn("extract-review.result.json", summary["clean_results"])
        self.assertEqual(len(summary["clean_results"]), 4)

    def test_inputs_are_bounded_and_strict(self):
        stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract"))
        code, _, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract"))
        self.assertEqual(code, 2)
        self.assertIn("must not exist", stderr)
        results = self.extract_results()
        (self.answers / "link").mkdir()
        link = self.answers / "link" / "extract-blind.result.json"
        os.symlink(results[0], link)
        big = self.answers / "big"
        big.mkdir()
        (big / "extract-blind.result.json").write_bytes(b"{" + b" " * stage_inputs.ANSWER_MAX_BYTES + b"}")
        duplicate = self.answers / "dup"
        duplicate.mkdir()
        (duplicate / "extract-blind.result.json").write_text('{"lens":"blind","lens":"blind"}', encoding="utf-8")
        for label, replacement in (("symlink", str(link)), ("oversized", str(big / "extract-blind.result.json")),
                                   ("duplicate key", str(duplicate / "extract-blind.result.json"))):
            with self.subTest(label):
                paths = [replacement if Path(p).name == "extract-blind.result.json" else p for p in results]
                args = [a for path in paths for a in ("--extract-result", path)]
                code, _, _ = stage("synthesis-input", "--run-dir", self.run, *args, "--out", str(self.root / ("s-" + label)))
                self.assertEqual(code, 2, label)
        # Missing a lens, or a synthesis citing one PR, is refused.
        args = [a for path in results[1:] for a in ("--extract-result", path)]
        self.assertEqual(stage("synthesis-input", "--run-dir", self.run, *args, "--out", str(self.root / "s-missing"))[0], 2)
        single = self.write("single.json", {"candidates": [{"id": "C1", "statement": "x", "cited_prs": [self.key[self.pr[101]]]}],
                                            "dropped": []})
        self.assertEqual(stage("verify-inputs", "--run-dir", self.run, "--synthesis", single, *self.extract_args(), "--out", str(self.root / "v"))[0], 2)

    def test_counts_are_capped_and_refused_not_truncated(self):
        limits = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
        # 5 + 1 + synthesis_max_candidates 3 + 1 (#248) + the retry share 5 (#280)
        self.assertEqual(stage_inputs.MAX_MODEL_CALLS, 15)
        keys = [self.key[self.pr[101]], self.key[self.pr[102]]]
        many = [{"id": "C%d" % n, "statement": "x", "cited_prs": keys}
                for n in range(1, limits["synthesis_max_candidates"] + 2)]
        dropped = [{"idea": "x", "reason": "y"}] * (limits["max_rejected"] + 1)
        for label, answer in (("candidates", {"candidates": many, "dropped": []}),
                              ("dropped", {"candidates": [], "dropped": dropped})):
            with self.subTest(label):
                path = self.write("synthesis-%s.json" % label, answer)
                code, _, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", path,
                                        *self.extract_args(), "--out", str(self.root / ("v-" + label)))
                self.assertEqual(code, 2, label)
                self.assertIn("at most", stderr)
                self.assertFalse((self.root / ("v-" + label)).exists())
        at_cap = self.write("synthesis-cap.json", {"candidates": many[:-1], "dropped": dropped[:-1]})
        code, summary, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", at_cap, *self.extract_args(), "--out", str(self.root / "v-cap"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(summary["calls"]), limits["synthesis_max_candidates"])
        self.run_stages()
        answer = self.final_answer()
        extra = [dict(answer["patterns"][0], pattern_id="PAT-%03d" % n) for n in range(2, limits["max_patterns"] + 2)]
        answer["patterns"] += extra
        for record in answer["records"].values():
            record["pattern_ids"] = [p["pattern_id"] for p in answer["patterns"]]
        code, _, stderr = self.assemble(answer)
        self.assertEqual(code, 2)
        self.assertIn("at most", stderr)
        stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract-2"))
        results = self.extract_results()
        noisy = json.loads(Path(results[0]).read_text(encoding="utf-8"))
        noisy["rejected"] = [{"idea": "x", "reason": "y"}] * (limits["max_rejected"] + 1)
        (self.answers / "noisy").mkdir()
        results[0] = str(self.answers / "noisy" / Path(results[0]).name)
        Path(results[0]).write_text(json.dumps(noisy), encoding="utf-8")
        args = [a for path in results for a in ("--extract-result", path)]
        code, _, stderr = stage("synthesis-input", "--run-dir", self.run, *args, "--out", str(self.root / "s-noisy"))
        self.assertEqual(code, 2)
        self.assertIn("at most", stderr)

    def test_a_fifo_answer_is_refused_without_blocking(self):
        fifo = self.answers / "extract-blind.result.json"
        os.mkfifo(fifo)
        code = ("import sys; sys.path.insert(0, %r); import stage_inputs\n"
                "try:\n    stage_inputs.read_bounded(stage_inputs.Path(%r), 1024, 'answer')\n"
                "except stage_inputs.StageError as error:\n    print(error); sys.exit(0)\n"
                "sys.exit(1)\n") % (str(HERE), str(fifo))
        # On Linux the real _execute_child spawns through os.posix_spawn, which the guard also blocks: use fork_exec (#274)
        with mock.patch.object(subprocess.Popen, "_execute_child", _REAL_EXECUTE_CHILD), \
                mock.patch.object(subprocess, "_USE_POSIX_SPAWN", False):
            completed = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("not a regular file", completed.stdout)

    # -- final-prompt budget (B3) ---------------------------------------------

    def cap(self, value):
        patcher = mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_digest_boundary_decides_between_the_digest_path_and_the_extraction_only_path(self):
        fixed = len(stage_inputs.final_prompt_text(self.context, [], {}, stage_inputs.EMPTY_REPORT).encode("utf-8"))
        self.assertFalse(self.context.reduced)
        # One byte short: the final prompt can no longer carry the digest, so the run reduces from extraction
        # answers instead of refusing (its own, much smaller, fixed part is what is then budgeted). Its worst-case
        # plan (#248) must pass too, so the stage output limits are scaled down with the prompt cap.
        use_limits(self, SMALL_LIMITS)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", fixed + stage_inputs.FINAL_RESERVE_BYTES - 1):
            code, summary, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "over"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual("extraction-only", summary["mode"])
        self.assertNotEqual(summary["final_budget"]["fixed_bytes"], fixed)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", fixed + stage_inputs.FINAL_RESERVE_BYTES):
            code, summary, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "at"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual("digest-in-final", summary["mode"])
        self.assertEqual(summary["final_budget"]["fixed_bytes"], fixed)
        # The extraction-only final prompt has its own budget: refused before any model call when even it is over.
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", 9000), \
                mock.patch.object(stage_inputs, "FINAL_RESERVE_BYTES", 9000):
            code, _, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "none"))
        self.assertEqual(code, 2)
        self.assertIn("refused", stderr)
        self.assertFalse((self.root / "none").exists())

    def test_quotes_are_deduplicated_deterministically(self):
        paths = self.extract_results()
        report = stage_inputs.check_quotes(self.context, paths)
        compact = stage_inputs.compact_report(report)
        self.assertEqual(report["totals"], {"verified": 3, "total": 3})
        self.assertEqual([(e["pr_id"], e["quote"]) for e in compact["verified_evidence"]],
                         [(self.pr[101], fx.PULLS[101]["quote"]), (self.pr[102], fx.PULLS[102]["quote"])])
        self.assertEqual(compact, stage_inputs.compact_report(stage_inputs.check_quotes(self.context, paths)))

    def test_three_long_candidates_hit_the_budget_at_verify_inputs(self):
        keys = [self.key[self.pr[101]], self.key[self.pr[102]]]
        limit = stage_inputs.STATEMENT_MAX_CHARS
        count = stage_inputs.MAX_CANDIDATES
        # as long as candidate_max_bytes allows (a statement of 600 Hangul characters no longer fits one, #248)
        room = (stage_inputs.CANDIDATE_MAX_BYTES
                - stage_inputs.answer_bytes({"id": "C1", "statement": "", "cited_prs": keys})) // 3
        candidates = [{"id": "C%d" % n, "statement": "가" * room, "cited_prs": keys} for n in range(1, count + 1)]
        path = self.write("three.json", {"candidates": candidates, "dropped": []})
        compact = stage_inputs.compact_report(stage_inputs.check_quotes(self.context, self.extract_results()))
        base = len(stage_inputs.final_prompt_text(self.context, candidates, {}, compact).encode("utf-8"))
        floor = base + count * stage_inputs.VERIFY_FLOOR_BYTES
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", floor - 1), \
                mock.patch.object(stage_inputs, "FINAL_RESERVE_BYTES", 0):   # keep the run on the digest path
            code, _, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", path, *self.extract_args(),
                                    "--out", str(self.root / "v-over"))
        self.assertEqual(code, 2)
        self.assertIn("%d candidates" % count, stderr)
        self.assertFalse((self.root / "v-over").exists())
        code, summary, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", path, *self.extract_args(),
                                      "--out", str(self.root / "v-ok"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(summary["calls"]), count)
        self.assertEqual(summary["final_budget"]["floor_bytes"], floor)
        self.assertEqual(summary["final_budget"]["guaranteed"],
                         base + count * 2 * stage_inputs.VERIFY_ANSWER_MAX_BYTES <= stage_inputs.PROMPT_MAX_BYTES)
        over = self.write("over.json", {"candidates": [dict(candidates[0], statement="가" * (room + 1))], "dropped": []})
        code, _, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", over, *self.extract_args(),
                                "--out", str(self.root / "v-big"))
        self.assertEqual(code, 2)
        self.assertIn("candidate_max_bytes %d" % stage_inputs.CANDIDATE_MAX_BYTES, stderr)
        use_limits(self, {"CANDIDATE_MAX_BYTES": None})
        too_long = self.write("long.json", {"candidates": [dict(candidates[0], statement="가" * (limit + 1))], "dropped": []})
        code, _, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", too_long, *self.extract_args(),
                                "--out", str(self.root / "v-long"))
        self.assertEqual(code, 2)
        self.assertIn("at most %d characters" % limit, stderr)

    def test_final_prompt_overflow_after_verify_is_refused_not_cut(self):
        results, verify, _ = self.run_stages()
        synthesis = str(self.answers / "synthesis.result.json")
        args = [a for path in results for a in ("--extract-result", path)] + ["--verify-result", verify[0]]
        size = (self.root / "in-final" / "final" / "task.txt").stat().st_size
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", size - 1), \
                mock.patch.object(stage_inputs, "FINAL_RESERVE_BYTES", 0):   # keep the run on the digest path
            code, _, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *args,
                                    "--out", str(self.root / "in-final-2"))
        self.assertEqual(code, 2)
        self.assertIn("dynamic overflow", stderr)
        self.assertFalse((self.root / "in-final-2").exists())
        big = json.loads(Path(verify[0]).read_text(encoding="utf-8"))
        big["actionable_reason"] = "x" * stage_inputs.VERIFY_ANSWER_MAX_BYTES
        (self.answers / "big").mkdir()
        big_path = self.answers / "big" / "verify-C1.result.json"
        big_path.write_text(json.dumps(big), encoding="utf-8")
        code, _, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis,
                                *args[:-2], "--verify-result", str(big_path), "--out", str(self.root / "in-final-3"))
        self.assertEqual(code, 2)
        self.assertIn("verify_answer_max_bytes", stderr)



OTHER = fx.OTHER_TARGET  # a second synthetic source repository


def mixed_routes():
    """The fixture world for REPO plus the same PRs under OTHER (own identities), one routes table."""
    routes, interval = fx.collector_routes()
    other = {}
    for (path, query), (status, payload) in routes.items():
        if path == "/versions":
            continue
        text = json.dumps([path, query, payload]).replace(fx.REPO, OTHER).replace(
            fx.REPO_NODE, "R_SYNTH_FIXTURE_OTHER").replace("PR_SYNTH_FIXTURE_", "PR_SYNTH_OTHER_")
        new_path, new_query, new_payload = json.loads(text)
        other[(new_path, tuple(tuple(pair) for pair in new_query))] = (status, new_payload)
    routes.update(other)
    return routes, interval


class MixedSourceTargetTests(unittest.TestCase):
    """Two source repositories in one corpus (max_per_repository each) and a target repository."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "collected").mkdir()
        routes, interval = mixed_routes()
        client = fx.collector.GhApiClient(api_version=fx.API_VERSION, budget=fx.collector.RequestBudget(60),
                                          sleeper=fx.collector_tests.FakeSleep(), clock=lambda: fx.EPOCH,
                                          opener=fx.CollectorOpener(routes), token_provider=lambda: None)
        paths = {name: self.root / "collected" / name for name in ("corpus.json", "manifest.json", "inventory.md")}
        fx.collector.collect(client, [fx.REPO, OTHER], interval, outcome="all", max_per_repository=3,
                             request_budget=60, output=paths["corpus.json"], manifest_output=paths["manifest.json"],
                             markdown_output=paths["inventory.md"], captured_at=fx.CAPTURED_AT,
                             api_version=fx.API_VERSION)
        request = fx.base_request()
        request["mode"] = "offline-fixture"
        request["source"].update(repositories=[fx.REPO, OTHER], max_per_repository=3, request_budget=60)
        request["target"].update(repositories=[fx.REPO], max_candidates_per_repo=5, max_candidates_total=5)
        request["budget"]["total_api_requests"] = 60 + request["target"]["discover_request_budget"] \
            + request["target"]["locus_request_budget"]
        (self.root / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.run = str(self.root / "run")
        for argv in (("init", "--request", str(self.root / "request.json")),
                     ("adopt-collection", "--corpus", str(paths["corpus.json"]), "--manifest",
                      str(paths["manifest.json"]), "--inventory", str(paths["inventory.md"])),
                     ("digest",)):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)
        self.context = stage_inputs.Context(self.run)
        self.answers = self.root / "answers"
        self.answers.mkdir()
        # (repository, number) -> (pr_id, digest key, url)
        self.prs = {}
        for key, section in self.context.keymap["sections"].items():
            number = int(section["url"].rsplit("/", 1)[1])
            self.prs[(section["repository"], number)] = (section["pr_id"], key, section["url"])

    def write(self, name, document):
        path = self.answers / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run_to_final(self, *, pattern):
        code, extract, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "in-extract"))
        self.assertEqual(code, 0, stderr)
        evidence = [{"pr": self.prs[(repo, 101)][1], "role": "supports", "quote": fx.PULLS[101]["quote"], "claim": "c"}
                    for repo in (fx.REPO, OTHER)]
        results = []
        for lens in LENSES:
            answer = {"lens": lens, "patterns": [], "observations": [], "rejected": []}
            if pattern and lens == "blind":
                answer["patterns"].append({
                    "title": "t", "description": fx.SYNTHETIC, "kind": "convention", "evidence": evidence,
                    "applicability": [], "counterconditions": [], "search_clues": [fx.PATTERN_CLUE], "expected_tests": [],
                    "maintainer_judgment_required": [], "confidence": "low", "confidence_reason": fx.SYNTHETIC})
            results.append(self.write("extract-%s.result.json" % lens, answer))
        args = [a for path in results for a in ("--extract-result", path)]
        code, synthesis_stage, stderr = stage("synthesis-input", "--run-dir", self.run, *args,
                                              "--out", str(self.root / "in-synthesis"))
        self.assertEqual(code, 0, stderr)
        candidates = [{"id": "C1", "statement": fx.SYNTHETIC, "cited_prs": [e["pr"] for e in evidence]}] if pattern else []
        synthesis = self.write("synthesis.result.json", {"candidates": candidates, "dropped": []})
        code, verify_stage, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *args,
                                           "--out", str(self.root / "in-verify"))
        self.assertEqual(code, 0, stderr)
        verify = []
        if pattern:
            checks = [{"pr": e["pr"], "holds": True, "quote": e["quote"], "reason": "r"} for e in evidence]
            verify.append(self.write("verify-C1.result.json", {
                "id": "C1", "verdict": "supported", "evidence_checks": checks, "counterexamples": [], "distinct_prs": 2,
                "distinct_repos": 2, "actionable": True, "actionable_reason": "r", "confidence": "low",
                "revised_statement": fx.SYNTHETIC}))
        code, final_stage, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *args,
                                          *[a for path in verify for a in ("--verify-result", path)],
                                          "--out", str(self.root / "in-final"))
        self.assertEqual(code, 0, stderr)
        return results, verify, extract

    def final_answer(self, *, pattern):
        evidence_ids = [self.prs[(repo, 101)][0] for repo in (fx.REPO, OTHER)] if pattern else []
        records = {}
        for pr_id, _, url in self.prs.values():
            records[pr_id] = {
                "change_summary": {"value": fx.SYNTHETIC, "basis": "fact", "evidence_links": [url]},
                "motivation": {"value": None, "basis": "unknown", "evidence_links": []},
                "review_judgment": {"value": fx.SYNTHETIC, "basis": "fact", "evidence_links": [url]},
                "closure_reason": {"value": None, "basis": "unknown", "evidence_links": []},
                "test_evidence": [], "pattern_ids": ["PAT-001"] if pr_id in evidence_ids else [],
                "evidence_links": [url], "provenance_mode": "independent-reimplementation",
                "confidence": {"level": "low", "evidence": [fx.SYNTHETIC], "limitations": [fx.SYNTHETIC_LIMITATION]}}
        patterns = [{
            "pattern_id": "PAT-001", "description": fx.SYNTHETIC, "evidence_pr_ids": evidence_ids,
            "applicability": [fx.SYNTHETIC], "counterconditions": [fx.SYNTHETIC], "search_clues": [fx.PATTERN_CLUE],
            "expected_tests": [fx.SYNTHETIC], "maintainer_judgment_required": [fx.SYNTHETIC],
            "provenance_mode": "independent-reimplementation",
            "confidence": {"level": "low", "evidence": ['%s "%s"' % (pr_id, fx.PULLS[101]["quote"]) for pr_id in evidence_ids],
                           "limitations": [fx.SYNTHETIC_LIMITATION]}}] if pattern else []
        return {"records": records, "patterns": patterns, "extraction_ledger": fx.SYNTHETIC + " mixed-repo ledger",
                "limitations": [fx.SYNTHETIC_LIMITATION]}

    def finish(self, answer, results, verify):
        code, _, stderr = stage("assemble", "--run-dir", self.run, "--final", self.write("final.result.json", answer),
                                "--generated-at", GENERATED_AT, "--out", str(self.root / "analysis.json"))
        self.assertEqual(code, 0, stderr)
        extraction = [a for path in results + verify for a in ("--extraction-result", path)]
        code, stdout, stderr = workflow("adopt-analysis", "--run-dir", self.run, "--analysis",
                                        str(self.root / "analysis.json"), "--origin", "synthetic-fixture", *extraction)
        self.assertIn(code, (0, 3), stderr)
        code, report_path, stderr = workflow("report", "--run-dir", self.run)
        self.assertEqual(code, 0, stderr)
        code, stdout, _ = workflow("status", "--run-dir", self.run)
        return Path(report_path.strip()).read_text(encoding="utf-8"), json.loads(stdout)

    def test_two_sources_are_one_corpus_over_the_old_three_record_cap(self):
        records = self.context.records
        repositories = sorted({record["repository"]["full_name"] for record in records})
        self.assertEqual(repositories, sorted([fx.REPO, OTHER]))
        self.assertGreater(len(records), 3)  # the old single-repository cap of 3 would refuse this corpus
        self.assertLessEqual(len(records), 2 * self.context.request["source"]["max_per_repository"])
        self.assertEqual(len(self.context.keymap["shards"]), 1)
        self.assertEqual(len(self.context.extract_stems()), 5)

    def test_cross_repository_pattern_with_a_target_awaits_candidate_verification(self):
        results, verify, _ = self.run_to_final(pattern=True)
        report, status = self.finish(self.final_answer(pattern=True), results, verify)
        self.assertEqual(status["outcome"], "awaiting-input")
        self.assertEqual(status["stages"]["discovery"]["status"], "not-run")
        self.assertEqual(status["next"][0]["stage"], "discovery")
        self.assertEqual(status["next"][0]["kind"], "external-network-step")
        self.assertFalse(status["next"][0]["executed"])
        self.assertIn("Outcome: `awaiting-input`", report)
        self.assertNotIn("candidate verification skipped", report)

    def test_no_pattern_with_a_target_is_insufficient_not_a_candidate_verdict(self):
        results, verify, _ = self.run_to_final(pattern=False)
        report, status = self.finish(self.final_answer(pattern=False), results, verify)
        self.assertEqual(status["outcome"], "insufficient")
        self.assertEqual(status["next"], [])

    def test_the_caps_come_from_the_request_not_from_the_bundle(self):
        request = json.loads((Path(self.run) / "request.json").read_text(encoding="utf-8"))

        def check(per_repository=3, repositories=(fx.REPO,), collections=None):
            changed = json.loads(json.dumps(request))
            changed["source"].update(max_per_repository=per_repository, repositories=list(repositories))
            context = stage_inputs.Context.__new__(stage_inputs.Context)
            context.request, context.collections = changed, collections
            stage_inputs.Context._check_scope(context, self.context.records)

        count = len(self.context.records)
        two = [fx.REPO, OTHER]
        # A corpus fits a request that allows it, however large the request's caps are.
        check(per_repository=count, repositories=[fx.REPO])
        check(per_repository=count // 2, repositories=two)
        check(per_repository=50, repositories=two + ["synth-fixture-org/third"])
        # A corpus beyond what the request allows is refused, and so is a cap that is not a positive integer.
        with self.assertRaises(stage_inputs.StageError):
            check(per_repository=1, repositories=two)
        for bad in (0, -1, True, "3", None):
            with self.subTest(bad=bad), self.assertRaises(stage_inputs.StageError):
                check(per_repository=bad, repositories=two)
        # Several collection runs add up: two runs over both repositories allow twice the cap per repository.
        check(per_repository=1, repositories=two, collections=[{"repositories": two}] * 2)
        with self.assertRaises(stage_inputs.StageError):
            check(per_repository=1, repositories=two, collections=[{"repositories": two}])


def make_run(test, root: Path, digest_args=()) -> str:
    paths = collect(root / "collected")
    request = fx.base_request(target=None)
    request["source"].update(max_per_repository=3, request_budget=60)
    request["budget"]["total_api_requests"] = 60
    (root / "request.json").write_text(json.dumps(request), encoding="utf-8")
    run = str(root / "run")
    for argv in (("init", "--request", str(root / "request.json")),
                 ("adopt-collection", "--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"]),
                  "--inventory", str(paths["inventory"])),
                 ("digest", *digest_args)):
        code, _, stderr = workflow(argv[0], "--run-dir", run, *argv[1:])
        test.assertEqual(code, 0, stderr)
    return run


class ShardedRunTests(unittest.TestCase):
    """The fixture digest is about 1,700 bytes in two sections: --max-bytes 1200 makes two shards."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def sharded(self):
        return make_run(self, self.root, ("--max-bytes", "1200"))

    def test_the_call_limit_is_shards_times_lenses_plus_synthesis_verify_final_and_the_retry_share(self):
        # 5 + 1 + synthesis_max_candidates 3 + 1 (#248), and the retry share 5 (#280)
        self.assertEqual(15, stage_inputs.MAX_MODEL_CALLS)
        self.assertEqual({"shards": 1, "lenses": 5, "extract": 5, "synthesis": 1, "verify": 5, "final": 1,
                          "retry": {"per_call": 1, "per_stage": 3, "stages": {"extract": 3, "synthesis": 1, "verify": 3,
                                                                               "final": 1}, "total": 5},
                          "max_model_calls": 17}, {k: v for k, v in stage_inputs.model_call_plan(1, 5, 5).items()
                                                   if k != "formula"})
        self.assertEqual(22, stage_inputs.model_call_plan(2, 5, 5)["max_model_calls"])
        self.assertEqual(20, stage_inputs.model_call_plan(2, 5, 3)["max_model_calls"])
        plan = stage_inputs.model_call_plan(2, 5, 3)
        self.assertEqual("2 shards x 5 lenses + 1 (synthesis) + 3 (verify, at most max_candidates) + 1 (final) + 5 "
                         "(retry share) = 20", plan["formula"])

    def test_a_single_shard_run_records_the_ten_call_plan(self):
        run = make_run(self, self.root)
        code, summary, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(15, summary["max_model_calls"])        # 10 and the retry share 5 (#280); 12 under 5 candidates
        self.assertEqual(5, len(summary["calls"]))
        self.assertEqual(1, summary["call_plan"]["shards"])
        self.assertEqual(summary, json.loads((self.root / "x" / "stage.json").read_text(encoding="utf-8")))

    def test_a_sharded_run_gets_one_prompt_per_shard_and_lens_and_a_larger_limit(self):
        code, summary, stderr = stage("extract-inputs", "--run-dir", self.sharded(), "--out", str(self.root / "x"))
        self.assertEqual(code, 0, stderr)
        names = sorted(call["input_dir"] for call in summary["calls"])
        self.assertEqual(sorted("extract-%s-%02d" % (lens, n) for lens in LENSES for n in (1, 2)), names)
        # The worst-case plan (#248): every answer at the stage output limits. Ten answers of 7,680 bytes make 4
        # groups of at most 3 (the fixture's PR index is small), and their candidates fit one merge.
        plan = summary["call_plan"]
        self.assertEqual({"mode": "extraction-only", "shards": 2, "lenses": 5, "extract": 10, "synthesis": 5,
                          "synthesis_levels": [4, 1], "verify": 3, "final": 1, "max_model_calls": 24,
                          "retry": {"per_call": 1, "per_stage": 3, "stages": {"extract": 3, "synthesis": 3, "verify": 3,
                                                                               "final": 1}, "total": 5}},
                         {k: v for k, v in plan.items() if k not in ("formula", "inputs")})
        self.assertEqual(plan["max_model_calls"], plan["extract"] + plan["synthesis"] + plan["verify"] + plan["final"]
                         + plan["retry"]["total"])
        self.assertEqual({"max_candidates": 3, "shards": 2, "lenses": 5, "limits": stage_inputs.output_limits()},
                         plan["inputs"])
        self.assertEqual(24, summary["max_model_calls"])
        self.assertEqual(plan, stage("dry-plan", "--run-dir", str(self.root / "run"))[1]["call_plan"])
        for call in summary["calls"]:
            self.assertLessEqual(call["bytes"], stage_inputs.PROMPT_MAX_BYTES)
            self.assertEqual("%s.result.json" % call["input_dir"], call["result_name"])

    def test_every_extract_result_of_a_sharded_run_is_taken_by_the_synthesis_input(self):
        run = self.sharded()
        self.assertEqual(0, stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))[0])
        answers = self.root / "answers"
        answers.mkdir()
        args = []
        for lens in LENSES:
            for n in (1, 2):
                path = answers / ("extract-%s-%02d.result.json" % (lens, n))
                path.write_text(json.dumps({"lens": lens, "patterns": [], "observations": [], "rejected": []}))
                args += ["--extract-result", str(path)]
        code, summary, stderr = stage("synthesis-input", "--run-dir", run, *args, "--out", str(self.root / "s"))
        self.assertEqual(code, 0, stderr)
        missing = args[2:]
        self.assertEqual(2, stage("synthesis-input", "--run-dir", run, *missing, "--out", str(self.root / "s2"))[0])

    def test_the_extract_count_rule_comes_from_the_same_caps_the_answers_are_checked_against(self):
        run = self.sharded()
        caps = {"patterns": 3, "observations": 7, "rejected": 9}
        with mock.patch.dict(stage_inputs.EXTRACT_CAPS, caps):
            code, summary, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))
        self.assertEqual(0, code, stderr)
        self.assertEqual(10, len(summary["calls"]))
        for call in summary["calls"]:
            text = (self.root / "x" / call["input_dir"] / "task.txt").read_text(encoding="utf-8")
            self.assertIn("patterns 는 최대 3개, observations 는 최대 7개, rejected 는 최대 9개다.", text)
        self.assertEqual({"patterns": "max_patterns", "observations": "max_observations", "rejected": "max_rejected"},
                         {kind: "max_" + kind for kind in stage_inputs.EXTRACT_CAPS})
        self.assertEqual(stage_inputs.EXTRACT_CAPS, {kind: stage_inputs.LIMITS["max_" + kind] for kind in caps})

    def test_a_prompt_over_the_cap_refuses_the_stage_before_anything_is_written(self):
        run = self.sharded()
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", 5000):
            code, _, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))
        self.assertEqual(2, code)
        self.assertIn("5000", stderr)
        self.assertFalse((self.root / "x").exists())

    def test_a_final_prompt_that_carries_no_digest_is_budgeted_on_its_own_and_there_is_no_overflow_flag(self):
        run = self.sharded()
        self.assertEqual(2, stage("extract-inputs", "--run-dir", run, "--allow-final-overflow", "--out", str(self.root / "f"))[0])
        with mock.patch.object(stage_inputs, "FINAL_RESERVE_BYTES", 70000):
            code, _, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "a"))
        self.assertEqual(2, code)
        self.assertIn("the final prompt would need", stderr)
        self.assertFalse((self.root / "a").exists())
        code, summary, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "b"))
        self.assertEqual(code, 0, stderr)
        self.assertLess(summary["final_budget"]["fixed_bytes"] + summary["final_budget"]["reserve_bytes"],
                        stage_inputs.PROMPT_MAX_BYTES)


EVIDENCE_928 = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-two-repo-20260928T075531Z")


@unittest.skipUnless((EVIDENCE_928 / "analysis/in-final/final/task.txt").is_file(),
                     "the 2026-09-28 run's inputs are only on the machine that made them")
class Run928InputTests(unittest.TestCase):
    """The six pull request, one shard run of 2026-09-28: every stage input stays byte for byte what the
    isolated calls were given (the evidence kept their task.txt), however the larger-corpus path changes.

    Except the extract inputs: the bundle now states run_limits.json's count limits in them (#82). The
    analyzer's prompt without limits (the digest's) is still the evidence's byte for byte, and the bundle's
    extract inputs are pinned by sha256 below.

    And the final input: finalize.md now holds one more line, the rule that a quote keeps the source's double
    quotes and backslashes (#82; the 2026-10-05 r5 final answer copied the JSON notation \\" into a quote).
    Without that line the final input is still the evidence's byte for byte, and with it it is pinned below.

    And the search clue rule (#82, after the 2026-10-05 discover read nothing with Korean clues): the analyzer's
    extract prompt and finalize.md now ask for GitHub search strings. With those sentences taken back
    (CLUE_TEXT_CHANGES, FINAL_CLUE_CHANGES) every prompt is the earlier one byte for byte; the new bytes are pinned.

    All of that under the stage output limits the run was made under (EARLIER_LIMITS, #248). Under run_limits.json's
    own limits the extract inputs also state the answer size limit (pinned below), and two of the run's extract
    answers (blind 11,571 and review 8,740 bytes) are over it, so synthesis-input refuses them; its verify and final
    inputs are the same bytes as under the earlier limits."""

    # The analyzer's extract prompt: the search clue sentence, the output sentence and the schema placeholder.
    CLUE_TEXT_CHANGES = (
        ("search_clues 는 후보 저장소에서 GitHub 검색(`repo:<저장소> \"<단서>\"` 꼴의 이슈·PR·코드 검색)에 그대로 넣을 짧은 문자열이다: "
         "파일 경로 조각, 헤딩·코드 토큰, 도메인, 정확한 영어 문구(예: `## Options`, `further_reading`, `npm install eslint`, "
         "`kangax.github.io`). 설명 문장·한국어를 쓰지 않는다. 한 항목은 1~200자이고 큰따옴표·백슬래시·줄바꿈·앞뒤 공백이 없다. "
         "앞의 5개만 검색에 쓰인다.", "search_clues 에는 후보 저장소에서 찾을 파일 경로·코드 형태·검색어를 적는다."),
        ("quote 는 원문 그대로, search_clues 는 위 규칙의 검색 문자열로 둔다.", "quote 는 원문 그대로 둔다."),
        ('"search_clues": ["<검색 문자열>"]', '"search_clues": ["후보 저장소에서 찾을 단서"]'),
    )
    # finalize.md: the output sentence (the search clue rule is its own line, taken out whole).
    FINAL_CLUE_CHANGES = (("인용은 원문 그대로, `search_clues` 는 위 규칙의 검색 문자열로 둔다.", "인용은 원문 그대로 둔다."),)

    @staticmethod
    def taken_back(data: bytes, changes) -> bytes:
        text = data.decode("utf-8")
        for now, before in changes:
            assert text.count(now) == 1, now[:40]
            text = text.replace(now, before)
        return text.encode("utf-8")

    # sha256 of the bundle's extract inputs for this run, with the count rule of run_limits.json (5/20/20) and the
    # search clue rule.
    EXTRACT_SHA256 = {
        "blind": "c4df9fbd9635a3e229de22c2479a48d6c2d98cab3884cffbc0f68524139619ec",
        "correctness": "2a23735690ca02df3f4ce994f864f45ec089437125cec7b10cb26a91cc62a366",
        "maintenance": "47324320bdfa4bff4851e3e2d823f8201a0fec04fa46e75317a66aa93df18449",
        "review": "b0e38587d30160dcc98ef07023232cb62d54c2f9f43650b0b2b12063ec66faa5",
        "structure": "1f6684e2d8d3915f8656042f8b63507fd28d645fff4fba5f015d2f2c659f96e5",
    }
    # The same before the search clue rule (with the count rule).
    EARLIER_EXTRACT_SHA256 = {
        "blind": "b4d018f7327933fedad57152f66fb5fe57c2e308d9379ba810884f78631a3d1c",
        "correctness": "549728379842667350aa7417da60b3ee7a0f2ff199fa3d45447e4c594cbbc6e9",
        "maintenance": "341a81180c9818bc4d0fe0231ae44f5884055d72bd1fe0f45ecb2f27319a1eec",
        "review": "07136757ee204700abacbfdffe93405b58824cda12650352cec7f07abd50f2a7",
        "structure": "26c3c9e62d97ee463663e32eb6acc3f2a01d8c4aca28b44901b0efac9dae07d5",
    }

    # sha256 of the bundle's final input for this run, with finalize.md's quote rule and search clue rule.
    FINAL_SHA256 = "27f62a9de3b6fb5bb5c4dfece264d0bc780a5233669a7682740c7fc20309e2dc"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        analysis = EVIDENCE_928 / "analysis"
        unpacked = EVIDENCE_928 / "unpacked"
        self.analysis = analysis
        self.run = str(self.root / "run")
        for argv in (("init", "--request", str(entry_budget_request(analysis / "request.json", self.root))),
                     ("adopt-collection", "--corpus", str(unpacked / "corpus.json"),
                      "--manifest", str(unpacked / "collection-manifest.json"),
                      "--inventory", str(unpacked / "inventory.md")),
                     ("digest",)):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)

    def same(self, built: Path, kept: Path):
        self.assertEqual(kept.read_bytes(), built.read_bytes(), str(kept))

    def test_every_input_of_the_run_is_reproduced_byte_for_byte_under_the_limits_it_was_made_under(self):
        use_limits(self, EARLIER_LIMITS)
        answers = self.analysis / "answers"
        extract = [item for lens in LENSES for item in ("--extract-result", str(answers / ("extract-%s.result.json" % lens)))]
        code, summary, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "extract"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(17, summary["max_model_calls"])        # 12 and the retry share 5 (#280)
        digest_prompts = stage_inputs.Context(self.run).extract_prompts
        for lens in LENSES:
            kept = self.analysis / "in-extract-2" / ("extract-" + lens) / "task.txt"
            self.assertEqual(kept.read_bytes(), self.taken_back(
                (digest_prompts / ("extract-" + lens + ".md")).read_bytes(), self.CLUE_TEXT_CHANGES), lens)
            built = (self.root / "extract" / ("extract-" + lens) / "task.txt").read_bytes()
            self.assertNotEqual(kept.read_bytes(), built)
            self.assertEqual(self.EXTRACT_SHA256[lens], hashlib.sha256(built).hexdigest(), lens)
            self.assertEqual(self.EARLIER_EXTRACT_SHA256[lens],
                             hashlib.sha256(self.taken_back(built, self.CLUE_TEXT_CHANGES)).hexdigest(), lens)
        code, _, stderr = stage("synthesis-input", "--run-dir", self.run, *extract, "--out", str(self.root / "synthesis"))
        self.assertEqual(code, 0, stderr)
        self.same(self.root / "synthesis/synthesis/task.txt", self.analysis / "in-synthesis/synthesis/task.txt")
        synthesis = str(answers / "synthesis.result.json")
        code, summary, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *extract,
                                      "--out", str(self.root / "verify"))
        self.assertEqual(code, 0, stderr)
        for call in summary["calls"]:
            self.same(self.root / "verify" / call["input_dir"] / "task.txt",
                      self.analysis / "in-verify" / call["input_dir"] / "task.txt")
        verify = [item for path in sorted(answers.glob("verify-*.result.json")) for item in ("--verify-result", str(path))]
        code, _, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *extract, *verify,
                                "--out", str(self.root / "final"))
        self.assertEqual(code, 0, stderr)
        built = (self.root / "final/final/task.txt").read_bytes()
        lines = (HERE / "prompts" / "finalize.md").read_text(encoding="utf-8").splitlines(True)
        rule = [line for line in lines if "JSON 표기" in line]
        clue_rule = [line for line in lines if "search_clues" in line and "GitHub 검색" in line]
        self.assertEqual((1, 1), (len(rule), len(clue_rule)))
        self.assertEqual((self.analysis / "in-final/final/task.txt").read_bytes(), self.taken_back(
            built.replace(rule[0].encode("utf-8"), b"", 1).replace(clue_rule[0].encode("utf-8"), b"", 1),
            self.FINAL_CLUE_CHANGES))
        self.assertEqual(self.FINAL_SHA256, hashlib.sha256(built).hexdigest())

    # sha256 of the bundle's extract inputs for this run under run_limits.json: the count and the size rule (#248).
    SIZED_EXTRACT_SHA256 = {
        "blind": "cce7c3a1593e2994381c8b6d81f02b3643f699d33ef12c596743b3e2bb39a2e9",
        "correctness": "fa58c7ad76aa78d557e29be192dd489357abad32b54c6e4645a008b9c93e2c99",
        "maintenance": "88b3a9f4d3853fd1c52093ddafbb965f973d5232479f9b04ab79de1fdf1ad12a",
        "review": "c860b9dcfc5606a1ede94d2d2e61446ace0c0e8a4fb902a3301ef5cad2da9f54",
        "structure": "d4f3bf6fe6732604c7a8c88b6ac17402bfe1d2b6fb336a17c2305a036840a24d",
    }

    def test_under_the_new_limits_the_extract_inputs_state_the_size_limit_and_the_larger_answers_are_refused(self):
        answers = self.analysis / "answers"
        extract = [item for lens in LENSES for item in ("--extract-result", str(answers / ("extract-%s.result.json" % lens)))]
        code, summary, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "extract"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(15, summary["max_model_calls"])        # 10 and the retry share 5 (#280)
        prompts = stage_inputs.Context(self.run).siblings.build_prompts
        size_rule = prompts.SIZE_RULE.format(answer_bytes=7680)
        # #280: the size rule is followed by its target (6,500 of the 7,680 checked); without it the pins are unchanged
        target_rule = prompts.TARGET_RULE.format(target_bytes=6500).encode("utf-8")
        self.assertEqual(6500, summary["extract_answer_target_bytes"])
        for lens in LENSES:
            built = (self.root / "extract" / ("extract-" + lens) / "task.txt").read_bytes()
            self.assertIn(size_rule.encode("utf-8") + target_rule, built, lens)
            built = built.replace(target_rule, b"", 1)
            self.assertEqual(self.SIZED_EXTRACT_SHA256[lens], hashlib.sha256(built).hexdigest(), lens)
            self.assertEqual(self.EXTRACT_SHA256[lens],
                             hashlib.sha256(built.replace(size_rule.encode("utf-8"), b"", 1)).hexdigest(), lens)
        code, _, stderr = stage("synthesis-input", "--run-dir", self.run, *extract, "--out", str(self.root / "synthesis"))
        self.assertEqual(code, 2)
        self.assertIn("extract result extract-blind is 11571 bytes as a prompt holds it (> extract_answer_max_bytes 7680)",
                      stderr)
        self.assertFalse((self.root / "synthesis").exists())
        synthesis = str(answers / "synthesis.result.json")
        code, summary, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *extract,
                                      "--out", str(self.root / "verify"))
        self.assertEqual(code, 0, stderr)
        for call in summary["calls"]:
            self.same(self.root / "verify" / call["input_dir"] / "task.txt",
                      self.analysis / "in-verify" / call["input_dir"] / "task.txt")
        verify = [item for path in sorted(answers.glob("verify-*.result.json")) for item in ("--verify-result", str(path))]
        code, _, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *extract, *verify,
                                "--out", str(self.root / "final"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.FINAL_SHA256, hashlib.sha256((self.root / "final/final/task.txt").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
