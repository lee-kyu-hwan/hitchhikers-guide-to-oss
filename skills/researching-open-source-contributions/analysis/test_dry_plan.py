"""stage_inputs.py dry-plan, the stage output limits and the hierarchical merge (#248).

Before any model call, the worst-case plan sizes every stage input with every answer at the stage output limits
of run_limits.json (synthesis_max_candidates, candidate_max_bytes, extract_answer_max_bytes, max_rejected,
verify_answer_max_bytes), counts the reduce levels (synthesis groups, then merges of merges until one call) and
the model calls, and refuses a run whose stage input can pass 65,536 bytes or whose calls can pass the absolute
cap 40, with repository bundles that would pass. The merge itself is split into another level whenever its group
answers do not fit one prompt; there is no fixed group limit. Every answer below is a hand-written stand-in;
nothing here starts a model, opens the network or reads a credential.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_dry_plan.py
"""

from __future__ import annotations

import json
from pathlib import Path
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_reduce_inputs as reduce  # noqa: E402  (installs the offline guards first)
import test_stage_inputs as base  # noqa: E402

stage_inputs, stage, workflow, fx, CAP = reduce.stage_inputs, reduce.stage, reduce.workflow, reduce.fx, reduce.CAP


class LimitsTests(unittest.TestCase):
    def test_run_limits_hold_the_stage_output_limits_and_no_group_limit(self):
        limits = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
        self.assertEqual({"synthesis_max_candidates": 3, "candidate_max_bytes": 1024, "extract_answer_max_bytes": 7680,
                          "max_rejected": 20, "verify_answer_max_bytes": 5120, "max_patterns": 5},
                         {key: limits[key] for key in ("synthesis_max_candidates", "candidate_max_bytes",
                                                       "extract_answer_max_bytes", "max_rejected",
                                                       "verify_answer_max_bytes", "max_patterns")})
        self.assertNotIn("max_synthesis_groups", limits)
        self.assertEqual(limits["synthesis_max_candidates"], stage_inputs.MAX_CANDIDATES)
        self.assertEqual(limits["candidate_max_bytes"], stage_inputs.CANDIDATE_MAX_BYTES)
        self.assertEqual(limits["extract_answer_max_bytes"], stage_inputs.EXTRACT_ANSWER_MAX_BYTES)

    def test_every_synthesis_prompt_states_the_limits_its_answer_is_checked_against(self):
        for name in ("synthesize.md", "synthesize-reduced.md", "synthesize-merge.md"):
            with self.subTest(name):
                text = stage_inputs.template(name)
                self.assertIn("후보는 **최대 3개**, `dropped` 는 최대 20개다.", text)
                self.assertIn("3개를 넘으면 가장 근거가 강한 3개만 후보로 두고", text)
                self.assertIn("1024 바이트 이하다", text)
                self.assertNotIn("{{", text)

    def test_the_earlier_limits_render_the_templates_as_they_were(self):
        base.use_limits(self, base.EARLIER_LIMITS)
        for name in ("synthesize.md", "synthesize-reduced.md", "synthesize-merge.md"):
            with self.subTest(name):
                text = stage_inputs.template(name)
                self.assertIn("후보는 **최대 5개**, `dropped` 는 최대 20개다. 넘으면 답 전체가 거부된다(잘라내지 않는다). "
                              "5개를 넘으면 가장 근거가 강한 5개만 후보로 두고 나머지는 `dropped` 에 넣는다.\n\n출력:", text)
                self.assertNotIn("바이트 이하", text)

    def test_an_unknown_limit_in_a_template_is_refused(self):
        with self.assertRaises(stage_inputs.StageError):
            stage_inputs.render_limits("- 후보는 최대 {{no_such_limit}}개다.")
        self.assertEqual("a\nb", stage_inputs.render_limits("a\nb"))

    def test_answer_bytes_is_the_indent_one_json_a_prompt_block_holds(self):
        answer = {"candidates": [{"id": "C1", "statement": "가", "cited_prs": ["a#1", "b#2"]}], "dropped": []}
        body = stage_inputs.block("x", answer).split("=== x ===\n", 1)[1].rsplit("\n=== x 끝 ===", 1)[0]
        self.assertEqual(len(body.encode("utf-8")), stage_inputs.answer_bytes(answer))


class DryPlanTests(reduce.ReducedCase):
    """The fixture run of six PRs in three shards (fifteen extract prompts)."""

    def test_dry_plan_writes_nothing_and_gives_the_plan_extract_inputs_records(self):
        before = sorted(path.name for path in self.root.iterdir())
        plan, _ = self.stage("dry-plan", "--run-dir", self.run)
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))
        self.assertTrue(plan["ok"])
        self.assertEqual("dry-plan", plan["stage"])
        self.assertEqual(["extract", "synthesis", "merge", "verify", "final"], [s["stage"] for s in plan["stages"]])
        self.assertTrue(all(s["worst_bytes"] <= CAP for s in plan["stages"]))
        self.assertEqual(plan["total_calls"], plan["call_plan"]["max_model_calls"])
        self.assertLessEqual(plan["total_calls"], stage_inputs.ABSOLUTE_MAX_CALLS)
        self.assertNotIn("split", plan)
        summary, _ = self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"))
        self.assertEqual(plan["call_plan"], summary["call_plan"])
        self.assertEqual(plan["stages"], summary["worst_case"]["stages"])

    def test_a_plan_over_the_absolute_cap_is_refused_before_any_model_call(self):
        with mock.patch.object(stage_inputs, "ABSOLUTE_MAX_CALLS", 20):
            code, stdout, stderr = fx.workflow.run_main(stage_inputs.main, ["dry-plan", "--run-dir", self.run])
            self.assertEqual(2, code)
            plan = json.loads(stdout)                   # the plan is printed even when it does not pass
            self.assertFalse(plan["ok"])
            self.assertIn("over the absolute cap 20", stderr)
            self.assertEqual(plan["refusals"], [reason for reason in plan["refusals"] if "calls" in reason])
            _, stderr = self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"), code=2)
            self.assertIn("the worst-case plan does not pass", stderr)
            self.assertFalse((self.root / "x").exists())

    def test_a_stage_input_over_the_cap_at_the_limits_is_refused(self):
        with mock.patch.object(stage_inputs, "VERIFY_ANSWER_MAX_BYTES", 40000):
            _, stderr = self.stage("dry-plan", "--run-dir", self.run, code=2)
            self.assertIn("final:", stderr)
            self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"), code=2)
            self.assertFalse((self.root / "x").exists())

    def test_a_part_of_every_repository_plans_as_the_whole_run(self):
        whole = stage_inputs.worst_case_plan(stage_inputs.plan_inputs(self.context))
        part = stage_inputs.worst_case_plan(stage_inputs.part_inputs(self.context, whole["repositories"]))
        for key in ("ok", "mode", "records", "stages", "synthesis_levels", "total_calls"):
            self.assertEqual(whole[key], part[key], key)

    def test_the_levels_and_calls_grow_with_the_answers_allowed(self):
        plans = []
        for answer in (2000, 4000, 7680):
            with mock.patch.object(stage_inputs, "EXTRACT_ANSWER_MAX_BYTES", answer):
                plans.append(stage_inputs.worst_case_plan(stage_inputs.plan_inputs(self.context)))
        groups = [plan["synthesis_levels"][0] for plan in plans]
        self.assertEqual(sorted(groups), groups)
        self.assertLess(groups[0], groups[-1])
        self.assertEqual(sorted(plan["total_calls"] for plan in plans), [plan["total_calls"] for plan in plans])


class ReviewFixTests(reduce.ReducedCase):
    """Budgets after the extract calls follow the worst-case plan, a part on the digest path may be proposed, and
    the plan reads reference repositories the way check_extraction does."""

    def padded_report(self, real):
        def padded(report):
            compact = real(report)
            if "clean_results" not in report:          # a group's subset report (subset_report) stays as it is
                return compact
            filler = [{"pr": key, "pr_id": "PR-%03d" % n, "quote": "q" * 290}
                      for n, key in enumerate(list(self.context.keymap["sections"]) * 40)]
            return dict(compact, verified_evidence=compact["verified_evidence"] + filler)
        return padded

    def test_a_planned_run_is_not_refused_after_its_extract_calls_by_the_every_quote_projection(self):
        real = stage_inputs.compact_report
        extract = self.extract_args()
        with mock.patch.object(stage_inputs, "compact_report", self.padded_report(real)):
            summary, _ = self.stage("synthesis-input", "--run-dir", self.run, *extract, "--out", str(self.root / "s"))
            self.assertGreater(summary["final_projection_bytes"], CAP)       # recorded, not a refusal
            base.use_limits(self, base.EARLIER_LIMITS)                        # without the limits it still refuses
            _, stderr = self.stage("synthesis-input", "--run-dir", self.run, *extract, "--out", str(self.root / "e"),
                                   code=2)
        self.assertIn("PR index and the verified extract quotes alone", stderr)

    def test_verify_inputs_budgets_the_quotes_the_final_input_will_hold(self):
        keys = list(self.context.keymap["sections"])[:2]
        synthesis = self.synthesis_answer(keys)
        summary, _ = self.stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *self.extract_args(),
                                "--out", str(self.root / "v"))
        paths = self.extract_args()[1::2]
        extract = stage_inputs.load_results(paths, self.context.extract_stems(), "extract result")
        report = stage_inputs.check_quotes(self.context, paths)
        candidates = stage_inputs.parse_synthesis(self.context, synthesis)
        kept, missing = stage_inputs.final_quotes(self.context, candidates, extract, {}, report)
        quotes = {"totals": report["totals"], "verified_evidence": kept, "no_quote": missing}
        self.assertEqual(len(stage_inputs.final_prompt_text(self.context, candidates, {}, quotes).encode("utf-8")),
                         summary["final_budget"]["base_bytes"])

    def test_a_part_small_enough_for_the_digest_path_may_be_proposed(self):
        plan = {"ok": False, "mode": "digest-in-final", "stages": [{"stage": "extract", "ok": True}], "total_calls": 10}
        self.assertTrue(stage_inputs.runnable(plan))
        self.assertFalse(stage_inputs.runnable(dict(plan, total_calls=41)))
        self.assertFalse(stage_inputs.runnable(dict(plan, mode="extraction-only")))
        self.assertTrue(stage_inputs.runnable({"ok": True}))

    def test_the_plan_reads_reference_repositories_in_lower_case(self):
        self.context.references = ["Acme/Plugin-B"]
        self.context.references_normalized = self.context.siblings.validate_corpus.normalize_reference_repositories(
            self.context.references)
        self.assertEqual(("acme/plugin-b",), tuple(self.context.references_normalized))
        seen = []
        real = self.context.siblings.validate_corpus.quotable_texts
        with mock.patch.object(self.context.siblings.validate_corpus, "quotable_texts",
                               lambda record, references=(): seen.append(tuple(references)) or real(record, references)):
            stage_inputs.quote_bounds(self.context)
        self.assertEqual({("acme/plugin-b",)}, set(seen))


class DigestInFinalPlanTests(unittest.TestCase):
    """A one-shard run keeps the digest in its final prompt: that prompt carries every quote, which the output
    limits do not bound, so its worst-case plan does not pass; extract-inputs still records its 10-call plan."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = base.make_run(self, self.root)

    def test_the_digest_in_final_path_is_planned_but_not_guaranteed(self):
        code, stdout, stderr = fx.workflow.run_main(stage_inputs.main, ["dry-plan", "--run-dir", self.run])
        self.assertEqual(2, code)
        self.assertIn("digest-in-final", stderr)
        self.assertNotIn("split", json.loads(stdout))          # nothing to split on the digest path
        code, summary, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"))
        self.assertEqual(0, code, stderr)
        self.assertEqual(10, summary["max_model_calls"])
        self.assertNotIn("worst_case", summary)


class SyntheticCorpusTests(unittest.TestCase):
    """S2: synthetic corpora of 21 to 100 PRs (rows, quotes and shards shaped like the 2026-10-04 collections: digest
    keys like ``eslint#21183``, a 322-byte widest quote, sections of about 7.3 KB in 54,000-byte shards). Their plans
    at 21 and 28 PRs are those of the real bundles (PeriodCorpusPlanTests)."""

    @staticmethod
    def corpus(records: int) -> stage_inputs.PlanInputs:
        repositories = ["eslint/eslint", "typescript-eslint/typescript-eslint", "eslint/markdown", "eslint/css"]
        rows, quotes = [], {}
        for number in range(records):
            repository = repositories[number % len(repositories)]
            key = "%s#%d" % (repository.split("/")[1], 20000 + number)
            rows.append({"key": key, "pr_id": "PR-%03d" % (number + 1), "repository": repository,
                         "number": 20000 + number, "title": "docs: fix the wording of rule %d" % number,
                         "outcome": "merged", "files": 2, "url": "https://github.com/%s/pull/%d" % (repository, number),
                         "partial_categories": []})
            quotes[key] = (322, 317)
        shards = -(-records * 7300 // 54000)
        lenses = ("blind", "correctness", "maintenance", "review", "structure")
        stems = sorted("extract-%s%s" % (lens, "" if shards == 1 else "-%02d" % n)
                       for lens in lenses for n in range(1, shards + 1))
        return stage_inputs.PlanInputs(mode="extraction-only", shards=shards, stems=stems, rows=rows, quotes=quotes,
                                       extract_bytes=58700, verify_bytes=36000)

    def test_more_pull_requests_need_more_levels_and_a_hundred_are_refused_before_any_model_call(self):
        plans = {records: stage_inputs.worst_case_plan(self.corpus(records)) for records in (21, 28, 40, 100)}
        self.assertEqual((True, [5, 2, 1], 27), (plans[21]["ok"], plans[21]["synthesis_levels"], plans[21]["total_calls"]))
        self.assertEqual((True, [7, 3, 1], 35), (plans[28]["ok"], plans[28]["synthesis_levels"], plans[28]["total_calls"]))
        self.assertGreater(plans[40]["levels"], plans[28]["levels"])          # 15 groups, then 8, 4, 2 and 1 merges
        self.assertEqual([15, 8, 4, 2, 1], plans[40]["synthesis_levels"])
        self.assertIn("calls: 64 model calls at the limits, over the absolute cap 40", plans[40]["refusals"])
        large = plans[100]
        self.assertFalse(large["ok"])
        self.assertGreater(large["total_calls"], 40)
        self.assertTrue(any("another level would merge nothing" in reason for reason in large["refusals"]))
        self.assertTrue(any("over the absolute cap 40" in reason for reason in large["refusals"]))
        for plan in plans.values():
            self.assertEqual(plan["total_calls"], plan["call_plan"]["extract"] + sum(plan["synthesis_levels"])
                             + plan["call_plan"]["verify"] + 1)
            for level in plan["stages"]:
                if level["stage"] in ("synthesis", "merge") and level["ok"]:
                    self.assertLessEqual(level["worst_bytes"], CAP)


class HierarchicalMergeTests(reduce.ReducedCase):
    """Seven groups whose answers do not fit one merge prompt: a second level of merge calls, then the last merge."""

    PAD = 1000

    def setUp(self):
        super().setUp()
        cap = self.cap_for_groups(7, self.PAD)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            self.groups, _ = self.stage("synthesis-input", "--run-dir", self.run, *self.extract_args(self.PAD),
                                        "--out", str(self.root / "g"))
        self.assertEqual(7, len(self.groups["groups"]))
        self.group_results = [self.synthesis_answer(sorted(group["keys"])[:2], name=group["input_dir"] + ".result.json")
                              for group in self.groups["groups"]]

    def merge(self, stage_dir, results, out, code=0):
        args = [item for path in results for item in ("--group-result", path)]
        return self.stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage", str(self.root / stage_dir),
                          *args, *self.extract_args(self.PAD), "--out", str(self.root / out), code=code)

    def single_merge_bytes(self):
        merged, _ = self.merge("g", self.group_results, "one")
        return merged["calls"][0]["bytes"]

    def test_two_levels_then_the_last_merge_and_the_quotes_stay_with_their_own_groups(self):
        cap = self.single_merge_bytes() - 1
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            level, _ = self.merge("g", self.group_results, "l2")
            self.assertEqual((2, True), (level["level"], level["merge_required"]))
            names = [call["input_dir"] for call in level["calls"]]
            self.assertEqual(["merge-02-01", "merge-02-02"], names)
            for group in level["groups"]:
                text = self.prompt("l2", group["input_dir"], "task.txt")
                self.assertIn("합침 2층 묶음", text)
                for member in group["members"]:
                    self.assertIn("그룹 종합 " + member, text)
                stems = [stem for g in self.groups["groups"] if g["input_dir"] in group["members"] for stem in g["stems"]]
                self.assertEqual(stems, group["stems"])
                scope = json.loads((self.root / "l2" / (group["input_dir"] + ".scope.json")).read_text(encoding="utf-8"))
                self.assertEqual(group["keys"], sorted(scope["keys"]))
            # the level-2 answers, each citing within its own input, merge in one last call
            answers = [self.synthesis_answer(group["keys"][:2], name=group["input_dir"] + ".result.json")
                       for group in level["groups"]]
            last, _ = self.merge("l2", answers, "l3")
        self.assertEqual(["synthesis"], [call["input_dir"] for call in last["calls"]])
        self.assertEqual({"stage": "synthesis-merge", "level": 3, "groups": 2, "synthesis_calls": 7 + 2 + 1},
                         {key: last[key] for key in ("stage", "level", "groups", "synthesis_calls")})
        self.assertNotIn("merge_required", last)
        text = self.prompt("l3", "synthesis", "task.txt")
        self.assertNotIn("=== 범위 ===", text)
        self.assertIn("그룹 종합 merge-02-01", text)
        # a level-2 answer citing a key its own input did not hold is refused
        outside = next(key for key in self.context.keymap["sections"] if key not in level["groups"][0]["keys"])
        bad = self.synthesis_answer([level["groups"][0]["keys"][0], outside], name="merge-02-01.result.json")
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            _, stderr = self.merge("l2", [bad, answers[1]], "bad", code=2)
        self.assertIn("cites keys its own input did not hold", stderr)

    def test_a_level_that_would_merge_nothing_is_refused(self):
        groups = self.groups["groups"]
        candidates = {group["input_dir"]: stage_inputs.parse_synthesis(self.context, path)
                      for group, path in zip(groups, self.group_results)}
        paths = self.extract_args(self.PAD)[1::2]
        results = stage_inputs.load_results(paths, self.context.extract_stems(), "extract result")
        report = stage_inputs.check_quotes(self.context, paths)

        def size(members):
            names = [group["input_dir"] for group in members]
            return len(stage_inputs.merge_text(self.context, members, candidates, results, report,
                                               stage_inputs.merge_note(99, 99, 99, names))[0].encode("utf-8"))

        alone = max(size([group]) for group in groups)
        pairs = min(size(groups[index:index + 2]) for index in range(len(groups) - 1))
        self.assertLess(alone, pairs)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", alone):        # each fits alone, no two together
            _, stderr = self.merge("g", self.group_results, "none", code=2)
        self.assertIn("another level would merge nothing", stderr)
        self.assertFalse((self.root / "none").exists())

    def test_the_real_levels_never_need_more_calls_than_the_worst_case_plan(self):
        """Random answers within the limits: the real groups and merge levels are never more than planned."""
        limits = {"EXTRACT_ANSWER_MAX_BYTES": 3000, "CANDIDATE_MAX_BYTES": 700, "MAX_CANDIDATES": 3}
        cap = 14000
        base.use_limits(self, limits)
        generator = random.Random(248)
        seen = []
        for trial in range(4):
            with self.subTest(trial=trial), mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
                pads = [generator.randrange(300, 1300) for _ in self.context.extract_stems()]
                paths = [self.extract_result(stem, pad) for stem, pad in zip(self.context.extract_stems(), pads)]
                extract = [item for path in paths for item in ("--extract-result", path)]
                inputs = stage_inputs.PlanInputs(mode="extraction-only", shards=3, stems=self.context.extract_stems(),
                                                 rows=self.context.pr_index(),
                                                 quotes=stage_inputs.quote_bounds(self.context),
                                                 extract_bytes=0, verify_bytes=0)
                planned, refusals = stage_inputs.synthesis_levels(inputs)
                self.assertEqual([], refusals)
                out = "t%d" % trial
                groups, _ = self.stage("synthesis-input", "--run-dir", self.run, *extract, "--out", str(self.root / out))
                self.assertLessEqual(len(groups["calls"]), planned[0]["calls"])
                real, stage_dir = [len(groups["calls"])], out
                previous = groups
                level = 0
                while previous.get("merge_required"):
                    level += 1
                    answers = []
                    for group in previous["groups"]:
                        cited = generator.sample(sorted(group["keys"]), min(len(group["keys"]), generator.randrange(2, 7)))
                        room = (700 - stage_inputs.answer_bytes({"id": "C1", "statement": "", "cited_prs": cited})) // 3
                        candidates = [{"id": "C%d" % n, "statement": "가" * generator.randrange(room // 2, room + 1),
                                       "cited_prs": cited} for n in range(1, 4)]
                        answers.append(self.write(group["input_dir"] + ".result.json",
                                                  {"candidates": candidates, "dropped": []}))
                    args = [item for path in answers for item in ("--group-result", path)]
                    next_dir = "%s-m%d" % (out, level)
                    previous, _ = self.stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage",
                                             str(self.root / stage_dir), *args, *extract, "--out", str(self.root / next_dir))
                    real.append(len(previous["calls"]))
                    stage_dir = next_dir
                worst, _ = stage_inputs.synthesis_levels(inputs, real[0]) if real[0] > 1 else ([], [])
                self.assertLessEqual(sum(real), real[0] + sum(level["calls"] for level in worst))
                self.assertLessEqual(sum(real), sum(level["calls"] for level in planned))
                seen.append(real)
        self.assertTrue(any(len(real) > 2 for real in seen), seen)          # some trial merged over two levels


class PackingTests(reduce.ReducedCase):
    def test_a_lens_packing_over_the_bound_gives_way_to_packing_one_by_one_when_that_needs_fewer(self):
        pad = 1000
        paths = self.extract_args(pad)[1::2]
        results = stage_inputs.load_results(paths, self.context.extract_stems(), "extract result")
        report = stage_inputs.check_quotes(self.context, paths)
        stems = self.context.extract_stems()

        def size(group):
            return len(stage_inputs.synthesis_group_text(self.context, results, report, group,
                                                         stage_inputs.group_note(99, 99, group)).encode("utf-8"))

        cap = max(size(stems[index:index + 4]) for index in range(0, len(stems), 4))   # four answers fit, not five
        self.assertGreater(min(size(stems[index:index + 5]) for index in range(len(stems) - 4)), cap)

        def fits(group):
            return size(group) <= cap

        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            by_lens = stage_inputs.plan_synthesis_groups(self.context, results, report, stems)
            one_by_one = stage_inputs.next_fit([[stem] for stem in stems], fits)
            # a lens (three shards) fits whole but two do not: five groups of one lens, against four of four answers
            self.assertEqual(5, len(by_lens))
            self.assertEqual([4, 4, 4, 3], [len(group) for group in one_by_one])
            with mock.patch.object(stage_inputs, "synthesis_group_bound", lambda rows, names: 4):
                self.assertEqual(one_by_one, stage_inputs.plan_synthesis_groups(self.context, results, report, stems))
            with mock.patch.object(stage_inputs, "synthesis_group_bound", lambda rows, names: 5):
                self.assertEqual(by_lens, stage_inputs.plan_synthesis_groups(self.context, results, report, stems))


E = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-period-20261004T055447Z")
PERIOD_REQUEST = HERE.parents[3] / "docs/research/2026-10-04-period-research/analysis-eslint-request.json"
OWNERS = {"eslint": "eslint/eslint", "tseslint": "typescript-eslint/typescript-eslint", "markdown": "eslint/markdown",
          "css": "eslint/css"}


def period_run(test, root: Path, names: tuple) -> str:
    """A run of the 2026-10-04 period collections of these repositories (short names), rebuilt in a temporary
    directory as the eslint analysis run was (its request, digest --max-bytes 54000); the evidence is only read."""
    runs = sorted(path for path in E.iterdir() if path.is_dir() and path.name.startswith("R")
                  and path.name.split("-")[1] in names)
    request = json.loads(PERIOD_REQUEST.read_text(encoding="utf-8"))
    request["source"]["repositories"] = [OWNERS[name] for name in names]
    request["budget"]["total_api_requests"] = 1000          # the period collection's approved total (440 used)
    for key in ("discover_request_budget", "locus_request_budget"):
        request["target"][key] = min(request["target"][key], fx.workflow.CANDIDATE_READS_MAX_REQUEST_BUDGET)
    root.mkdir(parents=True)
    (root / "request.json").write_text(json.dumps(request), encoding="utf-8")
    adopt = []
    for run in runs:
        unpacked = run / "unpacked"
        adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                  "--inventory", str(unpacked / "inventory.md")]
    out = str(root / "run")
    for argv in (("init", "--request", str(root / "request.json")), ("adopt-collection", *adopt),
                 ("digest", "--max-bytes", "54000")):
        code, stdout, stderr = workflow(argv[0], "--run-dir", out, *argv[1:])
        test.assertEqual(code, 0, stdout + stderr)
    return out


@unittest.skipUnless(all((E / name / "unpacked/corpus.json").is_file() for name in
                         ("R01-eslint-0706-M", "R07-tseslint-0706-M", "R13-markdown-0706-M", "R19-css-0706-M")),
                     "the 2026-10-04 period collections are only on the machine that made them")
class PeriodCorpusPlanTests(unittest.TestCase):
    """S1 and S4: the dry plans of the 2026-10-04 period collection (49 PRs: 40 merged, 9 closed unmerged) and of its
    two repository bundles, the eslint one (28 PRs, the r2-r6 analysis) and the typescript-eslint one (21 PRs, the next
    analysis). Recorded in README 3.1g."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def plan(self, name, names, code):
        run = period_run(self, self.root / name, names)
        got, plan, stderr = base.fx.workflow.run_main(stage_inputs.main, ["dry-plan", "--run-dir", run])
        self.assertEqual(code, got, stderr)
        return run, json.loads(plan)

    def check_passes(self, plan, records, shards, levels, calls):
        self.assertTrue(plan["ok"], plan["refusals"])
        self.assertEqual((records, shards, levels, calls),
                         (plan["records"], plan["call_plan"]["shards"], plan["synthesis_levels"], plan["total_calls"]))
        self.assertTrue(all(stage["worst_bytes"] <= CAP for stage in plan["stages"]))
        self.assertLessEqual(plan["total_calls"], 40)

    def test_s1_the_eslint_bundle_passes_within_forty_calls(self):
        run, plan = self.plan("eslint", ("eslint", "markdown", "css"), 0)
        self.check_passes(plan, 28, 4, [7, 3, 1], 35)
        code, summary, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))
        self.assertEqual(0, code, stderr)
        self.assertEqual(plan["call_plan"], summary["call_plan"])

    def test_s4_the_typescript_eslint_bundle_passes(self):
        _, plan = self.plan("tseslint", ("tseslint",), 0)
        self.check_passes(plan, 21, 3, [5, 2, 1], 27)

    def test_s4_the_whole_period_collection_is_refused_and_split_by_owner(self):
        run, plan = self.plan("all", ("eslint", "tseslint", "markdown", "css"), 2)
        self.assertFalse(plan["ok"])
        self.assertEqual((49, 6, 64), (plan["records"], plan["call_plan"]["shards"], plan["total_calls"]))
        self.assertEqual([15, 8, 4, 2, 1], plan["synthesis_levels"])
        self.assertEqual(["final: 75854 bytes at the limits (> 65536)",
                          "calls: 64 model calls at the limits, over the absolute cap 40"], plan["refusals"])
        self.assertEqual([(["eslint/css", "eslint/eslint", "eslint/markdown"], 28, [7, 3, 1], 35, True),
                          (["typescript-eslint/typescript-eslint"], 21, [5, 2, 1], 27, True)],
                         [(b["repositories"], b["records"], b["synthesis_levels"], b["total_calls"], b["ok"])
                          for b in plan["split"]])
        code, _, stderr = stage("extract-inputs", "--run-dir", run, "--out", str(self.root / "x"))
        self.assertEqual(2, code)
        self.assertIn("the worst-case plan does not pass", stderr)
        self.assertFalse((self.root / "x").exists())


if __name__ == "__main__":
    unittest.main()
