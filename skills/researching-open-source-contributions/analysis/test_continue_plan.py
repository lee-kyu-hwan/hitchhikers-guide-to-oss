"""stage_inputs.py continue-plan: carry the adopted extract answers of a run into the reduce stages.

A run whose extract calls were all adopted under strict verification keeps those answers; only the
stages not yet run get a new call plan (synthesis groups + merge, verify, final). Each adopted answer
must be bound to the prompt its extract stage recorded, through the strict verdict and the picked-up
worker manifest. Every answer and record below is a hand-written stand-in; nothing here starts a
model, opens the network or reads a credential.

  /opt/homebrew/bin/python3 -B -m unittest docs/research/oss-e2e-2026-09-28/test_continue_plan.py
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
        # verify at most synthesis_max_candidates 3 (7 calls under the earlier 5, #248)
        self.assertEqual((0, 1, 1, 3, 1, 5), (plan["extract"], plan["synthesis"], plan["synthesis_groups"],
                                              plan["verify"], plan["final"], plan["max_model_calls"]))
        self.assertEqual([1], plan["synthesis_levels"])
        self.assertEqual(plan["shards"] * plan["lenses"], plan["continues"]["completed"]["extract"])
        self.assertEqual(5, summary["max_model_calls"])
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
        self.assertEqual((7, levels, sum(levels), sum(levels) + 2 + 1),
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

    def test_the_answers_must_be_the_whole_extract_stage(self):
        paths = self.adopted()
        _, stderr = self.continue_plan(paths[1:], out="short", code=2)
        self.assertIn("extract result must be exactly", stderr)
        self.assertFalse((self.root / "short").exists())


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
        self.assertEqual((0, 0, 0, 1, 1), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))
        self.assertEqual(plan["max_model_calls"], plan["extract"] + plan["synthesis"] + plan["verify"] + plan["final"])
        self.assertEqual(1, summary["max_model_calls"])
        self.assertEqual([("synthesis", "synthesis", "synthesis.result.json"), ("verify", "verify-c1", "verify-C1.result.json")],
                         [(a["kind"], a["call"], a["result_name"]) for a in summary["reduce_answers"]])
        self.assertEqual(summary, json.loads((self.root / "d" / "stage.json").read_text(encoding="utf-8")))

    def test_a_candidate_without_its_verify_answer_keeps_its_verify_call(self):
        plan = self.chain()[0]["call_plan"]
        self.assertEqual((0, 1, 1, 2), (plan["synthesis"], plan["verify"], plan["final"], plan["max_model_calls"]))
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
        with self.subTest("no synthesis answer"):
            _, stderr = self.chain(out="no-synthesis", code=2, synthesis=False)
            self.assertIn("synthesis answer", stderr)
        with self.subTest("reduce answers given with an extract stage"):
            args = [item for path in self.paths for item in ("--extract-result", path)]
            _, stderr = self.stage("continue-plan", "--run-dir", self.run, "--extract-stage", str(self.extract_stage),
                                   *args, "--synthesis-result", self.synthesis, "--records", str(self.records),
                                   "--out", str(self.root / "x-reduce"), code=2)
            self.assertIn("continue stage", stderr)


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
        self.assertEqual((0, 0, 0, 1, 1), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
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
        self.assertEqual((0, 1), (summary["call_plan"]["verify"], summary["max_model_calls"]))

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


EVIDENCE_R2 =Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-period-analysis-eslint-r2-20261004T131350Z")


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



EVIDENCE_R3 = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-period-analysis-eslint-r3-20261005T025628Z")


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
    verified quotes it fits."""

    def test_the_final_input_of_the_adopted_answers_is_within_the_cap(self):
        verify = [item for path in sorted((EVIDENCE_R3 / "answers").glob("verify-C*.result.json"))
                  for item in ("--verify-result", str(path))]
        code, final, stderr = stage("final-input", "--run-dir", self.run, "--synthesis",
                                    str(EVIDENCE_R3 / "answers/synthesis.result.json"), *self.extract, *verify,
                                    "--out", str(self.root / "f"))
        self.assertEqual(0, code, stderr)
        self.assertLessEqual(final["calls"][0]["bytes"], CAP)
        self.assertEqual({"listed": 38, "no_quote": 0}, final["final_quotes"])
        self.assertEqual((44108, "fc66309c06a61ad65f88198f3aae1e333dc26610c7648297539942caaf051a06"),
                         (final["calls"][0]["bytes"], final["calls"][0]["sha256"]))     # the r6 final input (README 3.1f)
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
        self.assertEqual((0, 0, 0, 1, 1), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))


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


EVIDENCE_R6 = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-period-analysis-eslint-r6-20261005T043448Z")


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
        self.assertEqual((0, 0, 0, 1, 1), (plan["extract"], plan["synthesis"], plan["verify"], plan["final"],
                                           plan["max_model_calls"]))
        recorded = json.loads((EVIDENCE_R6 / "in-continue/stage.json").read_text(encoding="utf-8"))["reduce_answers"]
        self.assertEqual([dict(answer, ancestor=1) for answer in recorded], summary["reduce_answers"])
        self.assertEqual({path: path.read_bytes() for path in kept}, kept)
        self.assertFalse((EVIDENCE_R6 / "continued-by.json").exists() or (EVIDENCE_R3 / "continued-by.json").exists())


if __name__ == "__main__":
    unittest.main()
