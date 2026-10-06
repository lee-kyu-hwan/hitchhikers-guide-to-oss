"""stage_inputs.py continue-plan: carry the adopted extract answers of a run into the reduce stages.

A run whose extract calls were all adopted under strict verification keeps those answers; only the
stages not yet run get a new call plan (synthesis groups + merge, verify, final). Each adopted answer
must be bound to the prompt its extract stage recorded, through the strict verdict and the picked-up
worker manifest. Every answer and record below is a hand-written stand-in; nothing here starts a
model, opens the network or reads a credential.

  /opt/homebrew/bin/python3 -B -m unittest skills/researching-open-source-contributions/analysis/test_continue_plan.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_reduce_inputs as reduce  # noqa: E402  (installs the offline guards first)
import test_stage_inputs as base  # noqa: E402
import retry_policy  # noqa: E402
import machine_settings as machine  # noqa: E402

stage_inputs, stage, workflow, CAP = reduce.stage_inputs, reduce.stage, reduce.workflow, reduce.CAP


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ContinuePlanTests(reduce.ReducedCase):
    def setUp(self):
        super().setUp()
        self.extract_stage = self.root / "x"
        self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.extract_stage))
        self.records = self.root / "calls"

    def adopted(self, pad=0):
        """The stand-in answers, each with the strict verdict and worker manifest of the call that made it."""
        recorded = json.loads((self.extract_stage / "stage.json").read_text(encoding="utf-8"))
        prompts = {call["input_dir"]: call["sha256"] for call in recorded["calls"]}
        paths = self.extract_args(pad)[1::2]
        for path in paths:
            stem = Path(path).name[: -len(".result.json")]
            call = self.records / stem
            (call / "result").mkdir(parents=True, exist_ok=True)
            (call / "result" / "manifest.json").write_text(json.dumps(
                {"status": "succeeded", "prompt_sha256": prompts[stem], "report_sha256": sha256(path)}))
            (call / "verdict.json").write_text(json.dumps({
                "ok": True, "result_class": "evidence", "answer_kind": "extract", "limits": "strict",
                "adopted_to": "/elsewhere/answers/" + Path(path).name,
                "items": [{"id": "P3-prompt-sha256", "status": "INFO", "detail": prompts[stem]},
                          {"id": "P4-prompt-binding", "status": "PASS", "detail": "manifest prompt_sha256 matches"},
                          {"id": "R1-report-present", "status": "PASS", "detail": "n bytes"}]}))
        return paths

    def continue_plan(self, paths, out="c", code=0):
        args = [item for path in paths for item in ("--extract-result", path)]
        return self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage),
                          *args, "--records", str(self.records), "--out", str(self.root / out), code=code)

    def test_the_plan_holds_only_the_stages_not_yet_run(self):
        summary, _ = self.continue_plan(self.adopted())
        plan = summary["call_plan"]
        self.assertEqual({"extract_stage_sha256": sha256(self.extract_stage / "stage.json"), "completed": {"extract": 15}},
                         plan["continues"])
        # verify at most synthesis_max_candidates 3 (7 calls under the earlier 5, #248), and the retry share of these
        # 5 calls (#280): 1 + 3 + 1
        self.assertEqual((0, 1, 1, 3, 1, 10), (plan["extract"], plan["synthesis"], plan["synthesis_groups"],
                                               plan["verify"], plan["final"], plan["max_model_calls"]))
        self.assertEqual(retry_policy.retry_share({"extract": 0, "synthesis": 1, "verify": 3, "final": 1}), plan["retry"])
        self.assertEqual([1], plan["synthesis_levels"])
        self.assertEqual(plan["shards"] * plan["lenses"], plan["continues"]["completed"]["extract"])
        self.assertEqual(10, summary["max_model_calls"])
        self.assertNotIn("pending", summary)
        self.assertEqual(summary, json.loads((self.root / "c" / "stage.json").read_text(encoding="utf-8")))
        self.assertEqual(sorted(self.context.extract_stems()), [answer["stem"] for answer in summary["answers"]])

    def test_grouped_answers_plan_one_call_per_group_and_the_merge_levels_above_them_at_the_limits(self):
        pad = 1000
        cap = self.cap_for_groups(7, pad)
        paths = self.adopted(pad)
        # the merge above the real groups is planned with every group answer at the limits (#248): limits scaled
        # down with the cap, so that the groups' candidates need more than one merge level
        base.use_limits(self, {"CANDIDATE_MAX_BYTES": 400, "MAX_CANDIDATES": 2})
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.continue_plan(paths)
            inputs = stage_inputs.PlanInputs(mode="extraction-only", shards=3, stems=self.context.extract_stems(),
                                             rows=self.context.pr_index(), quotes=stage_inputs.quote_bounds(self.context),
                                             extract_bytes=0, verify_bytes=0)
            merges, refusals = stage_inputs.synthesis_levels(inputs, 7)
        plan = summary["call_plan"]
        self.assertEqual([], refusals)
        levels = [7] + [level["calls"] for level in merges]
        self.assertGreater(len(levels), 2)
        self.assertEqual(1, levels[-1])
        self.assertEqual((7, levels, sum(levels), sum(levels) + 2 + 1 + 5),
                         (plan["synthesis_groups"], plan["synthesis_levels"], plan["synthesis"], plan["max_model_calls"]))
        self.assertIn("7 group calls, then merge levels", plan["formula"])
        # a merge that cannot shrink at these limits is refused before the next model call
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap), \
                mock.patch.object(stage_inputs, "CANDIDATE_MAX_BYTES", cap):
            _, stderr = self.continue_plan(paths, out="c2", code=2)
        self.assertIn("does not pass the worst-case plan", stderr)
        self.assertFalse((self.root / "c2").exists())

    def test_an_answer_not_bound_to_its_recorded_prompt_is_refused(self):
        def edit_json(path, change):
            document = json.loads(path.read_text(encoding="utf-8"))
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")

        def verdict_item(item_id, field, value):
            def change(document):
                for item in document["items"]:
                    if item["id"] == item_id:
                        item[field] = value
            return change

        stem = "extract-blind-01"
        cases = {
            "answer edited after adoption": lambda paths: Path(paths[0]).write_text(
                Path(paths[0]).read_text(encoding="utf-8") + " ", encoding="utf-8"),
            "answer from another prompt": lambda paths: edit_json(self.records / stem / "verdict.json",
                                                                  verdict_item("P3-prompt-sha256", "detail", "0" * 64)),
            "manifest of another prompt": lambda paths: edit_json(
                self.records / stem / "result/manifest.json", lambda d: d.update(prompt_sha256="1" * 64)),
            "binding not passed": lambda paths: edit_json(self.records / stem / "verdict.json",
                                                          verdict_item("P4-prompt-binding", "status", "FAIL")),
            "verdict not ok": lambda paths: edit_json(self.records / stem / "verdict.json", lambda d: d.update(ok=False)),
            "legacy limits": lambda paths: edit_json(self.records / stem / "verdict.json",
                                                     lambda d: d.update(limits="legacy")),
            "another answer kind": lambda paths: edit_json(self.records / stem / "verdict.json",
                                                           lambda d: d.update(answer_kind="synthesis")),
            "adopted under another name": lambda paths: edit_json(
                self.records / stem / "verdict.json", lambda d: d.update(adopted_to="/x/extract-blind-02.result.json")),
            "no record": lambda paths: shutil.rmtree(self.records / stem),
            "stage call edited (and the records with it)": lambda paths: (
                edit_json(self.extract_stage / "stage.json", lambda d: d["calls"][0].update(sha256="2" * 64)),
                edit_json(self.records / stem / "verdict.json", verdict_item("P3-prompt-sha256", "detail", "2" * 64)),
                edit_json(self.records / stem / "result/manifest.json", lambda d: d.update(prompt_sha256="2" * 64))),
            "not an extract stage": lambda paths: edit_json(self.extract_stage / "stage.json",
                                                            lambda d: d.update(stage="synthesis")),
        }
        for number, (label, spoil) in enumerate(cases.items()):
            with self.subTest(label):
                shutil.rmtree(self.records, ignore_errors=True)
                shutil.rmtree(self.extract_stage)
                self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.extract_stage))
                paths = self.adopted()
                self.assertTrue(paths[0].endswith(stem + ".result.json"))
                spoil(paths)
                _, stderr = self.continue_plan(paths, out="refused-%d" % number, code=2)
                self.assertIn("error: continue-plan:", stderr)
                self.assertFalse((self.root / ("refused-%d" % number)).exists())

    def test_a_part_of_the_extract_answers_plans_the_other_extract_calls_and_every_later_stage(self):
        """#280: a run stopped during its extract calls (the split gate run: 10 of 15 adopted) is continued with the
        adopted answers kept and the other extract prompts, bytes as the extract stage recorded them, still to call;
        the later stages are the extract stage's worst-case plan, and the retry share is that of these calls."""
        paths = self.adopted()
        recorded = json.loads((self.extract_stage / "stage.json").read_text(encoding="utf-8"))
        prompts = {call["input_dir"]: call["sha256"] for call in recorded["calls"]}
        stems = [Path(path).name[: -len(".result.json")] for path in paths]
        summary, _ = self.continue_plan(paths[:10])
        plan, worst = summary["call_plan"], recorded["call_plan"]
        self.assertEqual({"extract_stage_sha256": sha256(self.extract_stage / "stage.json"), "completed": {"extract": 10}},
                         plan["continues"])
        self.assertEqual((5, worst["synthesis"], worst["synthesis_levels"], 3, 1),
                         (plan["extract"], plan["synthesis"], plan["synthesis_levels"], plan["verify"], plan["final"]))
        calls = {stage: plan[stage] for stage in ("extract", "synthesis", "verify", "final")}
        self.assertEqual(retry_policy.retry_share(calls), plan["retry"])
        self.assertEqual(sum(calls.values()) + plan["retry"]["total"], plan["max_model_calls"])
        self.assertEqual(sorted(stems[:10]), [answer["stem"] for answer in summary["answers"]])
        self.assertEqual([{"stem": stem, "result_name": stem + ".result.json", "prompt_sha256": prompts[stem]}
                          for stem in sorted(stems[10:])], summary["pending"])
        self.assertIn("5 of 15 extract calls left", plan["formula"])
        self.assertEqual(summary, json.loads((self.root / "c" / "stage.json").read_text(encoding="utf-8")))

    def test_answers_that_are_not_a_part_of_the_extract_stage_are_refused(self):
        paths = self.adopted()
        other = self.root / "extract-blind-09.result.json"
        other.write_text(Path(paths[0]).read_text(encoding="utf-8"), encoding="utf-8")
        for label, given in {"a duplicate": paths[:3] + paths[:1], "another stem": paths[:3] + [str(other)]}.items():
            with self.subTest(label):
                _, stderr = self.continue_plan(given, out="refused", code=2)
                self.assertIn("extract result", stderr)
                self.assertFalse((self.root / "refused").exists())

    def test_an_answer_adopted_by_a_retry_is_bound_through_the_retry_call(self):
        """#280: a call retried by call_with_retry.py leaves the failed attempt under its name and the adopted one
        under <name>-r2; the answer is bound by the attempt whose strict verdict adopted it."""
        paths = self.adopted()
        stem = Path(paths[0]).name[: -len(".result.json")]
        prompt = json.loads((self.records / stem / "result/manifest.json").read_text())["prompt_sha256"]
        shutil.move(str(self.records / stem), str(self.records / (stem + "-r2")))
        (self.records / stem / "result").mkdir(parents=True)
        (self.records / stem / "result/manifest.json").write_text(json.dumps(
            {"status": "succeeded", "prompt_sha256": prompt, "report_sha256": "0" * 64}))
        (self.records / stem / "verdict.json").write_text(json.dumps({
            "ok": False, "result_class": "invalid", "answer_kind": "extract", "limits": "strict", "adopted_to": None,
            "items": [{"id": "P3-prompt-sha256", "status": "INFO", "detail": prompt},
                      {"id": "P4-prompt-binding", "status": "PASS"}, {"id": "Q1-quotes-verbatim", "status": "FAIL"}]}))
        summary, _ = self.continue_plan(paths)
        calls = {answer["stem"]: answer.get("call") for answer in summary["answers"]}
        self.assertEqual(stem + "-r2", calls.pop(stem))
        self.assertEqual({None}, set(calls.values()))
        shutil.rmtree(self.records / (stem + "-r2"))
        _, stderr = self.continue_plan(paths, out="c2", code=2)
        self.assertIn("is not bound to its recorded prompt", stderr)


class ContinueChainTests(reduce.ReducedCase):
    """continue-plan on a continue stage (#82): the parent ledger was itself a continuation and has adopted the
    synthesis (and some or all verify) answers, so only the stages after them remain, bound to that continue
    stage. Each reduce answer must be bound to the parent ledger: its strict verdict, its worker manifest and
    the ledger line of its call (input and report sha)."""

    adopted = ContinuePlanTests.adopted

    def setUp(self):
        super().setUp()
        self.extract_stage = self.root / "x"
        self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.extract_stage))
        self.records = self.root / "calls"
        self.paths = self.adopted()
        args = [item for path in self.paths for item in ("--extract-result", path)]
        self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage), *args,
                   "--records", str(self.records), "--out", str(self.root / "c"))
        self.continue_stage = self.root / "c"
        self.ledger = self.root / "gen2"
        self.ledger.mkdir()
        (self.ledger / "ledger-limit.json").write_text(json.dumps({
            "limit": 7, "call_plan_sha256": sha256(self.continue_stage / "stage.json"),
            "parent": {"ledger_sha256": "a" * 64, "calls": 15}}) + "\n")
        keys = list(self.context.keymap["sections"])[:2]
        self.synthesis = self.synthesis_answer(keys)
        self.verify = self.write("verify-C1.result.json", {
            "id": "C1", "verdict": "supported", "evidence_checks": [
                {"pr": key, "holds": True, "quote": self.quote_of(key), "reason": "r"} for key in keys],
            "counterexamples": [], "distinct_prs": 2, "distinct_repos": 1, "actionable": True,
            "actionable_reason": "r", "confidence": "medium", "revised_statement": "s"})
        self.record("synthesis", "synthesis", self.synthesis, "5" * 64)
        self.record("verify-c1", "verify", self.verify, "6" * 64)

    def record(self, call, kind, path, prompt_sha, ledger=None):
        """The call directory and ledger line of an adopted reduce answer (as analysis_call.sh and the verifier
        leave them; harness call names are lower case)."""
        ledger = ledger or self.ledger
        directory = ledger / call
        (directory / "result").mkdir(parents=True)
        (directory / "result" / "manifest.json").write_text(json.dumps(
            {"status": "succeeded", "prompt_sha256": prompt_sha, "report_sha256": sha256(path)}))
        (directory / "verdict.json").write_text(json.dumps({
            "ok": True, "result_class": "evidence", "answer_kind": kind, "limits": "strict",
            "adopted_to": "/elsewhere/answers/" + Path(path).name,
            "items": [{"id": "P3-prompt-sha256", "status": "INFO", "detail": prompt_sha},
                      {"id": "P4-prompt-binding", "status": "PASS", "detail": "manifest prompt_sha256 matches"}]}))
        with (ledger / "ledger.jsonl").open("a") as handle:
            handle.write(json.dumps({"name": call, "kind": "token", "input_sha256": prompt_sha,
                                     "prompt_sha256_recorded": prompt_sha, "report_sha256": sha256(path),
                                     "status": "succeeded"}) + "\n")

    def chain(self, *extra, out="d", code=0, synthesis=True):
        args = [item for path in self.paths for item in ("--extract-result", path)]
        if synthesis:
            args += ["--synthesis-result", self.synthesis]
        return self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.continue_stage), *args,
                          *extra, "--records", str(self.ledger), "--out", str(self.root / out), code=code)

    def test_with_the_synthesis_and_every_verify_answer_adopted_only_the_final_remains(self):
        summary, _ = self.chain("--verify-result", self.verify)
        plan = summary["call_plan"]
        self.assertEqual({"extract_stage_sha256": sha256(self.continue_stage / "stage.json"),
                          "completed": {"extract": 15, "synthesis": 1, "verify": 1}, "parent_stage": "continue"},
                         plan["continues"])
        self.assertEqual((0, 0, 0, 1, 2), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))
        self.assertEqual(plan["max_model_calls"], plan["extract"] + plan["synthesis"] + plan["verify"] + plan["final"]
                         + plan["retry"]["total"])
        self.assertEqual(2, summary["max_model_calls"])         # the final and its retry share (#280)
        self.assertEqual([("synthesis", "synthesis", "synthesis.result.json"), ("verify", "verify-c1", "verify-C1.result.json")],
                         [(a["kind"], a["call"], a["result_name"]) for a in summary["reduce_answers"]])
        self.assertEqual(summary, json.loads((self.root / "d" / "stage.json").read_text(encoding="utf-8")))

    def test_a_candidate_without_its_verify_answer_keeps_its_verify_call(self):
        plan = self.chain()[0]["call_plan"]
        self.assertEqual((0, 1, 1, 4), (plan["synthesis"], plan["verify"], plan["final"], plan["max_model_calls"]))
        self.assertEqual({"extract": 15, "synthesis": 1, "verify": 0}, plan["continues"]["completed"])

    def test_a_reduce_answer_not_bound_to_the_parent_ledger_is_refused(self):
        def edit_json(path, change):
            document = json.loads(path.read_text(encoding="utf-8"))
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")

        def edit_ledger(change):
            lines = [json.loads(line) for line in (self.ledger / "ledger.jsonl").read_text().splitlines()]
            change(lines)
            (self.ledger / "ledger.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

        cases = {
            "answer edited after adoption": lambda: Path(self.verify).write_text(
                Path(self.verify).read_text(encoding="utf-8") + " ", encoding="utf-8"),
            "verdict of another answer kind": lambda: edit_json(self.ledger / "verify-c1/verdict.json",
                                                                lambda d: d.update(answer_kind="synthesis")),
            "verdict not strict": lambda: edit_json(self.ledger / "synthesis/verdict.json", lambda d: d.update(limits="legacy")),
            "no ledger line for the call": lambda: edit_ledger(lambda lines: lines.pop()),
            "ledger input is another prompt": lambda: edit_ledger(lambda lines: lines[1].update(input_sha256="7" * 64)),
            "ledger report is another answer": lambda: edit_ledger(lambda lines: lines[0].update(report_sha256="8" * 64)),
            "records bound to another plan": lambda: edit_json(self.ledger / "ledger-limit.json",
                                                               lambda d: d.update(call_plan_sha256="9" * 64)),
            "verify answer of no candidate": lambda: edit_json(Path(self.verify), lambda d: d.update(id="C9")),
            "extract answer not the one the continue stage recorded": lambda: Path(self.paths[0]).write_text(
                Path(self.paths[0]).read_text(encoding="utf-8").replace("}", ' }', 1), encoding="utf-8"),
        }
        for number, (label, spoil) in enumerate(cases.items()):
            with self.subTest(label):
                saved = {path: path.read_bytes() for path in list(self.ledger.rglob("*")) + [Path(p) for p in self.paths]
                         + [Path(self.verify), Path(self.synthesis)] if path.is_file()}
                try:
                    spoil()
                    _, stderr = self.chain("--verify-result", self.verify, out="refused-%d" % number, code=2)
                    self.assertIn("error: continue-plan:", stderr)
                    self.assertFalse((self.root / ("refused-%d" % number)).exists())
                finally:
                    for path, data in saved.items():
                        path.write_bytes(data)
        with self.subTest("a continue stage with extract calls left"):
            partial = self.root / "partial"
            shutil.copytree(self.continue_stage, partial)
            document = json.loads((partial / "stage.json").read_text(encoding="utf-8"))
            document["pending"] = [{"stem": "extract-blind-01"}]
            (partial / "stage.json").write_text(json.dumps(document), encoding="utf-8")
            args = [item for path in self.paths for item in ("--extract-result", path)]
            _, stderr = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(partial), *args,
                                   "--synthesis-result", self.synthesis, "--records", str(self.ledger),
                                   "--out", str(self.root / "from-partial"), code=2)
            self.assertIn("extract calls left", stderr)
        with self.subTest("no synthesis answer"):
            _, stderr = self.chain(out="no-synthesis", code=2, synthesis=False)
            self.assertIn("synthesis answer", stderr)
        with self.subTest("reduce answers given with an extract stage whose records are no ledger bound to it"):
            # #286: an extract stage takes a synthesis answer only from a ledger bound to it
            # (ContinueFromExtractStageReduceTests); these records are the extract calls alone
            args = [item for path in self.paths for item in ("--extract-result", path)]
            _, stderr = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage),
                                   *args, "--synthesis-result", self.synthesis, "--records", str(self.records),
                                   "--out", str(self.root / "x-reduce"), code=2)
            self.assertIn("parent ledger limit", stderr)


class ContinueChainRetryTests(ContinueChainTests):
    """#280: a reduce answer adopted by a retry (<call>-r2) is bound by that attempt's record and ledger line."""

    def setUp(self):
        super().setUp()
        lines = (self.ledger / "ledger.jsonl").read_text().splitlines()
        shutil.move(str(self.ledger / "synthesis"), str(self.ledger / "synthesis-r2"))
        retried = dict(json.loads(lines[0]), name="synthesis-r2", attempt=2, retry_of="synthesis")
        failed = {"name": "synthesis", "kind": "token", "input_sha256": "5" * 64, "prompt_sha256_recorded": "5" * 64,
                  "report_sha256": "0" * 64, "status": "succeeded"}
        (self.ledger / "synthesis" / "result").mkdir(parents=True)
        (self.ledger / "synthesis/result/manifest.json").write_text(json.dumps(
            {"status": "succeeded", "prompt_sha256": "5" * 64, "report_sha256": "0" * 64}))
        (self.ledger / "synthesis/verdict.json").write_text(json.dumps({
            "ok": False, "result_class": "invalid", "answer_kind": "synthesis", "limits": "strict", "adopted_to": None,
            "items": [{"id": "P3-prompt-sha256", "status": "INFO", "detail": "5" * 64},
                      {"id": "P4-prompt-binding", "status": "PASS"}, {"id": "R3-report-schema", "status": "FAIL"}]}))
        (self.ledger / "ledger.jsonl").write_text("".join(json.dumps(line) + "\n" for line in
                                                          (failed, retried, json.loads(lines[1]))))

    def test_with_the_synthesis_and_every_verify_answer_adopted_only_the_final_remains(self):
        summary, _ = self.chain("--verify-result", self.verify)
        self.assertEqual(["synthesis-r2", "verify-c1"], [answer["call"] for answer in summary["reduce_answers"]])
        self.assertEqual(2, summary["max_model_calls"])

    test_a_candidate_without_its_verify_answer_keeps_its_verify_call = None
    test_a_reduce_answer_not_bound_to_the_parent_ledger_is_refused = None


class ContinuePartialChainTests(reduce.ReducedCase):
    """#280: a continuation of a run stopped during its extract calls (a continue stage with ``pending`` extract
    calls) is itself continued: the extract answers it adopted are compared by sha as before, the extract calls it
    left must be bound to the parent ledger (the continuation that made them) under the prompts it recorded, and the
    new continue stage lists all of them."""

    adopted = ContinuePlanTests.adopted
    record = ContinueChainTests.record

    def setUp(self):
        super().setUp()
        self.extract_stage = self.root / "x"
        self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.extract_stage))
        self.records = self.root / "calls"
        self.paths = self.adopted()
        args = [item for path in self.paths[:10] for item in ("--extract-result", path)]
        self.partial, _ = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage),
                                     *args, "--records", str(self.records), "--out", str(self.root / "c"))
        self.ledger = self.root / "gen2"
        self.ledger.mkdir()
        (self.ledger / "ledger-limit.json").write_text(json.dumps({
            "limit": self.partial["max_model_calls"], "call_plan_sha256": sha256(self.root / "c" / "stage.json"),
            "parent": {"ledger_sha256": "a" * 64, "calls": 10}}) + "\n")
        for item in self.partial["pending"]:                     # the extract calls gen2 made
            shutil.copytree(self.records / item["stem"], self.ledger / item["stem"])
        keys = list(self.context.keymap["sections"])[:2]
        self.synthesis = self.synthesis_answer(keys)
        self.record("synthesis", "synthesis", self.synthesis, "5" * 64)

    def chain(self, paths, out="d", code=0):
        args = [item for path in paths for item in ("--extract-result", path)]
        return self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.root / "c"), *args,
                          "--synthesis-result", self.synthesis, "--records", str(self.ledger),
                          "--out", str(self.root / out), code=code)

    def test_the_extract_calls_left_are_bound_to_the_parent_ledger(self):
        summary, _ = self.chain(self.paths)
        plan = summary["call_plan"]
        self.assertEqual({"extract": 15, "synthesis": self.partial["call_plan"]["synthesis"], "verify": 0},
                         plan["continues"]["completed"])
        self.assertEqual(sorted(self.context.extract_stems()), [answer["stem"] for answer in summary["answers"]])
        pending = {item["stem"]: item["prompt_sha256"] for item in self.partial["pending"]}
        self.assertEqual(pending, {answer["stem"]: answer["prompt_sha256"] for answer in summary["answers"]
                                   if answer["stem"] in pending})
        self.assertNotIn("pending", summary)

    def test_an_extract_call_left_and_not_bound_to_the_parent_ledger_is_refused(self):
        stem = self.partial["pending"][0]["stem"]
        shutil.rmtree(self.ledger / stem)
        _, stderr = self.chain(self.paths, out="unbound", code=2)
        self.assertIn(stem, stderr)
        _, stderr = self.chain(self.paths[:-1], out="short", code=2)
        self.assertIn("extract result", stderr)


class ContinueStraightChainTests(ContinueChainTests):
    """continue-plan on the latest ledger of a straight chain (#258): the parent ledger (gen3) continued gen2 and
    made only the calls its plan left, so the synthesis and verify answers adopted from gen2 are bound to gen2's
    ledger lines. With --ancestor-records gen2, linked by the sha gen3's ledger-limit.json recorded for its parent,
    those answers are found there, provided the continue stage of gen3 adopted the very same answers."""

    def setUp(self):
        super().setUp()
        self.gen3 = self.leaf("d", "gen3", "--verify-result", self.verify)

    def leaf(self, out, name, *extra):
        """A continue stage made from gen2 (``out``) and the ledger bound to it, continuing gen2 (``name``)."""
        ContinueChainTests.chain(self, *extra, out=out)
        ledger = self.root / name
        ledger.mkdir()
        (ledger / "ledger-limit.json").write_text(json.dumps({
            "limit": 1, "call_plan_sha256": sha256(self.root / out / "stage.json"),
            "parent": {"ledger_sha256": sha256(self.ledger / "ledger.jsonl"), "calls": 2, "ancestors": 15,
                       "limit_sha256": sha256(self.ledger / "ledger-limit.json")}}) + "\n")
        (ledger / "ledger.jsonl").write_text(json.dumps({"name": "final", "kind": "token", "status": "succeeded"}) + "\n")
        return ledger

    def chain(self, *extra, out="e", code=0, synthesis=True, stage="d", records=None, ancestors=("gen2",)):
        args = [item for path in self.paths for item in ("--extract-result", path)]
        if synthesis:
            args += ["--synthesis-result", self.synthesis]
        args += [item for name in ancestors for item in ("--ancestor-records", str(self.root / name))]
        return self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.root / stage), *args,
                          *extra, "--records", str(records or self.gen3), "--out", str(self.root / out), code=code)

    def test_the_answers_adopted_from_an_ancestor_leave_only_the_final(self):
        summary, _ = self.chain("--verify-result", self.verify)
        plan = summary["call_plan"]
        self.assertEqual({"extract_stage_sha256": sha256(self.root / "d" / "stage.json"),
                          "completed": {"extract": 15, "synthesis": 1, "verify": 1}, "parent_stage": "continue"},
                         plan["continues"])
        self.assertEqual((0, 0, 0, 1, 2), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))
        recorded = json.loads((self.root / "d" / "stage.json").read_text(encoding="utf-8"))["reduce_answers"]
        self.assertEqual([dict(answer, ancestor=1) for answer in recorded], summary["reduce_answers"])

    def test_an_answer_made_by_the_parent_and_one_adopted_from_the_ancestor(self):
        gen3 = self.leaf("d2", "gen3b")                                # gen3 still had the verify call to make
        verify = self.root / "gen3b-answers" / "verify-C1.result.json"     # gen3b's own verify answer
        verify.parent.mkdir()
        verify.write_text(json.dumps(dict(json.loads(Path(self.verify).read_text()), confidence="high")))
        verify = str(verify)
        self.record("verify-c1", "verify", verify, "7" * 64, ledger=gen3)
        summary, _ = self.chain("--verify-result", verify, stage="d2", records=gen3)
        self.assertEqual([("synthesis", 1), ("verify-c1", None)],
                         [(answer["call"], answer.get("ancestor")) for answer in summary["reduce_answers"]])
        self.assertEqual((0, 2), (summary["call_plan"]["verify"], summary["max_model_calls"]))

    def test_an_answer_of_the_chain_not_bound_through_the_recorded_links_is_refused(self):
        gen2_ledger, gen2_limit = self.ledger / "ledger.jsonl", self.ledger / "ledger-limit.json"
        with self.subTest("no ancestor records"):
            _, stderr = self.chain("--verify-result", self.verify, out="no-ancestor", code=2, ancestors=())
            self.assertIn("no call record", stderr)
        cases = {
            "ancestor ledger grew": lambda: gen2_ledger.write_text(gen2_ledger.read_text() + gen2_ledger.read_text()),
            "ancestor limit edited": lambda: gen2_limit.write_text(gen2_limit.read_text() + " "),
            "parent records another parent": lambda: (self.gen3 / "ledger-limit.json").write_text(
                (self.gen3 / "ledger-limit.json").read_text().replace(sha256(gen2_ledger), "0" * 64)),
        }
        for number, (label, spoil) in enumerate(cases.items()):
            with self.subTest(label):
                saved = {path: path.read_bytes() for path in (gen2_ledger, gen2_limit, self.gen3 / "ledger-limit.json")}
                try:
                    spoil()
                    _, stderr = self.chain("--verify-result", self.verify, out="refused-%d" % number, code=2)
                    self.assertIn("is not the parent ledger", stderr)
                    self.assertFalse((self.root / ("refused-%d" % number)).exists())
                finally:
                    for path, data in saved.items():
                        path.write_bytes(data)
        with self.subTest("records with no ledger as the ancestor"):
            _, stderr = self.chain("--verify-result", self.verify, out="calls", code=2, ancestors=("calls",))
            self.assertIn("error: continue-plan:", stderr)
        with self.subTest("an ancestor answer the continue stage did not adopt"):
            gen3 = self.leaf("d3", "gen3c")                            # d3 left the verify call to gen3c
            _, stderr = self.chain("--verify-result", self.verify, out="not-adopted", code=2, stage="d3", records=gen3)
            self.assertIn("not adopted by the continue stage", stderr)
        with self.subTest("ancestor records with an extract stage"):
            args = [item for path in self.paths for item in ("--extract-result", path)]
            _, stderr = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage),
                                   *args, "--ancestor-records", str(self.ledger), "--records", str(self.records),
                                   "--out", str(self.root / "x-ancestor"), code=2)
            self.assertIn("continue stage", stderr)

    # the gen2 cases of ContinueChainTests run against gen2 itself
    test_with_the_synthesis_and_every_verify_answer_adopted_only_the_final_remains = None
    test_a_candidate_without_its_verify_answer_keeps_its_verify_call = None
    test_a_reduce_answer_not_bound_to_the_parent_ledger_is_refused = None


class ContinueFromExtractStageReduceTests(reduce.ReducedCase):
    """#286: a ledger bound to an extract stage that went on past the extract calls: every extract answer, the group
    answers and the run's synthesis answer adopted, then a stop at a verify call (the split gate fresh run). Its
    continue plan holds the verify calls not adopted and the final, with their retry share, bound to the same extract
    stage; the extract answers are bound to their recorded prompts, the synthesis and verify answers to the ledger."""

    adopted = ContinuePlanTests.adopted

    def setUp(self):
        super().setUp()
        self.extract_stage = self.root / "x"
        self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.extract_stage))
        self.records = self.root / "calls"
        self.paths = self.adopted()
        (self.records / "ledger-limit.json").write_text(json.dumps({
            "limit": 32, "call_plan_sha256": sha256(self.extract_stage / "stage.json")}) + "\n")
        keys = list(self.context.keymap["sections"])
        self.group = self.synthesis_answer(keys[:2], name="synthesis-01.result.json")
        self.synthesis = self.synthesis_answer(keys[:2])
        self.verify = self.write("verify-C1.result.json", {
            "id": "C1", "verdict": "supported", "evidence_checks": [
                {"pr": key, "holds": True, "quote": self.quote_of(key), "reason": "r"} for key in keys[:2]],
            "counterexamples": [{"pr": keys[2], "reason": "listed only"}], "distinct_prs": 2, "distinct_repos": 1,
            "actionable": True, "actionable_reason": "r", "confidence": "medium", "revised_statement": "s"})
        for stem in [Path(path).name[: -len(".result.json")] for path in self.paths]:
            self.line(stem, "1" * 64, "2" * 64)
        self.record("synthesis-01", "synthesis", self.group, "3" * 64)
        self.record("synthesis", "synthesis", self.synthesis, "5" * 64)

    def line(self, call, prompt_sha, report_sha):
        with (self.records / "ledger.jsonl").open("a") as handle:
            handle.write(json.dumps({"name": call, "kind": "token", "input_sha256": prompt_sha,
                                     "prompt_sha256_recorded": prompt_sha, "report_sha256": report_sha,
                                     "status": "succeeded"}) + "\n")

    def record(self, call, kind, path, prompt_sha):
        ContinueChainTests.record(self, call, kind, path, prompt_sha, ledger=self.records)

    def plan(self, *extra, out="c", code=0, synthesis=None):
        args = [item for path in self.paths for item in ("--extract-result", path)]
        return self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage), *args,
                          "--synthesis-result", synthesis or self.synthesis, *extra, "--records", str(self.records),
                          "--out", str(self.root / out), code=code)

    def test_the_plan_holds_the_verify_calls_not_adopted_and_the_final(self):
        summary, _ = self.plan()
        plan = summary["call_plan"]
        self.assertEqual({"extract_stage_sha256": sha256(self.extract_stage / "stage.json"),
                          "completed": {"extract": 15, "synthesis": 2, "verify": 0}}, plan["continues"])
        self.assertNotIn("parent_stage", plan["continues"])          # the parent ledger is bound to an extract stage
        self.assertEqual(retry_policy.retry_share({"verify": 1, "final": 1}), plan["retry"])
        self.assertEqual((0, 0, 1, 1, 4), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))
        # the harness's own rules for a continuation plan (analysis_call.sh)
        self.assertEqual(plan["shards"] * plan["lenses"], plan["extract"] + plan["continues"]["completed"]["extract"])
        self.assertEqual(plan["max_model_calls"], plan["extract"] + plan["synthesis"] + plan["verify"] + plan["final"]
                         + plan["retry"]["total"])
        self.assertEqual([("synthesis", "synthesis", "synthesis.result.json")],
                         [(a["kind"], a["call"], a["result_name"]) for a in summary["reduce_answers"]])
        self.assertEqual(15, len(summary["answers"]))
        self.assertEqual(summary, json.loads((self.root / "c" / "stage.json").read_text(encoding="utf-8")))

    def test_an_adopted_verify_answer_leaves_only_the_final(self):
        self.record("verify-c1", "verify", self.verify, "6" * 64)
        summary, _ = self.plan("--verify-result", self.verify)
        plan = summary["call_plan"]
        self.assertEqual((0, 1, 2), (plan["verify"], plan["final"], plan["max_model_calls"]))
        self.assertEqual({"extract": 15, "synthesis": 2, "verify": 1}, plan["continues"]["completed"])

    def test_what_is_not_the_run_s_own_adopted_synthesis_is_refused(self):
        _, stderr = self.plan(out="group", code=2, synthesis=self.group)
        self.assertIn("the run's own (synthesis.result.json)", stderr)
        _, stderr = self.plan("--verify-result", self.verify, out="unbound", code=2)
        self.assertIn("no call record for the adopted answer verify-C1.result.json", stderr)
        args = [item for path in self.paths for item in ("--extract-result", path)]
        _, stderr = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage), *args,
                               "--verify-result", self.verify, "--records", str(self.records),
                               "--out", str(self.root / "alone"), code=2)
        self.assertIn("only with the synthesis answer they verify", stderr)
        limit = self.records / "ledger-limit.json"
        kept = limit.read_bytes()
        for label, document in (("another plan", {"limit": 32, "call_plan_sha256": "9" * 64}),
                                ("a continuation", {"limit": 32, "call_plan_sha256": sha256(self.extract_stage / "stage.json"),
                                                    "parent": {"ledger_sha256": "a" * 64, "calls": 3}})):
            with self.subTest(label):
                limit.write_text(json.dumps(document) + "\n")
                _, stderr = self.plan(out="bound-" + label.split()[-1], code=2)
                self.assertIn("not a ledger bound to this extract stage", stderr)
                limit.write_bytes(kept)
        self.assertFalse(any((self.root / name).exists() for name in ("group", "unbound", "alone", "bound-plan",
                                                                         "bound-continuation")))


EVIDENCE_R2 = machine.VM_LOGS / "a78-period-analysis-eslint-r2-20261004T131350Z"


class RunR2Case(unittest.TestCase):
    """The eslint r2 run rebuilt from the same collections in a temporary directory (same corpus, same digest,
    same extract prompts); the evidence is only read. Its answers were made and adopted under the stage output
    limits before #248 (base.EARLIER_LIMITS), under which every stage input below is what it was; RunR2NewLimitsTests
    shows what run_limits.json's own limits do with them."""

    LIMITS = base.EARLIER_LIMITS

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        base.use_limits(self, self.LIMITS)
        state = json.loads((EVIDENCE_R2 / "wf-run/state.json").read_text(encoding="utf-8"))
        inputs = state["stages"]["collect"]["generations"][0]["inputs"]
        adopt = []
        for name in sorted(key for key in inputs if key.startswith("corpus-")):
            unpacked = Path(inputs[name]["source_path"]).parent
            adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                      "--inventory", str(unpacked / "inventory.md")]
        self.run = str(self.root / "run")
        request = base.entry_budget_request(EVIDENCE_R2 / "wf-run/request.json", self.root)
        for argv in (("init", "--request", str(request)), ("adopt-collection", *adopt),
                     ("digest", "--max-bytes", "54000")):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)
        self.answers = sorted(str(path) for path in (EVIDENCE_R2 / "answers").glob("*.result.json"))
        self.extract = [item for path in self.answers for item in ("--extract-result", path)]


@unittest.skipUnless((EVIDENCE_R2 / "in-extract/stage.json").is_file() and (EVIDENCE_R2 / "answers").is_dir(),
                     "the 2026-10-04 eslint r2 run is only on the machine that made it")
class RunR2ContinuationTests(RunR2Case):
    """The eslint r2 run of 2026-10-04: 20 extract answers adopted under strict verification, then refused at
    synthesis-input for 7 groups over the old limit of 3. (Its 14-call continuation plan was made and run as r3,
    a78-period-analysis-eslint-r3-20261005T025628Z/in-continue.)

    Since the analyzer's extract prompt asks for search clues as GitHub search strings (#82), the r2 extract stage
    records other prompts than the analyzer writes now, and continue-plan refuses to start from it again: answers
    are reused only under the prompts that made them. A continuation of r3 (a continue stage) does not compare
    extract prompts and is unaffected (RunR3ChainTests)."""

    def test_the_r2_extract_stage_is_not_continued_again_under_the_changed_extract_prompt(self):
        code, _, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage",
                                str(EVIDENCE_R2 / "in-extract"), *self.extract, "--records", str(EVIDENCE_R2),
                                "--out", str(self.root / "continue"))
        self.assertEqual(2, code, stderr)
        self.assertIn("records other prompts than this run's analyzer writes", stderr)
        self.assertFalse((self.root / "continue").exists())
        code, synthesis, stderr = stage("synthesis-input", "--run-dir", self.run, *self.extract,
                                        "--out", str(self.root / "synthesis"))
        self.assertEqual(0, code, stderr)
        self.assertEqual(7, len(synthesis["groups"]))
        self.assertEqual(8, synthesis["synthesis_calls"])
        self.assertTrue(all(call["bytes"] <= CAP for call in synthesis["calls"]))
        # the group inputs r3 was given, byte for byte (no fixed group limit any more, #248)
        self.assertEqual([60575, 44558, 63190, 57468, 64492, 51021, 41632], [call["bytes"] for call in synthesis["calls"]])



EVIDENCE_R3 = machine.VM_LOGS / "a78-period-analysis-eslint-r3-20261005T025628Z"


@unittest.skipUnless((EVIDENCE_R3 / "answers/synthesis-07.result.json").is_file()
                     and (EVIDENCE_R2 / "answers").is_dir(), "the 2026-10-05 eslint r3 run is only on the machine that made it")
class RunR3MergeTests(RunR2Case):
    """The eslint r3 run of 2026-10-05: seven group answers adopted under strict verification, then the merge input
    refused at 83,563 bytes. With the candidates only and one quote per (candidate, cited PR) it fits."""

    def test_the_seven_group_answers_make_a_merge_input_within_the_cap(self):
        code, _, stderr = stage("synthesis-input", "--run-dir", self.run, *self.extract, "--out", str(self.root / "s"))
        self.assertEqual(0, code, stderr)
        groups = [item for path in sorted((EVIDENCE_R3 / "answers").glob("synthesis-*.result.json"))
                  for item in ("--group-result", str(path))]
        code, merged, stderr = stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage",
                                     str(self.root / "s"), *groups, *self.extract, "--out", str(self.root / "m"))
        self.assertEqual(0, code, stderr)
        self.assertLessEqual(merged["calls"][0]["bytes"], CAP)
        self.assertEqual({"listed": 61, "no_quote": 0}, merged["merge_quotes"])
        self.assertEqual(8, merged["synthesis_calls"])
        self.assertEqual((40205, "291230b69b8f73cb775958dd4b49e5a24a8555be3b986977e2a6649af2ab2500"),
                         (merged["calls"][0]["bytes"], merged["calls"][0]["sha256"]))     # the r4 merge input
        scope = json.loads((self.root / "m" / "synthesis.scope.json").read_text(encoding="utf-8"))
        self.assertEqual((25, 61), (len(scope["keys"]), len(scope["quotes"])))



@unittest.skipUnless((EVIDENCE_R3 / "answers/verify-C5.result.json").is_file() and (EVIDENCE_R2 / "answers").is_dir(),
                     "the 2026-10-05 eslint r4 answers are only on the machine that made them")
class RunR4FinalTests(RunR2Case):
    """The eslint r4 step of 2026-10-05: the merge answer and five verify answers adopted under strict verification,
    then the final input refused at 77,376 bytes. With one quote per (candidate, cited PR) plus the verify answers'
    verified quotes it fits. #286: finalize-reduced.md states the empty value rules and the clue's control characters;
    with those sentences taken back the input is the r6 final input byte for byte."""

    # finalize-reduced.md (#286): (now, before)
    EMPTY_VALUE_CHANGES = (
        ('"limitations": [문자열]}` 이다(배열의 항목은 비어 있지 않은 문자열이다). 부분', '"limitations": [문자열]}` 이다. 부분'),
        ("`extraction_ledger` 는 비어 있지 않은 한 줄 문자열이다.", "`extraction_ledger` 는 한 줄 문자열이다."),
        ("문자열 배열이다(항목은 비어 있지 않은 문자열이다).", "문자열 배열이다."),
        ("줄바꿈·탭 같은 제어 문자·앞뒤 공백이 없다.", "줄바꿈·앞뒤 공백이 없다."),
    )

    def test_the_final_input_of_the_adopted_answers_is_within_the_cap(self):
        verify = [item for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                  for item in ("--verify-result", str(path))]
        code, final, stderr = stage("final-input", "--run-dir", self.run, "--synthesis",
                                    str(EVIDENCE_R3 / "answers/synthesis.result.json"), *self.extract, *verify,
                                    "--out", str(self.root / "f"))
        self.assertEqual(0, code, stderr)
        self.assertLessEqual(final["calls"][0]["bytes"], CAP)
        self.assertEqual({"listed": 38, "no_quote": 0}, final["final_quotes"])
        self.assertEqual((44261, "2ecc1872ad427f14a76f96feb7d039902ebfe5b1c149922f5ea1d66840e6441e"),
                         (final["calls"][0]["bytes"], final["calls"][0]["sha256"]))     # with the #286 rules
        text = (self.root / "f" / "final" / "task.txt").read_text(encoding="utf-8")
        for now, before in self.EMPTY_VALUE_CHANGES:
            self.assertEqual(1, text.count(now), now)
            text = text.replace(now, before)
        self.assertEqual((44108, "fc66309c06a61ad65f88198f3aae1e333dc26610c7648297539942caaf051a06"),
                         (len(text.encode("utf-8")), hashlib.sha256(text.encode("utf-8")).hexdigest()))  # the r6 input
        scope = json.loads((self.root / "f" / "final.scope.json").read_text(encoding="utf-8"))
        self.assertEqual((28, 38), (len(scope["keys"]), len(scope["quotes"])))



@unittest.skipUnless((EVIDENCE_R3 / "in-continue/stage.json").is_file() and (EVIDENCE_R3 / "answers/verify-C5.result.json").is_file()
                     and (EVIDENCE_R2 / "answers").is_dir(), "the 2026-10-05 eslint r3 ledger is only on the machine that made it")
class RunR3ChainTests(RunR2Case):
    """The eslint r3 ledger of 2026-10-05 (a continuation of r2, 14/14 used): its adopted merge and five verify
    answers continue into a plan of the final call only, bound to the r3 continue stage that its ledger is bound
    to (#82). The evidence is only read."""

    def test_the_adopted_reduce_answers_leave_one_final_call(self):
        verify = [item for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                  for item in ("--verify-result", str(path))]
        code, summary, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage",
                                      str(EVIDENCE_R3 / "in-continue"), *self.extract, "--synthesis-result",
                                      str(EVIDENCE_R3 / "answers/synthesis.result.json"), *verify,
                                      "--records", str(EVIDENCE_R3), "--out", str(self.root / "chain"))
        self.assertEqual(0, code, stderr)
        plan = summary["call_plan"]
        bound = json.loads((EVIDENCE_R3 / "ledger-limit.json").read_text(encoding="utf-8"))["call_plan_sha256"]
        self.assertEqual(bound, plan["continues"]["extract_stage_sha256"])
        self.assertEqual({"extract": 20, "synthesis": 8, "verify": 5}, plan["continues"]["completed"])
        self.assertEqual("continue", plan["continues"]["parent_stage"])
        self.assertEqual((0, 0, 0, 1, 2), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))      # the final and its retry share (#280)


@unittest.skipUnless((EVIDENCE_R3 / "in-continue/stage.json").is_file() and (EVIDENCE_R3 / "answers/verify-C5.result.json").is_file()
                     and (EVIDENCE_R2 / "answers").is_dir(), "the 2026-10-05 eslint r2-r4 answers are only on the machine that made them")
class RunR2NewLimitsTests(RunR2Case):
    """The same evidence under run_limits.json's own stage output limits (#248): 13 of r2's 20 extract answers are
    over extract_answer_max_bytes (7,680), r3's group and merge answers hold 5 candidates (over 3), and r4's verify
    answer C1 is 5,691 bytes (over 5,120). Each is refused, never cut; the evidence stays as it is."""

    LIMITS = {}

    def test_the_answers_adopted_under_the_earlier_limits_are_refused_under_the_new_ones(self):
        sizes = {Path(path).name: stage_inputs.answer_bytes(json.loads(Path(path).read_text(encoding="utf-8")))
                 for path in self.answers}
        self.assertEqual(13, len([size for size in sizes.values() if size > 7680]))
        self.assertEqual(18294, sizes["extract-blind-01.result.json"])
        code, _, stderr = stage("synthesis-input", "--run-dir", self.run, *self.extract, "--out", str(self.root / "s"))
        self.assertEqual(2, code)
        self.assertIn("extract result extract-blind-01 is 18294 bytes as a prompt holds it (> extract_answer_max_bytes "
                      "7680)", stderr)
        for name in ("synthesis-01.result.json", "synthesis.result.json"):
            code, _, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis",
                                    str(EVIDENCE_R3 / "answers" / name), *self.extract, "--out", str(self.root / "v"))
            self.assertEqual(2, code, name)
            self.assertIn("synthesis may hold at most 3 candidates", stderr)
        over = [path.name for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                if path.stat().st_size > stage_inputs.VERIFY_ANSWER_MAX_BYTES]
        self.assertEqual(["verify-C1.result.json"], over)
        verify = [item for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                  for item in ("--verify-result", str(path))]
        code, _, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage",
                                str(EVIDENCE_R3 / "in-continue"), *self.extract, "--synthesis-result",
                                str(EVIDENCE_R3 / "answers/synthesis.result.json"), *verify,
                                "--records", str(EVIDENCE_R3), "--out", str(self.root / "chain"))
        self.assertEqual(2, code)
        self.assertIn("extract_answer_max_bytes", stderr)
        self.assertFalse(any((self.root / name).exists() for name in ("s", "v", "chain")))


EVIDENCE_R6 = machine.VM_LOGS / "a78-period-analysis-eslint-r6-20261005T043448Z"


@unittest.skipUnless((EVIDENCE_R6 / "in-continue/stage.json").is_file() and (EVIDENCE_R6 / "ledger-limit.json").is_file()
                     and (EVIDENCE_R3 / "answers/verify-C5.result.json").is_file() and (EVIDENCE_R2 / "answers").is_dir(),
                     "the 2026-10-05 eslint r6 ledger is only on the machine that made it")
class RunR6ChainTests(RunR2Case):
    """The eslint r6 ledger of 2026-10-05 (a continuation of r3, final 1/1 used): the latest ledger of the chain
    r2 → r3 → r6 (#258). Its continue stage adopted r3's merge and five verify answers, which stay bound to r3's
    ledger lines; with r3 as the ancestor ledger (linked by the sha r6's ledger-limit.json recorded) they continue
    into a plan of the final call only, bound to r6's continue stage. The evidence is only read."""

    def test_the_latest_ledger_continues_with_the_answers_bound_in_r3(self):
        verify = [item for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                  for item in ("--verify-result", str(path))]
        kept = {path: path.read_bytes() for directory in (EVIDENCE_R3, EVIDENCE_R6)
                for path in (directory / "ledger.jsonl", directory / "ledger-limit.json")}
        common = ("--run-dir", self.run, "--extract-stage", str(EVIDENCE_R6 / "in-continue"), *self.extract,
                  "--synthesis-result", str(EVIDENCE_R3 / "answers/synthesis.result.json"), *verify,
                  "--records", str(EVIDENCE_R6))
        code, _, stderr = stage("continue-plan", *common, "--out", str(self.root / "no-ancestor"))
        self.assertEqual(2, code, stderr)
        self.assertIn("no call record", stderr)
        code, _, stderr = stage("continue-plan", *common, "--ancestor-records", str(EVIDENCE_R2),
                                "--out", str(self.root / "r2-as-ancestor"))
        self.assertEqual(2, code, stderr)
        self.assertIn("is not the parent ledger", stderr)
        code, summary, stderr = stage("continue-plan", *common, "--ancestor-records", str(EVIDENCE_R3),
                                      "--out", str(self.root / "r7"))
        self.assertEqual(0, code, stderr)
        plan = summary["call_plan"]
        bound = json.loads((EVIDENCE_R6 / "ledger-limit.json").read_text(encoding="utf-8"))["call_plan_sha256"]
        self.assertEqual(bound, plan["continues"]["extract_stage_sha256"])
        self.assertEqual({"extract": 20, "synthesis": 8, "verify": 5}, plan["continues"]["completed"])
        self.assertEqual("continue", plan["continues"]["parent_stage"])
        self.assertEqual((0, 0, 0, 1, 2), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))      # the final and its retry share (#280)
        recorded = json.loads((EVIDENCE_R6 / "in-continue/stage.json").read_text(encoding="utf-8"))["reduce_answers"]
        self.assertEqual([dict(answer, ancestor=1) for answer in recorded], summary["reduce_answers"])
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)
        self.assertFalse((EVIDENCE_R6 / "continued-by.json").exists() or (EVIDENCE_R3 / "continued-by.json").exists())



SPLIT_GATE = machine.VM_LOGS / "a78-split-gate-20261006T055754Z"
# The ten extract answers the split gate run adopted (docs/research/2026-10-06-split-gate-run/README.md).
SPLIT_GATE_ANSWERS = {
    "extract-blind-01": "a10f20ff3842031dbdf1cb121cd4ed73c37688020ba66f6907ec546e70055a38",
    "extract-blind-02": "db8cc1e3c4d3784ed79ff1595ca83ec15447cadc57ba036ef2cb88196854fc75",
    "extract-correctness-01": "92f86f3456d30c147bb5a28f1d1b2c8050fdc95b6c19fba8563c5ad8772fd3a0",
    "extract-correctness-02": "d0b2838215e346af622e3fbc3b0540d91c2f99c79949d8db44c38a88811c36b0",
    "extract-maintenance-01": "b9a88a50ee7120cfb725911b894b75a868b212a3c0f04b1f8bcb92d92794d5c0",
    "extract-maintenance-02": "6df595daa889a0755e2557e0bc843a0aecdbbfa077e7765e6b96073d08c5261f",
    "extract-review-01": "7e630d7d25d953b705421737fa9f4d0a26c1fefb33a75b0cd4d186118f343dc0",
    "extract-review-02": "1edd2eeea6c93f9e3b3fef227a83da0e3dd0b49eff07550687b6326924b6f09f",
    "extract-structure-01": "5bab9981e10f8679603723acb6ee47b305a65cf80e1e25ffdc5c91379262575b",
    "extract-structure-02": "55fa0546b1676358223598a528427ae2af088bff37e1e3121bab8557e262e988",
}


@unittest.skipUnless((SPLIT_GATE / "in-extract/stage.json").is_file() and (SPLIT_GATE / "ledger/ledger.jsonl").is_file(),
                     "the 2026-10-06 split gate run is only on the machine that made it")
class SplitGateContinuationTests(unittest.TestCase):
    """#280: the split gate run (typescript-eslint, 21 PRs) stopped after 12 calls with 10 of 15 extract answers
    adopted, one of them by a retry (extract-structure-01-r2). Its run is rebuilt from the same collections in a
    temporary directory; the evidence is only read. continue-plan takes the ten answers byte for byte and plans the
    five other extract prompts (as the extract stage recorded them, without the #280 target sentence) and every
    later stage with its retry share: 17 + 5 = 22 calls, which with the parent ledger's 12 stay within 40.

    The 2026-10-06 rerun continued that ledger (its continued-by.json names the rerun's ledger); continue-plan only
    reads it and leaves it as it is."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        state = json.loads((SPLIT_GATE / "wf-run/state.json").read_text(encoding="utf-8"))
        inputs = state["stages"]["collect"]["generations"][0]["inputs"]
        adopt = []
        for name in sorted(key for key in inputs if key.startswith("corpus-")):
            unpacked = Path(inputs[name]["source_path"]).parent
            adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                      "--inventory", str(unpacked / "inventory.md")]
        self.run = str(self.root / "run")
        for argv in (("init", "--request", str(SPLIT_GATE / "request.json")), ("adopt-collection", *adopt),
                     ("digest", "--max-bytes", "54000")):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)

    def test_the_ten_adopted_answers_continue_into_the_remaining_calls_with_their_retry_share(self):
        kept = {path: path.read_bytes() for path in (SPLIT_GATE / "ledger").glob("ledger*")}
        claim = SPLIT_GATE / "ledger/continued-by.json"
        claimed = claim.read_bytes() if claim.exists() else None
        answers = sorted(SPLIT_GATE / "answers" / (stem + ".result.json") for stem in SPLIT_GATE_ANSWERS)
        self.assertEqual(SPLIT_GATE_ANSWERS, {path.name[: -len(".result.json")]: sha256(path) for path in answers})
        args = [item for path in answers for item in ("--extract-result", str(path))]
        code, summary, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage",
                                      str(SPLIT_GATE / "in-extract"), *args, "--records", str(SPLIT_GATE / "ledger"),
                                      "--out", str(self.root / "continue"))
        self.assertEqual(0, code, stderr)
        plan = summary["call_plan"]
        self.assertEqual({"extract_stage_sha256": "c24749fcf0cfaad606f2400213db2885072c065c13ca21a07542fb3addf2bdbc",
                          "completed": {"extract": 10}}, plan["continues"])
        self.assertEqual((5, 8, [5, 2, 1], 3, 1), (plan["extract"], plan["synthesis"], plan["synthesis_levels"],
                                                   plan["verify"], plan["final"]))
        self.assertEqual({"per_call": 1, "per_stage": 3, "stages": {"extract": 3, "synthesis": 3, "verify": 3, "final": 1},
                          "total": 5}, plan["retry"])
        self.assertEqual(22, plan["max_model_calls"])
        parent = [json.loads(line) for line in (SPLIT_GATE / "ledger/ledger.jsonl").read_text().splitlines()]
        self.assertEqual(12, len(parent))
        self.assertLessEqual(len(parent) + plan["max_model_calls"], stage_inputs.ABSOLUTE_MAX_CALLS)     # 34
        recorded = json.loads((SPLIT_GATE / "in-extract/stage.json").read_text(encoding="utf-8"))
        prompts = {call["input_dir"]: call["sha256"] for call in recorded["calls"]}
        self.assertEqual([{"stem": stem, "result_name": stem + ".result.json", "prompt_sha256": prompts[stem]}
                          for stem in ("extract-blind-03", "extract-correctness-03", "extract-maintenance-03",
                                       "extract-review-03", "extract-structure-03")], summary["pending"])
        for item in summary["pending"]:
            self.assertEqual(item["prompt_sha256"], sha256(SPLIT_GATE / "in-extract" / item["stem"] / "task.txt"))
        self.assertEqual({stem: (sha, "extract-structure-01-r2" if stem == "extract-structure-01" else None)
                          for stem, sha in SPLIT_GATE_ANSWERS.items()},
                         {answer["stem"]: (answer["answer_sha256"], answer.get("call")) for answer in summary["answers"]})
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)
        self.assertEqual(claimed, claim.read_bytes() if claim.exists() else None)



FRESH = machine.VM_LOGS / "a78-split-gate-fresh-20261006T081016Z"


def rebuild_run(test, evidence: Path, root: Path) -> str:
    """An evidence run rebuilt in ``root`` from the same collections, request and digest; the evidence is only read."""
    state = json.loads((evidence / "wf-run/state.json").read_text(encoding="utf-8"))
    inputs = state["stages"]["collect"]["generations"][0]["inputs"]
    adopt = []
    for name in sorted(key for key in inputs if key.startswith("corpus-")):
        unpacked = Path(inputs[name]["source_path"]).parent
        adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                  "--inventory", str(unpacked / "inventory.md")]
    run = str(root / "run")
    for argv in (("init", "--request", str(evidence / "request.json")), ("adopt-collection", *adopt),
                 ("digest", "--max-bytes", "54000")):
        code, _, stderr = workflow(argv[0], "--run-dir", run, *argv[1:])
        test.assertEqual(code, 0, stderr)
    return run


@unittest.skipUnless((FRESH / "in-extract/stage.json").is_file() and (FRESH / "ledger/ledger.jsonl").is_file()
                     and (FRESH / "answers/synthesis.result.json").is_file(),
                     "the 2026-10-06 split gate fresh run is only on the machine that made it")
class SplitGateFreshContinuationTests(unittest.TestCase):
    """#286: the split gate fresh run (typescript-eslint, 21 PRs) stopped after 22 of its 32 calls at its first verify
    call (verify-c1 R5, its retry verify-c1-r2 R3: the two prompt-strict contract defects). Its ledger adopted the 15
    extract answers (extract-blind-03 by its retry), the 3 group answers and the merge answer. continue-plan takes them
    byte for byte from that ledger and plans the run again from verify: 3 verify calls and the final, with their retry
    share; verify-inputs writes the verify prompts again under the prompts in force (the size and counterexample
    rules). The evidence is only read: no file of it changes and no continued-by.json is written."""

    # sha256 of the verify prompts verify-inputs writes now for this run (with the #286 verify rules)
    # (the run's own were 50,958, 35,795 and 32,253 bytes)
    VERIFY = {"verify-C1": (51915, "11ea5e90d663f2e503dd514b1a7e1200834a118b25ba824246cf8fb0f28a8dab"),
              "verify-C2": (36752, "4b47af3150a4344519a63e58d8cf3ff6a0d748aacc1dcf5d24978feedca49684"),
              "verify-C3": (33210, "b27fbf9bae20079a38f0ae20de607bbf50ede15223e1d89d7db8516d1e6dd626")}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = rebuild_run(self, FRESH, self.root)
        self.extract = sorted(FRESH / "answers" / path.name for path in (FRESH / "answers").glob("extract-*.result.json"))

    def test_the_run_continues_from_verify_with_the_adopted_answers_byte_for_byte(self):
        kept = {path: path.read_bytes() for path in FRESH.rglob("*") if path.is_file()}
        args = [item for path in self.extract for item in ("--extract-result", str(path))]
        code, summary, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(FRESH / "in-extract"),
                                      *args, "--synthesis-result", str(FRESH / "answers/synthesis.result.json"),
                                      "--records", str(FRESH / "ledger"), "--out", str(self.root / "continue"))
        self.assertEqual(0, code, stderr)
        plan = summary["call_plan"]
        bound = json.loads((FRESH / "ledger/ledger-limit.json").read_text(encoding="utf-8"))
        self.assertEqual({"limit": 32, "call_plan_sha256": sha256(FRESH / "in-extract/stage.json")}, bound)
        self.assertEqual({"extract_stage_sha256": bound["call_plan_sha256"],
                          "completed": {"extract": 15, "synthesis": 4, "verify": 0}}, plan["continues"])
        self.assertEqual((0, 0, 3, 1), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"]))
        self.assertEqual({"per_call": 1, "per_stage": 3, "stages": {"extract": 0, "synthesis": 0, "verify": 3, "final": 1},
                          "total": 4}, plan["retry"])
        self.assertEqual(8, plan["max_model_calls"])
        parent = [json.loads(line) for line in (FRESH / "ledger/ledger.jsonl").read_text().splitlines()]
        self.assertEqual(22, len(parent))
        self.assertEqual(30, len(parent) + plan["max_model_calls"])          # within the absolute cap 40
        self.assertLessEqual(len(parent) + plan["max_model_calls"], stage_inputs.ABSOLUTE_MAX_CALLS)
        answers = {answer["stem"]: answer for answer in summary["answers"]}
        self.assertEqual(15, len(answers))
        self.assertEqual("extract-blind-03-r2", answers["extract-blind-03"]["call"])
        recorded = {call["input_dir"]: call["sha256"] for call in
                    json.loads((FRESH / "in-extract/stage.json").read_text(encoding="utf-8"))["calls"]}
        for stem, answer in answers.items():
            self.assertEqual((recorded[stem], sha256(FRESH / "answers" / (stem + ".result.json"))),
                             (answer["prompt_sha256"], answer["answer_sha256"]), stem)
        self.assertEqual([("synthesis", "synthesis", sha256(FRESH / "answers/synthesis.result.json"))],
                         [(a["kind"], a["call"], a["answer_sha256"]) for a in summary["reduce_answers"]])
        # the verify prompts again, under the prompts in force
        code, verify, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis",
                                     str(FRESH / "answers/synthesis.result.json"), *args, "--out", str(self.root / "v"))
        self.assertEqual(0, code, stderr)
        written = {call["input_dir"]: (call["bytes"], call["sha256"]) for call in verify["calls"]}
        print("\nfresh run continuation: %s; verify prompts %s" % (plan["formula"], json.dumps(written)))
        self.assertEqual(self.VERIFY, written)
        before = {call["input_dir"]: call["sha256"] for call in
                  json.loads((FRESH / "in-verify/stage.json").read_text(encoding="utf-8"))["calls"]}
        self.assertTrue(all(written[name][1] != before[name] for name in before))
        for name in written:
            scope = json.loads((self.root / "v" / (name + ".scope.json")).read_text(encoding="utf-8"))
            self.assertEqual(21, len(scope["keys"]) + len(scope["listed"]), name)
        self.assertEqual(kept, {path: path.read_bytes() for path in FRESH.rglob("*") if path.is_file()})
        self.assertFalse((FRESH / "ledger/continued-by.json").exists())

if __name__ == "__main__":
    unittest.main()
