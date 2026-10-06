"""A quote that holds a double quote must reach the final answer as the source text, not as JSON notation (#82).

The r5 final answer of the eslint run quoted ``404 "Vhost unknown" error`` as ``404 \\"Vhost unknown\\" error``:
the input showed the verified quotes inside a JSON block, where the source ``"`` reads ``\\"``, and the answer
copied the backslashes. These tests pin the three parts of the fix:

* the extraction-only final input lists the verified quotes as the source text (no JSON escape);
* both final prompts say a quote keeps the source's ``"`` and ``\\`` and that ``\\"`` in a JSON block is notation;
* the verifier still refuses a quote that carries the escape, and says so (no normalization, the verdict stays FAIL):
  that part runs the verifier as a process, so it is in selftest_verify_scope.py (the offline guard here blocks
  subprocesses).

Every answer is a hand-written stand-in; nothing starts a model, opens the network or reads a credential.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_quote_escape.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_reduce_inputs as reduce  # noqa: E402  (installs the offline guards first)

fx, stage_inputs = reduce.fx, reduce.stage_inputs

# A fixture body whose quote holds double quotes; only these tests see it.
QUOTED = 'with "jittered" exponential backoff between retries'
PULL_101 = dict(fx.PULLS[101], body=fx.SYNTHETIC + '. Replace the fixed sleep ' + QUOTED + '.', quote=QUOTED)
ESCAPED = json.dumps(QUOTED)[1:-1]          # with "jittered" ... as a JSON string shows it: with \"jittered\" ...


class FinalInputShowsTheSourceText(reduce.ReducedCase):
    """The extraction-only final input of a run whose verified quote holds double quotes."""

    inputs, final = reduce.FinalTests.inputs, reduce.FinalTests.final

    def setUp(self):
        patch = mock.patch.dict(fx.PULLS, {101: PULL_101})
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def report_block(self, text):
        head = "=== 인용 확인 보고서 ===\n"
        start = text.index(head) + len(head)
        return text[start:text.index("\n=== 인용 확인 보고서 끝 ===", start)]

    def test_the_verified_quotes_are_listed_as_the_source_text(self):
        keys = list(self.context.keymap["sections"])[:2]
        self.assertIn(QUOTED, [self.quote_of(key) for key in keys])   # the candidate cites the quoted PR
        self.final()
        text = self.prompt("f", "final", "task.txt")
        block = self.report_block(text)
        self.assertIn("\n" + QUOTED + "\n", block + "\n")            # one line, the source characters
        self.assertNotIn(ESCAPED, block)                              # never the JSON notation
        scope = json.loads((self.root / "f" / "final.scope.json").read_text(encoding="utf-8"))
        self.assertIn(QUOTED, [item["quote"] for item in scope["quotes"]])
        listed = stage_inputs.parse_quote_lines(block)
        self.assertEqual([(item["pr"], item["quote"]) for item in scope["quotes"]],
                         [(item["pr"], item["quote"]) for item in listed])


class FinalPromptsStateTheEscapeRule(unittest.TestCase):
    def test_both_final_prompts_say_a_quote_keeps_the_source_double_quote(self):
        for name in ("finalize.md", "finalize-reduced.md"):
            text = (HERE / "prompts" / name).read_text(encoding="utf-8")
            self.assertIn("JSON 표기", text, name)                  # \" in an input JSON block is notation
            self.assertIn('\\"strict\\"', text, name)               # the worked example, escaped once in the output


if __name__ == "__main__":
    unittest.main()
