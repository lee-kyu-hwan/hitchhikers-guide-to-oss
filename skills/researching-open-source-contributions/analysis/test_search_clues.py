"""A final answer's search_clues must be strings the candidate verifier can put into GitHub search as they are (#82).

The verifier's discover (verifying-open-source-contribution-candidates, verify_candidates.py) uses each of a
pattern's first max_clues_per_pattern (5) search_clues verbatim, inside double quotes:
``repo:<target> "<clue>" is:issue|pr is:open|closed`` (four issue searches) and ``repo:<target> "<clue>"`` (one
code search). It drops a clue holding a double quote, a backslash or a control character (_safe_search_clue,
warning unsafe-search-clue-rejected). The 2026-10-05 discover of the r6 patterns read nothing: every clue was a
Korean sentence, so no exact phrase matched. The final prompts now ask for short search strings, and the strict
verifier refuses a final answer whose search_clues are not such strings (item R6): empty, longer than the
candidate-reads entry's clue limit (MAX_CLUE_CHARS, 200), edge spaces, a double quote, a backslash, a control
character, or Hangul. Nothing is relaxed; an answer without search_clues is left to the assemble schema.

The verifier runs as a process (selftest_verify_kinds), so this file does not install the offline guard of
test_stage_inputs. No model, network or credential is used.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_search_clues.py
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import selftest_verify_kinds as kinds  # noqa: E402
import machine_settings as machine  # noqa: E402


def final_with_clues(clues):
    answer = json.loads(json.dumps(kinds.GOOD["final"]))
    if clues is not None:
        answer["patterns"][0]["search_clues"] = clues
    return answer


class FinalAnswerSearchClues(unittest.TestCase):
    def check(self, clues):
        with tempfile.TemporaryDirectory(prefix="e2e-clues-") as t:
            tmp = Path(t)
            rc, items, doc = kinds.verdict(tmp, kinds.make(tmp, "final", answer=final_with_clues(clues)), "final")
            return rc, items, {item["id"]: item["detail"] for item in doc.get("items", [])}

    def test_search_strings_pass(self):
        rc, items, _ = self.check(["## Options", "further_reading", "npm install eslint", "kangax.github.io"])
        self.assertEqual((0, "PASS", "evidence"), (rc, items["R6"], items["class"]))

    def test_each_kind_of_clue_the_search_cannot_use_fails_with_its_own_reason(self):
        cases = {
            "Hangul": ["규칙 문서에서 '## Options' 헤딩이 없는 파일"],
            "empty": [""],
            "longer than 200": ["a" * 201],
            "double quote": ['the "strict" option'],
            "backslash": ["docs\\rules"],
            "edge spaces": [" ## Options"],
            "control character": ["a\tb"],
            "not a string": [5],
        }
        for reason, clues in cases.items():
            with self.subTest(reason):
                rc, items, details = self.check(clues)
                self.assertEqual((1, "FAIL", "invalid"), (rc, items["R6"], items["class"]))
                self.assertIn(reason, details["R6-search-clues"])

    def test_two_hundred_characters_still_pass(self):
        rc, items, _ = self.check(["a" * 200])
        self.assertEqual((0, "PASS"), (rc, items["R6"]))

    def test_an_answer_without_search_clues_is_left_to_the_schema(self):
        rc, items, details = self.check(None)
        self.assertEqual((0, "PASS"), (rc, items["R6"]))
        self.assertIn("no search_clues", details["R6-search-clues"])


EVIDENCE_R6 = machine.VM_LOGS / "a78-period-analysis-eslint-r6-20261005T043448Z"


@unittest.skipUnless((EVIDENCE_R6 / "final/verdict.json").is_file(), "the r6 final call is only on the machine that made it")
class AdoptedR6AnswerNowFails(unittest.TestCase):
    """The r6 final answer was adopted under the earlier rules; its search_clues are Korean sentences. With the
    search clue check the same answer is refused (R6), and nothing else in it changes verdict."""

    def test_the_r6_final_answer_fails_only_the_search_clue_check(self):
        with tempfile.TemporaryDirectory(prefix="e2e-r6-") as t:
            out = Path(t) / "verdict.json"
            run = subprocess.run([sys.executable, "-B", str(HERE / "verify_e2e_result.py"), "--run-dir",
                                  str(EVIDENCE_R6 / "final"), "--corpus",
                                  str(EVIDENCE_R6 / "wf-run/stages/collect/gen-001/corpus.json"),
                                  "--prompt", str(EVIDENCE_R6 / "in-final/final/task.txt"), "--answer-kind", "final",
                                  "--input-scope", str(EVIDENCE_R6 / "in-final/final.scope.json"), "--out", str(out)],
                                 capture_output=True, text=True, timeout=120)
            doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(1, run.returncode, run.stderr)
        failed = {item["id"]: item["detail"] for item in doc["items"] if item["status"] in ("FAIL", "BLOCKED")}
        self.assertEqual(["R6-search-clues"], sorted(failed))
        self.assertIn("Hangul", failed["R6-search-clues"])
        statuses = {item["id"]: item["status"] for item in doc["items"]}
        self.assertEqual(("PASS", "PASS", "PASS"), (statuses["R3-report-schema"], statuses["R5-input-scope"],
                                                    statuses["Q1-quotes-verbatim"]))


class FinalPromptsAskForSearchStrings(unittest.TestCase):
    def test_both_final_prompts_state_the_search_clue_rule_with_examples(self):
        for name in ("finalize.md", "finalize-reduced.md"):
            text = (HERE / "prompts" / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertIn("GitHub 검색", text)
                for example in ("`## Options`", "`further_reading`", "`npm install eslint`", "`kangax.github.io`"):
                    self.assertIn(example, text)
                rule = [line for line in text.splitlines() if "search_clues" in line and "GitHub 검색" in line]
                self.assertEqual(1, len(rule))
                self.assertIn("한국어", rule[0])


if __name__ == "__main__":
    unittest.main()
