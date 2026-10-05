"""stage_inputs.py for a corpus whose digest is more than one shard (or too large for the final prompt).

The reduce stages (synthesis, verify, final) then take the extraction answers and a PR index, never
the whole digest: every prompt stays at or under 65,536 bytes, and a synthesis that does not fit one
call is split in groups and merged. Every answer below is a hand-written stand-in for a model call;
nothing here starts a model, opens the network or reads a credential.

  /opt/homebrew/bin/python3 -B -m unittest docs/research/oss-e2e-2026-09-28/test_reduce_inputs.py
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_stage_inputs as base  # noqa: E402  (installs the offline guards first)

fx, stage_inputs, stage, workflow, LENSES = base.fx, base.stage_inputs, base.stage, base.workflow, base.LENSES
START, MID, END = "2026-09-01T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-08T00:00:00Z"
CAP = stage_inputs.PROMPT_MAX_BYTES


def make_big_run(test, root: Path, digest_args=()) -> str:
    """Six pull requests in three windows and both outcomes; a digest of about 5,000 bytes."""
    produced = base.collect(root / "collected")
    request = fx.base_request(target=None, source__outcome=["merged", "closed-unmerged"])
    request["source"].update(max_per_repository=3, request_budget=60)
    request["budget"]["total_api_requests"] = 200
    (root / "request.json").write_text(json.dumps(request), encoding="utf-8")
    bundles = [
        fx.derive_window(produced, root / "w1", "w1", interval=(START, MID), outcome="merged", keep=[0, 1]),
        fx.derive_window(produced, root / "w2", "w2", interval=(MID, END), outcome="merged", keep=[0, 1], shift=100),
        fx.derive_window(produced, root / "w3", "w3", interval=(START, END), outcome="closed-unmerged", keep=[0, 1], shift=200),
    ]
    run = str(root / "run")
    adopt = [item for b in bundles for item in ("--corpus", str(b["corpus"]), "--manifest", str(b["manifest"]))]
    for argv in (("init", "--request", str(root / "request.json")), ("adopt-collection", *adopt), ("digest", *digest_args)):
        code, _, stderr = workflow(argv[0], "--run-dir", run, *argv[1:])
        test.assertEqual(code, 0, stderr)
    return run


class ReducedCase(unittest.TestCase):
    SHARD_BYTES = "2000"          # six sections of about 870 bytes: three shards

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = make_big_run(self, self.root, ("--max-bytes", self.SHARD_BYTES))
        self.context = stage_inputs.Context(self.run)
        self.answers = self.root / "answers"
        self.answers.mkdir()

    # -- stand-in answers ----------------------------------------------------

    def key_of_shard(self):
        """Digest keys by shard file name."""
        shards = {}
        for key, section in self.context.keymap["sections"].items():
            shards.setdefault(section["shard"], []).append(key)
        return shards

    def quote_of(self, key):
        number = self.context.keymap["sections"][key]["url"].rsplit("/", 1)[1]
        return fx.PULLS[100 + int(number) % 100]["quote"]

    def extract_result(self, stem, pad=0):
        lens = stem.split("-")[1]
        shard = None if self.context.shards == 1 else "digest-%s.md" % stem.rsplit("-", 1)[1]
        keys = self.key_of_shard()[shard or "digest.md"]
        answer = {"lens": lens, "patterns": [], "rejected": [], "observations": [
            {"pr": key, "note": fx.SYNTHETIC + " " + "n" * pad, "quote": self.quote_of(key)} for key in keys]}
        path = self.answers / (stem + ".result.json")
        path.write_text(json.dumps(answer), encoding="utf-8")
        return str(path)

    def extract_args(self, pad=0):
        return [item for stem in self.context.extract_stems() for item in ("--extract-result", self.extract_result(stem, pad))]

    def write(self, name, document):
        path = self.answers / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def synthesis_answer(self, keys=None, name="synthesis.result.json", candidate="C1"):
        keys = keys or list(self.context.keymap["sections"])[:2]
        return self.write(name, {"candidates": [{"id": candidate, "statement": fx.SYNTHETIC + " backoff",
                                                 "cited_prs": keys, "found_by": "blind"}], "dropped": []})

    def group_size(self, stems_of_lenses, pad, note=True, paths=None):
        """The synthesis prompt for whole lenses (every shard of each), measured the way the stage measures it."""
        paths = paths or self.extract_args(pad)[1::2]
        results = stage_inputs.load_results(paths, self.context.extract_stems(), "extract result")
        report = stage_inputs.check_quotes(self.context, [path for path, _ in results.values()])
        stems = [stem for stem in self.context.extract_stems() if stem.split("-")[1] in stems_of_lenses]
        return len(stage_inputs.synthesis_group_text(
            self.context, results, report, stems, stage_inputs.group_note(99, 99, stems) if note else None).encode("utf-8"))

    def cap_for_groups(self, wanted, pad):
        """The smallest prompt cap (in 10-byte steps) at which the padded answers make exactly ``wanted`` groups."""
        paths = self.extract_args(pad)[1::2]
        results = stage_inputs.load_results(paths, self.context.extract_stems(), "extract result")
        report = stage_inputs.check_quotes(self.context, [path for path, _ in results.values()])
        stems = self.context.extract_stems()
        for cap in range(self.group_size([], pad), self.group_size(list(LENSES), pad) + 10, 10):
            with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap), \
                    mock.patch.object(stage_inputs, "MAX_SYNTHESIS_GROUPS", 99):
                try:
                    if len(stage_inputs.plan_synthesis_groups(self.context, results, report, stems)) == wanted:
                        return cap
                except stage_inputs.StageError:
                    continue
        self.fail("no cap makes %d groups" % wanted)

    def stage(self, *argv, code=0):
        got, summary, stderr = stage(*argv)
        self.assertEqual(code, got, stderr)
        return summary, stderr

    def prompt(self, *parts) -> str:
        return Path(self.root, *parts).read_text(encoding="utf-8")

    def sizes(self, summary):
        return {call["input_dir"]: call["bytes"] for call in summary["calls"]}


class ModeTests(ReducedCase):
    def test_a_sharded_digest_takes_the_extraction_only_path_and_a_small_one_keeps_the_digest_path(self):
        self.assertTrue(self.context.reduced)
        self.assertEqual("extraction-only", self.context.mode)
        small = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, small, ignore_errors=True)
        self.assertFalse(stage_inputs.Context(base.make_run(self, small)).reduced)

    def test_a_single_shard_digest_too_large_for_the_final_prompt_is_also_extraction_only(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with mock.patch.object(stage_inputs, "FINAL_RESERVE_BYTES", CAP - 1000):
            self.assertTrue(stage_inputs.Context(make_big_run(self, root, ("--max-bytes", "40000"))).reduced)

    def test_the_pr_index_holds_short_meta_and_no_text(self):
        index = self.context.pr_index()
        self.assertEqual(6, len(index))
        row = index[0]
        self.assertEqual({"key", "pr_id", "repository", "number", "title", "outcome", "files", "url",
                          "partial_categories"}, set(row))
        self.assertEqual({"merged", "closed-unmerged"}, {r["outcome"] for r in index})
        self.assertIsInstance(row["number"], int)
        self.assertLess(len(json.dumps(index)), 3000)
        self.assertNotIn(fx.PULLS[101]["patch"][:30], json.dumps(index))


class PlanTests(ReducedCase):
    def test_the_call_limit_counts_the_synthesis_groups_it_may_need(self):
        plan = stage_inputs.model_call_plan(3, 5, 5, synthesis_calls=4)
        self.assertEqual(3 * 5 + 4 + 5 + 1, plan["max_model_calls"])
        self.assertEqual(4, plan["synthesis"])
        self.assertEqual(12, stage_inputs.model_call_plan(1, 5, 5)["max_model_calls"])

    def test_extract_inputs_needs_no_overflow_flag_and_records_the_plan(self):
        summary, _ = self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"))
        self.assertEqual("extraction-only", summary["mode"])
        self.assertEqual(15, len(summary["calls"]))
        self.assertTrue(all(call["bytes"] <= CAP for call in summary["calls"]))
        plan = summary["call_plan"]
        groups = stage_inputs.LIMITS["max_synthesis_groups"]
        self.assertEqual(3 * 5 + (groups + 1) + 5 + 1, summary["max_model_calls"])
        self.assertEqual({"mode": "extraction-only", "shards": 3, "lenses": 5, "extract": 15,
                          "synthesis": groups + 1, "synthesis_min": 1, "synthesis_groups_max": groups,
                          "verify": 5, "final": 1, "max_model_calls": summary["max_model_calls"]},
                         {k: v for k, v in plan.items() if k not in ("formula", "inputs")})
        self.assertIn("synthesis", plan["formula"])
        self.assertNotIn("status", summary["final_budget"])

    def test_the_group_limit_is_eight_so_extract_inputs_plans_nine_synthesis_calls(self):
        self.assertEqual(8, stage_inputs.LIMITS["max_synthesis_groups"])
        self.assertEqual(8, stage_inputs.MAX_SYNTHESIS_GROUPS)
        summary, _ = self.stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "x"))
        self.assertEqual(9, summary["call_plan"]["synthesis"])
        self.assertEqual(8, summary["call_plan"]["synthesis_groups_max"])
        self.assertEqual(3 * 5 + 9 + 5 + 1, summary["max_model_calls"])

    def test_the_overflow_flag_is_gone(self):
        self.stage("extract-inputs", "--run-dir", self.run, "--allow-final-overflow", "--out", str(self.root / "x"), code=2)


class SynthesisTests(ReducedCase):
    def build(self, pad=0, name="s"):
        return self.stage("synthesis-input", "--run-dir", self.run, *self.extract_args(pad), "--out", str(self.root / name))

    def test_one_call_when_the_answers_fit_and_its_prompt_holds_no_digest(self):
        summary, _ = self.build()
        self.assertEqual([c["input_dir"] for c in summary["calls"]], ["synthesis"])
        self.assertEqual(1, summary["levels"])
        self.assertFalse(summary["merge_required"])
        text = self.prompt("s", "synthesis", "task.txt")
        self.assertLessEqual(len(text.encode("utf-8")), CAP)
        self.assertIn("PR 색인", text)
        self.assertNotIn("PR 목록", text)
        self.assertNotIn(fx.PULLS[101]["patch"][:30], text)       # the diff stays in the digest
        self.assertNotIn("=== 다이제스트", text)
        index_rows = json.loads(text.split("=== PR 색인 ===\n", 1)[1].split("\n=== PR 색인 끝 ===")[0])
        self.assertEqual(6, len(index_rows))
        self.assertIn("files", index_rows[0])
        scope = json.loads((self.root / "s" / "synthesis.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.context.keymap["sections"]), sorted(scope["keys"]))
        self.assertEqual({"verified": 6 * 15 // 3 * 1, "total": 6 * 15 // 3}, {k: v for k, v in summary["quotes"].items()}
                         if False else summary["quotes"])

    def test_the_quote_check_still_runs_over_every_extract_answer(self):
        bad = self.extract_args()
        broken = json.loads(Path(bad[1]).read_text(encoding="utf-8"))
        broken["observations"][0]["quote"] += " (reworded)"
        Path(bad[1]).write_text(json.dumps(broken), encoding="utf-8")
        summary, _ = self.stage("synthesis-input", "--run-dir", self.run, *bad, "--out", str(self.root / "s"))
        self.assertEqual(summary["quotes"]["total"] - summary["quotes"]["verified"], 1)
        self.assertEqual(len(summary["clean_results"]), 14)

    def test_answers_that_do_not_fit_are_grouped_by_lens_keeping_the_shards_of_a_lens_together(self):
        pad = 1000
        cap = self.group_size(["blind", "correctness"], pad) + 50      # two whole lenses fit, three do not
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.build(pad=pad, name="g")
            self.assertEqual(3, len(summary["groups"]))
            self.assertEqual(2, summary["levels"])
            self.assertTrue(summary["merge_required"])
            self.assertEqual(["synthesis-01", "synthesis-02", "synthesis-03"], [g["input_dir"] for g in summary["groups"]])
            for call in summary["calls"]:
                self.assertLessEqual(call["bytes"], cap)
                self.assertEqual(call["input_dir"] + ".result.json", call["result_name"])
            stems = [stem for g in summary["groups"] for stem in g["stems"]]
            self.assertEqual(sorted(self.context.extract_stems()), sorted(stems))
            for group in summary["groups"]:
                for lens in {stem.split("-")[1] for stem in group["stems"]}:     # a lens is never split here
                    self.assertEqual(3, len([s for s in group["stems"] if s.split("-")[1] == lens]))
                text = self.prompt("g", group["input_dir"], "task.txt")
                self.assertIn("그룹", text)
                self.assertNotIn("=== 다이제스트", text)
                scope = json.loads((self.root / "g" / (group["input_dir"] + ".scope.json")).read_text(encoding="utf-8"))
                self.assertEqual(sorted(group["keys"]), sorted(scope["keys"]))
            self.assertEqual(4, summary["synthesis_calls"])

    def test_a_lens_that_does_not_fit_alone_is_split_by_shard(self):
        pad = 1000
        cap = self.group_size(["blind"], pad) - 200                      # one whole lens does not fit
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap), mock.patch.object(stage_inputs, "MAX_SYNTHESIS_GROUPS", 30):
            summary, _ = self.build(pad=pad, name="g")
            by_lens = {}
            for group in summary["groups"]:
                for stem in group["stems"]:
                    by_lens.setdefault(stem.split("-")[1], set()).add(group["input_dir"])
            self.assertTrue(any(len(dirs) > 1 for dirs in by_lens.values()))
            for call in summary["calls"]:
                self.assertLessEqual(call["bytes"], cap)
            self.assertEqual(sorted(self.context.extract_stems()), sorted(s for g in summary["groups"] for s in g["stems"]))

    def test_seven_groups_are_within_the_limit_of_eight_and_their_merge_is_checked_against_the_cap(self):
        pad = 1000
        cap = self.cap_for_groups(7, pad)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.build(pad=pad, name="g")
        self.assertEqual(7, len(summary["groups"]))
        self.assertEqual(8, summary["synthesis_calls"])
        self.assertTrue(all(call["bytes"] <= cap for call in summary["calls"]))
        results = [self.synthesis_answer(sorted(group["keys"])[:2], name=group["input_dir"] + ".result.json")
                   for group in summary["groups"]]
        merge = [item for path in results for item in ("--group-result", path)]
        argv = ("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage", str(self.root / "g"), *merge,
                *self.extract_args(pad))
        merged, _ = self.stage(*argv, "--out", str(self.root / "m"))
        self.assertEqual(8, merged["synthesis_calls"])
        self.assertLessEqual(merged["calls"][0]["bytes"], CAP)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", merged["calls"][0]["bytes"] - 1):
            self.stage(*argv, "--out", str(self.root / "over"), code=2)
        self.assertFalse((self.root / "over").exists())

    def test_too_many_groups_and_an_answer_too_large_alone_are_refused_before_anything_is_written(self):
        cap = self.cap_for_groups(11, 1000)
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):       # single-answer groups: more than the limit of 8
            _, stderr = self.stage("synthesis-input", "--run-dir", self.run, *self.extract_args(1000),
                                   "--out", str(self.root / "many"), code=2)
            self.assertIn("need 11 synthesis groups, over the limit of 8", stderr)
            self.assertFalse((self.root / "many").exists())
            _, stderr = self.stage("synthesis-input", "--run-dir", self.run, *self.extract_args(9000),
                                   "--out", str(self.root / "big"), code=2)
            self.assertIn("alone", stderr)
            self.assertFalse((self.root / "big").exists())


class MergeTests(ReducedCase):
    PAD = 1000

    def grouped(self):
        """Group stage output for the padded answers, with a synthetic answer for every group."""
        cap = self.group_size(["blind", "correctness"], self.PAD) + 50
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.stage("synthesis-input", "--run-dir", self.run, *self.extract_args(self.PAD),
                                    "--out", str(self.root / "g"))
        keys = list(self.context.keymap["sections"])
        results = []
        for number, group in enumerate(summary["groups"]):
            cited = sorted(group["keys"])[:2]
            results.append(self.synthesis_answer(cited, name=group["input_dir"] + ".result.json"))
        return summary, results

    def merge(self, results, *extra, out="m", code=0):
        args = [item for path in results for item in ("--group-result", path)]
        return self.stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage", str(self.root / "g"),
                          *args, *self.extract_args(self.PAD), *extra, "--out", str(self.root / out), code=code)

    def test_the_merge_input_holds_the_group_candidates_and_the_quotes_of_the_pull_requests_they_cite(self):
        summary, results = self.grouped()
        merged, _ = self.merge(results)
        self.assertEqual(["synthesis"], [c["input_dir"] for c in merged["calls"]])
        self.assertEqual("synthesis.result.json", merged["calls"][0]["result_name"])
        text = self.prompt("m", "synthesis", "task.txt")
        self.assertLessEqual(len(text.encode("utf-8")), CAP)
        for group in summary["groups"]:
            self.assertIn("그룹 종합 " + group["input_dir"], text)
        self.assertNotIn("=== 다이제스트", text)
        cited = {key for path in results for c in json.loads(Path(path).read_text())["candidates"] for key in c["cited_prs"]}
        scope = json.loads((self.root / "m" / "synthesis.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(cited), sorted(scope["keys"]))
        self.assertEqual(len(summary["groups"]) + 1, merged["synthesis_calls"])

    def test_the_group_answers_are_checked_like_a_synthesis_answer_and_against_their_own_group(self):
        summary, results = self.grouped()
        # Every group here saw all six pull requests; narrow the first group's recorded scope to two of them.
        stage_json = self.root / "g" / "stage.json"
        recorded = json.loads(stage_json.read_text(encoding="utf-8"))
        narrow = sorted(recorded["groups"][0]["keys"])[:2]
        recorded["groups"][0]["keys"] = narrow
        stage_json.write_text(json.dumps(recorded), encoding="utf-8")
        outside = next(key for key in self.context.keymap["sections"] if key not in narrow)
        summary["groups"][0]["keys"] = narrow
        bad_scope = self.synthesis_answer([narrow[0], outside], name=summary["groups"][0]["input_dir"] + ".result.json")
        self.merge([bad_scope] + results[1:], out="a", code=2)
        one_pr = self.synthesis_answer([summary["groups"][0]["keys"][0]] * 2,
                                       name=summary["groups"][0]["input_dir"] + ".result.json")
        self.merge([one_pr] + results[1:], out="b", code=2)
        self.merge(results[1:], out="c", code=2)                       # a group answer missing
        self.merge(results + [self.synthesis_answer(name="synthesis-99.result.json")], out="d", code=2)
        for out in "abcd":
            self.assertFalse((self.root / out).exists())

    def test_a_merge_input_over_the_cap_is_refused(self):
        _, results = self.grouped()
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", 5000):
            self.merge(results, out="x", code=2)
        self.assertFalse((self.root / "x").exists())


class LensQuoteCase(ReducedCase):
    """Extract answers whose quote for a PR differs by lens, so a selected quote shows which answer it came from."""

    PAD = 1000
    # Which of a key's real sentences each lens quotes: the three groups' first lenses (blind, maintenance,
    # structure... and blind vs correctness) differ, so the quote shows which answer it was taken from.
    INDEX = {"blind": 0, "correctness": 1, "maintenance": 2, "review": 1, "structure": 2}

    def setUp(self):
        super().setUp()
        self.sentences = self.real_sentences()

    def real_sentences(self):
        """Per digest key, distinct sentences of its section that the quote checker accepts."""
        candidates = []
        for name in self.context.keymap["shards"]:
            text = (self.context.digest_dir / name).read_text(encoding="utf-8")
            for chunk in text.split("\n### "):
                head = chunk.lstrip("#").lstrip().split(" — ", 1)[0].strip()
                if head in self.context.keymap["sections"]:
                    for line in chunk.splitlines():
                        words = line.strip().split(" ")
                        if line.startswith("  ") and 25 <= len(line.strip()) <= 200:
                            # the line, and the line without its first or last word: still contiguous source text
                            candidates += [(head, " ".join(part)) for part in (words, words[1:], words[:-1])
                                           if len(" ".join(part)) >= 25]
        probe = self.answers / "probe.result.json"
        probe.write_text(json.dumps({"lens": "blind", "patterns": [], "rejected": [], "observations": [
            {"pr": key, "note": "n", "quote": quote} for key, quote in candidates]}))
        report = stage_inputs.check_quotes(self.context, [str(probe)])
        probe.unlink()
        found = {}
        for item in report["verified_evidence"]:
            if item["quote"] not in found.setdefault(item["pr"], []):
                found[item["pr"]].append(item["quote"])
        for key in self.context.keymap["sections"]:
            self.assertGreaterEqual(len(found.get(key, [])), 3, key)
        return found

    def lens_answers(self, broken=()):
        """Extract answers whose observation quote for a key differs by lens; (lens, key) in broken gets a reworded
        quote the checker refuses."""
        shards = self.key_of_shard()
        paths = []
        for stem in self.context.extract_stems():
            lens = stem.split("-")[1]
            keys = shards["digest-%s.md" % stem.rsplit("-", 1)[1]]
            observations = []
            for key in keys:
                quote = self.sentences[key][self.INDEX[lens]]
                observations.append({"pr": key, "note": fx.SYNTHETIC + " " + "n" * self.PAD,
                                     "quote": quote + " (reworded)" if (lens, key) in broken else quote})
            path = self.answers / (stem + ".result.json")
            path.write_text(json.dumps({"lens": lens, "patterns": [], "rejected": [], "observations": observations}))
            paths.append(str(path))
        return paths

    def section(self, text, title):
        head = "=== %s ===\n" % title
        start = text.index(head) + len(head)
        return json.loads(text[start:text.index("\n=== %s 끝 ===" % title, start)])


class MinimalMergeTests(LensQuoteCase):
    """The merge input holds the group candidates (never their dropped ideas), the PR index, and for each
    (candidate, cited PR) the first verified quote of that candidate's own group: the group's extract answers in
    the group's stem order, then each answer's own order (pattern evidence, then observations)."""

    RULE = "입력에는 각 그룹이 남긴 후보만 있다. 그룹이 버린 항목은 다시 살리지 않는다."

    def grouped(self, paths, out="g"):
        cap = self.group_size(["blind", "correctness"], self.PAD, paths=paths) + 50
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", cap):
            summary, _ = self.stage("synthesis-input", "--run-dir", self.run,
                                    *[item for path in paths for item in ("--extract-result", path)],
                                    "--out", str(self.root / out))
        self.assertEqual([["blind", "correctness"], ["maintenance", "review"], ["structure"]],
                         [sorted({stem.split("-")[1] for stem in group["stems"]}) for group in summary["groups"]])
        return summary

    def group_answers(self, summary):
        """Two candidates per group, each citing three of the group's keys, and dropped ideas the merge must not see."""
        paths = []
        for group in summary["groups"]:
            keys = sorted(group["keys"])
            answer = {"candidates": [{"id": "C%d" % n, "statement": "%s candidate %d" % (group["input_dir"], n),
                                      "cited_prs": [keys[(n + step) % len(keys)] for step in range(3)],
                                      "found_by": "blind"} for n in (1, 2)],
                      "dropped": [{"idea": "DROPPED-%s-%d" % (group["input_dir"], n), "reason": "weak"} for n in range(3)]}
            path = self.answers / (group["input_dir"] + ".result.json")
            path.write_text(json.dumps(answer), encoding="utf-8")
            paths.append(str(path))
        return paths

    def merge(self, groups, extract, out="m", code=0):
        return self.stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage", str(self.root / "g"),
                          *[item for path in groups for item in ("--group-result", path)],
                          *[item for path in extract for item in ("--extract-result", path)],
                          "--out", str(self.root / out), code=code)

    def expected_quote(self, summary, group_dir, key, broken=()):
        group = next(g for g in summary["groups"] if g["input_dir"] == group_dir)
        for stem in group["stems"]:
            lens = stem.split("-")[1]
            if key in self.key_of_shard()["digest-%s.md" % stem.rsplit("-", 1)[1]] and (lens, key) not in broken:
                return self.sentences[key][self.INDEX[lens]]
        return None

    def test_the_group_blocks_hold_the_candidates_only_and_the_prompt_says_dropped_ideas_stay_dropped(self):
        extract = self.lens_answers()
        summary = self.grouped(extract)
        groups = self.group_answers(summary)
        self.merge(groups, extract)
        text = self.prompt("m", "synthesis", "task.txt")
        self.assertIn(self.RULE, text)
        self.assertNotIn("DROPPED-", text)
        for group, path in zip(summary["groups"], groups):
            self.assertEqual({"candidates": json.loads(Path(path).read_text())["candidates"]},
                             self.section(text, "그룹 종합 " + group["input_dir"]))

    def test_each_candidate_pr_gets_the_first_verified_quote_of_its_own_group(self):
        extract = self.lens_answers()
        summary = self.grouped(extract)
        groups = self.group_answers(summary)
        merged, _ = self.merge(groups, extract)
        report = self.section(self.prompt("m", "synthesis", "task.txt"), "인용 확인 보고서")
        expected, picks = [], []
        for group, path in zip(summary["groups"], groups):
            for candidate in json.loads(Path(path).read_text())["candidates"]:
                for key in candidate["cited_prs"]:
                    quote = self.expected_quote(summary, group["input_dir"], key)
                    self.assertIsNotNone(quote)
                    picks.append({"group": group["input_dir"], "candidate": candidate["id"], "pr": key, "quote": quote})
                    if {"pr": key, "quote": quote} not in expected:
                        expected.append({"pr": key, "quote": quote})
        self.assertEqual(expected, [{"pr": item["pr"], "quote": item["quote"]} for item in report["verified_evidence"]])
        self.assertEqual([], report["no_quote"])
        # the second group's quotes come from its own lenses (maintenance first), not from blind
        second = [pick for pick in picks if pick["group"] == summary["groups"][1]["input_dir"]]
        self.assertTrue(all(pick["quote"] == self.sentences[pick["pr"]][self.INDEX["maintenance"]] for pick in second))
        scope = json.loads((self.root / "m" / "synthesis.scope.json").read_text(encoding="utf-8"))
        self.assertEqual({"keys": sorted({pick["pr"] for pick in picks}), "quotes": expected}, scope)
        reordered, _ = self.merge(list(reversed(groups)), list(reversed(extract)), out="m2")
        self.assertEqual(self.prompt("m", "synthesis", "task.txt"), self.prompt("m2", "synthesis", "task.txt"))

    def test_a_candidate_pr_without_a_verified_quote_in_its_group_is_marked_not_filled_from_another_group(self):
        first = self.key_of_shard()["digest-01.md"][0]
        broken = {("blind", first), ("correctness", first)}             # the first group's two lenses
        extract = self.lens_answers(broken)
        summary = self.grouped(extract)
        groups = self.group_answers(summary)
        group_dir = summary["groups"][0]["input_dir"]
        answer = json.loads(Path(groups[0]).read_text())
        other = next(key for key in sorted(summary["groups"][0]["keys"]) if key != first)
        answer["candidates"][0]["cited_prs"] = [first, other]
        Path(groups[0]).write_text(json.dumps(answer), encoding="utf-8")
        self.merge(groups, extract)
        report = self.section(self.prompt("m", "synthesis", "task.txt"), "인용 확인 보고서")
        self.assertEqual([{"group": group_dir, "candidate": "C1", "pr": first}],
                         [item for item in report["no_quote"] if item["pr"] == first and item["group"] == group_dir])
        # another group's quote for the same PR is never borrowed: only groups whose own candidates cite it add one
        elsewhere = {self.expected_quote(summary, group["input_dir"], first, broken)
                     for group, path in zip(summary["groups"][1:], groups[1:])
                     if any(first in c["cited_prs"] for c in json.loads(Path(path).read_text())["candidates"])}
        self.assertEqual(elsewhere, {item["quote"] for item in report["verified_evidence"] if item["pr"] == first})


class VerifyTests(ReducedCase):
    def verify(self, synthesis, out="v", code=0):
        return self.stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *self.extract_args(),
                          "--out", str(self.root / out), code=code)

    def test_a_candidate_gets_only_the_sections_of_the_pull_requests_it_cites(self):
        keys = list(self.context.keymap["sections"])
        summary, _ = self.verify(self.synthesis_answer(keys[:2]))
        call = summary["calls"][0]
        text = self.prompt("v", "verify-C1", "task.txt")
        self.assertLessEqual(call["bytes"], CAP)
        for key in keys[:2]:
            self.assertIn("### %s — " % key, text)
        for key in keys[2:]:
            self.assertNotIn("### %s — " % key, text.split("나머지 PR 목록")[0])
            self.assertIn("- %s — " % key, text)                     # title only, in the index of the others
        self.assertEqual({"cited_total": 2, "cited_included": 2, "trimmed": False},
                         {k: call[k] for k in ("cited_total", "cited_included", "trimmed")})
        scope = json.loads((self.root / "v" / "verify-C1.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(keys[:2]), sorted(scope["keys"]))

    def stage_args(self, name):
        return mock.Mock(out=str(self.root / name))

    def candidate(self, keys):
        return {"id": "C1", "statement": fx.SYNTHETIC + " backoff", "cited_prs": keys}

    def test_a_candidate_that_cites_too_many_pull_requests_keeps_the_first_n_and_records_n(self):
        keys = list(self.context.keymap["sections"])
        four = stage_inputs.verify_prompt(self.context, self.candidate(keys[:4]))
        three = stage_inputs.verify_prompt(self.context, self.candidate(keys[:3]))
        limit = (len(four.encode("utf-8")) + len(three.encode("utf-8"))) // 2 + 1
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", limit):
            summary = stage_inputs.reduced_verify_inputs(self.context, self.stage_args("v"),
                                                         [self.candidate(keys[:5])], {})
        call = summary["calls"][0]
        self.assertTrue(call["trimmed"])
        self.assertEqual((5, 3), (call["cited_total"], call["cited_included"]))
        self.assertLessEqual(call["bytes"], limit)
        scope = json.loads((self.root / "v" / "verify-C1.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(keys[:3]), scope["keys"])
        text = self.prompt("v", "verify-C1", "task.txt")
        self.assertIn('"cited_prs"', text)
        for key in keys[3:5]:
            self.assertNotIn("### %s — " % key, text.split("나머지 PR 목록")[0])
            self.assertIn("- %s — " % key, text)                         # still in the title list of the others

    def test_a_candidate_whose_two_pull_requests_do_not_fit_is_refused(self):
        keys = list(self.context.keymap["sections"])
        two = stage_inputs.verify_prompt(self.context, self.candidate(keys[:2]))
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", len(two.encode("utf-8")) - 1):
            with self.assertRaises(stage_inputs.StageError):
                stage_inputs.reduced_verify_inputs(self.context, self.stage_args("r"), [self.candidate(keys[:5])], {})
        self.assertFalse((self.root / "r").exists())

    def test_the_verify_prompt_of_a_one_shard_digest_still_holds_the_cited_sections_only(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        context = stage_inputs.Context(make_big_run(self, root, ("--max-bytes", "40000")))
        self.assertEqual(1, context.shards)
        keys = list(context.keymap["sections"])
        text = stage_inputs.verify_prompt(context, self.candidate(keys[:2]))
        self.assertIn("### %s — " % keys[0], text)
        self.assertNotIn("### %s — " % keys[3], text.split("나머지 PR 목록")[0])
        self.assertIn("- %s — " % keys[3], text)


class FinalTests(ReducedCase):
    def inputs(self, pad=0):
        keys = list(self.context.keymap["sections"])
        synthesis = self.synthesis_answer(keys[:2])
        self.stage("verify-inputs", "--run-dir", self.run, "--synthesis", synthesis, *self.extract_args(pad),
                   "--out", str(self.root / "v"))
        verify = self.write("verify-C1.result.json", {
            "id": "C1", "verdict": "supported", "evidence_checks": [
                {"pr": key, "holds": True, "quote": self.quote_of(key), "reason": "r"} for key in keys[:2]],
            "counterexamples": [], "distinct_prs": 2, "distinct_repos": 1, "actionable": True,
            "actionable_reason": "r", "confidence": "medium", "revised_statement": "s"})
        return synthesis, verify

    def final(self, pad=0, out="f", code=0, built=None):
        synthesis, verify = built or self.inputs(pad)
        return self.stage("final-input", "--run-dir", self.run, "--synthesis", synthesis, *self.extract_args(pad),
                          "--verify-result", verify, "--out", str(self.root / out), code=code)

    def test_the_final_prompt_holds_the_index_the_candidates_and_the_verified_quotes_but_no_digest(self):
        summary, _ = self.final()
        text = self.prompt("f", "final", "task.txt")
        self.assertLessEqual(summary["calls"][0]["bytes"], CAP)
        self.assertIn("PR 색인", text)
        self.assertIn("후보", text)
        self.assertIn("반박 검증 verify-C1", text)
        self.assertIn("인용 확인 보고서", text)
        self.assertNotIn("=== 다이제스트", text)
        self.assertNotIn(fx.PULLS[101]["patch"][:30], text)
        self.assertIn(fx.PULLS[101]["quote"], text)                  # a verified quote from the extraction answers

    def test_the_scope_lists_the_quotes_the_final_input_kept(self):
        self.final()
        scope = json.loads((self.root / "f" / "final.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(self.context.keymap["sections"]), sorted(scope["keys"]))
        keys = list(self.context.keymap["sections"])[:2]             # what the candidate and its verify answer cite
        self.assertEqual([{"pr": key, "quote": self.quote_of(key)} for key in keys], scope["quotes"])

    def test_a_final_prompt_over_the_cap_is_refused_not_cut(self):
        built = self.inputs()
        with mock.patch.object(stage_inputs, "PROMPT_MAX_BYTES", 8000):
            self.final(out="x", code=2, built=built)
        self.assertFalse((self.root / "x").exists())


class MinimalFinalTests(LensQuoteCase):
    """The extraction-only final input keeps, besides the PR index, the candidates and the verify answers: for each
    (final candidate, cited PR) the first verified quote of the extract answers (stem order, then each answer's
    order), and every (PR, quote) a verify answer cited that the checker verified. Nothing else."""

    def keys(self):
        return list(self.context.keymap["sections"])

    def built(self, broken=()):
        extract = self.lens_answers(broken)
        keys = self.keys()
        synthesis = self.write("synthesis.result.json", {"candidates": [
            {"id": "C1", "statement": "first", "cited_prs": [keys[0], keys[1], keys[2]], "found_by": "blind"},
            {"id": "C2", "statement": "second", "cited_prs": [keys[2], keys[3], keys[5]], "found_by": "review"}],
            "dropped": [{"idea": "DROPPED-final", "reason": "weak"}]})
        verify = [
            self.write("verify-C1.result.json", {
                "id": "C1", "verdict": "supported",
                "evidence_checks": [{"pr": keys[4], "holds": True, "quote": self.sentences[keys[4]][2], "reason": "r"}],
                "counterexamples": [{"pr": keys[0], "quote": self.sentences[keys[0]][1], "reason": "r"}],
                "distinct_prs": 2, "distinct_repos": 1, "actionable": True, "actionable_reason": "r",
                "confidence": "medium", "revised_statement": "s"}),
            self.write("verify-C2.result.json", {
                "id": "C2", "verdict": "weak",
                "evidence_checks": [{"pr": keys[3], "holds": True, "quote": self.sentences[keys[3]][2] + " (reworded)",
                                     "reason": "r"}],
                "counterexamples": [], "distinct_prs": 2, "distinct_repos": 1, "actionable": True,
                "actionable_reason": "r", "confidence": "low", "revised_statement": "s"})]
        return extract, synthesis, verify

    def final(self, built, out="f", reverse=False, code=0):
        extract, synthesis, verify = built
        extract = list(reversed(extract)) if reverse else extract
        verify = list(reversed(verify)) if reverse else verify
        return self.stage("final-input", "--run-dir", self.run, "--synthesis", synthesis,
                          *[item for path in extract for item in ("--extract-result", path)],
                          *[item for path in verify for item in ("--verify-result", path)],
                          "--out", str(self.root / out), code=code)

    def first_quote(self, key, broken=()):
        """The first verified extract quote of a key: stems in order (blind first), each answer in its own order."""
        for stem in self.context.extract_stems():
            lens = stem.split("-")[1]
            if key in self.key_of_shard()["digest-%s.md" % stem.rsplit("-", 1)[1]] and (lens, key) not in broken:
                return self.sentences[key][self.INDEX[lens]]
        return None

    def test_the_quote_report_holds_one_quote_per_candidate_pr_and_the_verified_verify_quotes(self):
        built = self.built()
        summary, _ = self.final(built)
        text = self.prompt("f", "final", "task.txt")
        report = self.section(text, "인용 확인 보고서")
        keys = self.keys()
        expected = []
        for key in (keys[0], keys[1], keys[2], keys[3], keys[5]):           # C1 then C2, each PR once
            expected.append({"pr": key, "quote": self.first_quote(key)})
        expected += [{"pr": keys[4], "quote": self.sentences[keys[4]][2]},   # verify-C1, in its own order
                     {"pr": keys[0], "quote": self.sentences[keys[0]][1]}]
        self.assertEqual(expected, [{"pr": item["pr"], "quote": item["quote"]} for item in report["verified_evidence"]])
        self.assertEqual([], report["no_quote"])
        self.assertNotIn(self.sentences[keys[3]][2] + " (reworded)", json.dumps(report, ensure_ascii=False))
        self.assertEqual({"verified", "total"}, set(report["totals"]))
        self.assertIn("no_quote", text.split("=== PR 색인 ===")[0])          # the instructions explain both lists
        scope = json.loads((self.root / "f" / "final.scope.json").read_text(encoding="utf-8"))
        self.assertEqual({"keys": sorted(keys), "quotes": expected},
                         {"keys": sorted(scope["keys"]), "quotes": scope["quotes"]})
        self.assertEqual({"listed": len(expected), "no_quote": 0}, summary["final_quotes"])
        self.final(built, out="f2", reverse=True)
        self.assertEqual(text, self.prompt("f2", "final", "task.txt"))

    def test_a_candidate_pr_without_any_verified_extract_quote_is_marked_no_quote(self):
        keys = self.keys()
        built = self.built(broken={(lens, keys[5]) for lens in LENSES})
        self.final(built)
        report = self.section(self.prompt("f", "final", "task.txt"), "인용 확인 보고서")
        self.assertEqual([{"candidate": "C2", "pr": keys[5]}], report["no_quote"])
        self.assertNotIn(keys[5], [item["pr"] for item in report["verified_evidence"]])


EVIDENCE_1010 = Path("/Users/lee-kyu-hwan/.oss-research-vm/logs/a78-docs-first-20261004T021348Z")
COLLECTIONS_1010 = ("P1-eslint-eslint", "P5-typescript-eslint", "P2-eslint-markdown", "P3-eslint-css")


@unittest.skipUnless(all((EVIDENCE_1010 / name / "unpacked/corpus.json").is_file() for name in COLLECTIONS_1010),
                     "the 2026-10-04 collections are only on the machine that made them")
class Run13InputTests(unittest.TestCase):
    """The first collection of 2026-10-04 (13 docs pull requests, four repositories): digest of two shards, then
    every stage input from hand-written stand-in answers whose quotes are real sentences of the corpus."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        request = {"schema_version": "1.0.0", "mode": "offline-fixture", "source": {
            "kind": "recent-closed-prs", "repositories": ["eslint/eslint", "typescript-eslint/typescript-eslint",
                                                          "eslint/markdown", "eslint/css"],
            "interval": {"start_at": "2026-09-03T00:00:00Z", "end_at": "2026-10-04T00:00:00Z"}, "outcome": "merged",
            "max_per_repository": 5, "request_budget": 60, "reference_repositories": []},
            "target": None, "budget": {"total_api_requests": 200, "max_elapsed_seconds": None}, "static_only": True,
            "provenance": {"model": "unknown", "effort": "unknown"}}
        (self.root / "request.json").write_text(json.dumps(request), encoding="utf-8")
        self.run = str(self.root / "run")
        adopt = []
        for name in COLLECTIONS_1010:
            unpacked = EVIDENCE_1010 / name / "unpacked"
            adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest", str(unpacked / "collection-manifest.json"),
                      "--inventory", str(unpacked / "inventory.md")]
        for argv in (("init", "--request", str(self.root / "request.json")), ("adopt-collection", *adopt), ("digest",)):
            code, _, stderr = workflow(argv[0], "--run-dir", self.run, *argv[1:])
            self.assertEqual(code, 0, stderr)
        self.context = stage_inputs.Context(self.run)
        self.answers = self.root / "answers"
        self.answers.mkdir()

    def quotes(self):
        """Per digest key, real sentences of that PR's section that the quote checker accepts."""
        candidates = []
        for name in self.context.keymap["shards"]:
            text = (self.context.digest_dir / name).read_text(encoding="utf-8")
            for chunk in text.split("\n### ")[0:]:
                head = chunk.lstrip("#").lstrip().split(" — ", 1)[0].strip()
                if head not in self.context.keymap["sections"]:
                    continue
                lines = [line.strip() for line in chunk.splitlines() if 45 <= len(line.strip()) <= 140
                         and line.startswith("  ") and "](" not in line and "`" not in line]
                candidates += [(head, line) for line in lines[:12]]
        path = self.answers / "probe.result.json"
        path.write_text(json.dumps({"lens": "blind", "patterns": [], "rejected": [],
                                    "observations": [{"pr": k, "note": "n", "quote": q} for k, q in candidates]}))
        report = stage_inputs.check_quotes(self.context, [str(path)])
        failed = {int(f["path"].split("[")[1].rstrip("]")) for f in report["files"][0]["failures"]}
        found = {}
        for index, (key, quote) in enumerate(candidates):
            if index not in failed:
                found.setdefault(key, []).append(quote)
        for key in self.context.keymap["sections"]:
            self.assertGreaterEqual(len(found.get(key, [])), 3, key)
        return found

    def extract_result(self, stem, quotes):
        lens = stem.split("-")[1]
        shard = "digest-%s.md" % stem.rsplit("-", 1)[1]
        keys = [k for k, section in self.context.keymap["sections"].items() if section["shard"] == shard]
        patterns = []
        for number in range(3):
            cited = [keys[(number + step) % len(keys)] for step in range(3)]
            patterns.append({
                "title": "%s pattern %d" % (lens, number), "description": fx.SYNTHETIC + " " + "d" * 280,
                "kind": "convention",
                "evidence": [{"pr": key, "role": "supports", "quote": quotes[key][number % 3], "claim": "c" * 120}
                             for key in cited],
                "applicability": ["a" * 150], "counterconditions": ["c" * 150], "search_clues": ["s" * 120],
                "expected_tests": ["t" * 120], "maintainer_judgment_required": ["m" * 120],
                "confidence": "low", "confidence_reason": "r" * 150})
        answer = {"lens": lens, "patterns": patterns, "rejected": [{"idea": "i" * 100, "reason": "r" * 100}],
                  "observations": [{"pr": key, "note": "o" * 160, "quote": quotes[key][0]} for key in keys]}
        path = self.answers / (stem + ".result.json")
        path.write_text(json.dumps(answer), encoding="utf-8")
        return str(path)

    def write(self, name, document):
        path = self.answers / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def run_stages(self):
        """Every stage input of the run; returns {stage: {input_dir: bytes}} and the call plan."""
        sizes, quotes = {}, self.quotes()
        code, extract, stderr = stage("extract-inputs", "--run-dir", self.run, "--out", str(self.root / "extract"))
        self.assertEqual(code, 0, stderr)
        sizes["extract"] = {c["input_dir"]: c["bytes"] for c in extract["calls"]}
        extract_args = []
        for stem in self.context.extract_stems():
            extract_args += ["--extract-result", self.extract_result(stem, quotes)]
        code, synthesis, stderr = stage("synthesis-input", "--run-dir", self.run, *extract_args, "--out", str(self.root / "syn"))
        self.assertEqual(code, 0, stderr)
        sizes["synthesis"] = {c["input_dir"]: c["bytes"] for c in synthesis["calls"]}
        keys = list(self.context.keymap["sections"])
        if synthesis["merge_required"]:
            groups = []
            for number, group in enumerate(synthesis["groups"]):
                cited = group["keys"][number: number + 3]
                groups.append(self.write(group["input_dir"] + ".result.json", {"candidates": [
                    {"id": "C1", "statement": fx.SYNTHETIC + " group %d pattern" % number, "cited_prs": cited,
                     "found_by": "group %d" % number}], "dropped": [{"idea": "i", "reason": "r"}]}))
            code, merged, stderr = stage("synthesis-merge-input", "--run-dir", self.run, "--synthesis-stage",
                                         str(self.root / "syn"), *[a for g in groups for a in ("--group-result", g)],
                                         *extract_args, "--out", str(self.root / "merge"))
            self.assertEqual(code, 0, stderr)
            sizes["synthesis-merge"] = {c["input_dir"]: c["bytes"] for c in merged["calls"]}
            cited_union = sorted({k for g in synthesis["groups"] for k in g["keys"][:5]})
        candidates = [{"id": "C%d" % n, "statement": fx.SYNTHETIC + " pattern %d" % n, "found_by": "merge",
                       "cited_prs": keys[n:n + 7]} for n in range(1, 4)]
        merged_answer = self.write("synthesis.result.json", {"candidates": candidates, "dropped": []})
        code, verify, stderr = stage("verify-inputs", "--run-dir", self.run, "--synthesis", merged_answer, *extract_args,
                                     "--out", str(self.root / "verify"))
        self.assertEqual(code, 0, stderr)
        sizes["verify"] = {c["input_dir"]: c["bytes"] for c in verify["calls"]}
        verify_args = []
        for candidate in candidates:
            checks = [{"pr": key, "holds": True, "quote": quotes[key][0], "reason": "r" * 200} for key in candidate["cited_prs"]]
            verify_args += ["--verify-result", self.write("verify-%s.result.json" % candidate["id"], {
                "id": candidate["id"], "verdict": "supported", "evidence_checks": checks, "counterexamples": [],
                "distinct_prs": len(checks), "distinct_repos": 2, "actionable": True, "actionable_reason": "a" * 300,
                "confidence": "medium", "revised_statement": candidate["statement"]})]
        code, final, stderr = stage("final-input", "--run-dir", self.run, "--synthesis", merged_answer, *extract_args,
                                    *verify_args, "--out", str(self.root / "final"))
        self.assertEqual(code, 0, stderr)
        sizes["final"] = {c["input_dir"]: c["bytes"] for c in final["calls"]}
        return sizes, extract["call_plan"], synthesis

    def test_every_stage_input_of_the_13_pull_request_run_is_within_the_prompt_limit(self):
        sizes, plan, synthesis = self.run_stages()
        self.assertEqual(13, len(self.context.records))
        self.assertEqual(2, self.context.shards)
        self.assertEqual(10, len(sizes["extract"]))
        self.assertEqual(1, len(sizes["final"]))
        self.assertEqual(3, len(sizes["verify"]))
        for stage_name, calls in sizes.items():
            for name, size in calls.items():
                self.assertLessEqual(size, CAP, (stage_name, name))
        total = sum(len(calls) for calls in sizes.values())
        self.assertLessEqual(total, plan["max_model_calls"])
        self.assertTrue(synthesis["merge_required"])           # ten full extract answers do not fit one synthesis prompt
        self.assertEqual(len(synthesis["groups"]) + 1, len(sizes["synthesis"]) + len(sizes["synthesis-merge"]))


if __name__ == "__main__":
    unittest.main()
