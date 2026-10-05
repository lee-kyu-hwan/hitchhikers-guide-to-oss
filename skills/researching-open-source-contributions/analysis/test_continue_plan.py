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
        self.assertEqual((0, 1, 1, 5, 1, 7), (plan["extract"], plan["synthesis"], plan["synthesis_groups"],
                                              plan["verify"], plan["final"], plan["max_model_calls"]))
        self.assertEqual(plan["shards"] * plan["lenses"], plan["continues"]["completed"]["extract"])
        self.assertEqual(7, summary["max_model_calls"])
        self.assertEqual(summary, json.loads((self.root / "c" / "stage.json").read_text(encoding="utf-8")))
        self.assertEqual(sorted(self.context.extract_stems()), [answer["stem"] for answer in summary["answers"]])

    def test_grouped_answers_plan_one_call_per_group_and_the_merge(self):
        pad = 1000
        cap = self.cap_for_groups(7, pad)
        paths = self.adopted(pad)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.continue_plan(paths)
        plan = summary["call_plan"]
        self.assertEqual((7, 8, 14), (plan["synthesis_groups"], plan["synthesis"], plan["max_model_calls"]))
        self.assertIn("7 group calls + 1 merge", plan["formula"])

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


EVIDENCE_R2 = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-period-analysis-eslint-r2-20261004T131350Z")


class RunR2Case(unittest.TestCase):
    """The eslint r2 run rebuilt from the same collections in a temporary directory (same corpus, same digest,
    same extract prompts); the evidence is only read."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        state = json.loads((EVIDENCE_R2 / "wf-run/state.json").read_text(encoding="utf-8"))
        inputs = state["stages"]["collect"]["generations"][0]["inputs"]
        adopt = []
        for name in sorted(key for key in inputs if key.startswith("corpus-")):
            unpacked = Path(inputs[name]["source_path"]).parent
            adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                      "--inventory", str(unpacked / "inventory.md")]
        self.run = str(self.root / "run")
        for argv in (("init", "--request", str(EVIDENCE_R2 / "wf-run/request.json")), ("adopt-collection", *adopt),
                     ("digest", "--max-bytes", "54000")):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)
        self.answers = sorted(str(path) for path in (EVIDENCE_R2 / "answers").glob("*.result.json"))
        self.extract = [item for path in self.answers for item in ("--extract-result", path)]


@unittest.skipUnless((EVIDENCE_R2 / "in-extract/stage.json").is_file() and (EVIDENCE_R2 / "answers").is_dir(),
                     "the 2026-10-04 eslint r2 run is only on the machine that made it")
class RunR2ContinuationTests(RunR2Case):
    """The eslint r2 run of 2026-10-04: 20 extract answers adopted under strict verification, then refused at
    synthesis-input for 7 groups over the old limit of 3."""

    def test_the_adopted_answers_continue_into_seven_groups_and_a_fourteen_call_plan(self):
        code, summary, stderr = stage("continue-plan", "--run-dir", self.run, "--extract-stage",
                                      str(EVIDENCE_R2 / "in-extract"), *self.extract, "--records", str(EVIDENCE_R2),
                                      "--out", str(self.root / "continue"))
        self.assertEqual(0, code, stderr)
        plan = summary["call_plan"]
        self.assertEqual(sha256(EVIDENCE_R2 / "in-extract/stage.json"), plan["continues"]["extract_stage_sha256"])
        self.assertEqual({"extract": 20}, plan["continues"]["completed"])
        self.assertEqual((7, 8, 5, 1, 14), (plan["synthesis_groups"], plan["synthesis"], plan["verify"], plan["final"],
                                             plan["max_model_calls"]))
        code, synthesis, stderr = stage("synthesis-input", "--run-dir", self.run, *self.extract,
                                        "--out", str(self.root / "synthesis"))
        self.assertEqual(0, code, stderr)
        self.assertEqual(7, len(synthesis["groups"]))
        self.assertEqual(8, synthesis["synthesis_calls"])
        self.assertTrue(all(call["bytes"] <= CAP for call in synthesis["calls"]))



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
        scope = json.loads((self.root / "f" / "final.scope.json").read_text(encoding="utf-8"))
        self.assertEqual((28, 38), (len(scope["keys"]), len(scope["quotes"])))


if __name__ == "__main__":
    unittest.main()
