"""The assess call plan binds an analysis_call.sh ledger the way the analysis plans do (#259).

stage_inputs.py assess-inputs writes {"stage": "assess", "call_plan": {"mode": "assess", "assess": 1,
"max_model_calls": 1, ...}}. The harness takes it as its 7th argument: the first call records the limit and the
plan's sha256 in ledger-limit.json, a later call under another plan is refused (exit 3), the absolute cap 40 holds,
and a malformed assess plan or one given a parent ledger is refused before anything starts (exit 2).

The harness runs as a copy whose VM root is a fake limactl (the fixture of tests/test_oss_research_worker_linux.py
AnalysisCallLedgerTests, borrowed, not changed), so no VM, model or token is used. This file does not import the
offline fixture: the harness is a bash process.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_assess_ledger.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

from tests import test_oss_research_worker_linux as linux  # noqa: E402
import stage_inputs  # noqa: E402



class AssessLedgerTests(unittest.TestCase):
    # Borrowed through the module only: a module-level name for that TestCase would make unittest run it here too.
    Borrowed = linux.AnalysisCallLedgerTests
    setUp = Borrowed.setUp
    stage = Borrowed.stage
    call = Borrowed.call
    parent = Borrowed.parent
    token_lines = Borrowed.token_lines
    limactl_calls = Borrowed.limactl_calls
    run_until_refused = Borrowed.run_until_refused
    HARNESS, VM_ROOT, FAKE_LIMACTL = Borrowed.HARNESS, Borrowed.VM_ROOT, Borrowed.FAKE_LIMACTL

    def assess_stage(self, calls=1, name="assess-stage.json", **changes):
        """A stage.json as assess-inputs writes it (its call plan from stage_inputs.assess_call_plan)."""
        plan = stage_inputs.assess_call_plan(calls)
        plan["inputs"] = {"combinations": 2, "repositories": 1}
        plan.update(changes)
        path = self.root / name
        path.write_text(json.dumps({"stage": "assess", "mode": "assess", "calls": [], "max_model_calls": calls,
                                    "call_plan": plan}))
        return path

    def test_an_assess_plan_of_one_call_binds_the_ledger_and_refuses_the_second(self):
        plan = self.assess_stage()
        self.assertEqual(0, self.call(plan).returncode)
        refused = self.call(plan)
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertEqual("refused: the ledger already holds 1 model calls\n", refused.stderr)
        self.assertEqual(1, self.token_lines())
        self.assertEqual({"limit": 1, "call_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest()},
                         json.loads((self.ledger_dir / "ledger-limit.json").read_text()))

    def test_another_plan_cannot_rebind_an_assess_ledger(self):
        self.assertEqual(0, self.call(self.assess_stage(2)).returncode)
        for other in (self.assess_stage(3, name="three.json"), self.stage(25)):
            refused = self.call(other)
            self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertEqual(1, self.token_lines())

    def test_the_absolute_cap_holds_for_an_assess_plan(self):
        refused = self.call(self.assess_stage(41))
        self.assertEqual(2, refused.returncode, refused.stderr)
        self.assertIn("absolute cap 40", refused.stderr)
        self.assertFalse(self.ledger_dir.exists())

    def test_a_malformed_assess_plan_is_refused_before_anything_starts(self):
        bad = {
            "zero calls": self.assess_stage(0, name="zero.json"),
            "total disagrees": self.assess_stage(2, name="total.json", max_model_calls=3),
            "an analysis key": self.assess_stage(1, name="mixed.json", final=1),
            "a continuation": self.assess_stage(1, name="continues.json", continues={"completed": {}}),
            "string count": self.assess_stage(1, name="string.json", assess="1"),
            "bool count": self.assess_stage(1, name="bool.json", assess=True, max_model_calls=True),
            "other mode": self.assess_stage(1, name="mode.json", mode="extraction-only"),
        }
        top = json.loads(self.assess_stage(1, name="top.json").read_text())
        top["max_model_calls"] = 2
        (self.root / "top.json").write_text(json.dumps(top))
        bad["top level disagrees"] = self.root / "top.json"
        for label, path in bad.items():
            with self.subTest(label):
                refused = self.call(path)
                self.assertEqual(2, refused.returncode, refused.stderr)
                self.assertIn("call plan", refused.stderr)
        self.assertFalse(self.ledger_dir.exists())
        self.assertEqual(0, self.limactl_calls())

    def test_an_assess_plan_does_not_continue_a_parent_ledger(self):
        self.parent(calls=2)
        child = self.root / "child"
        refused = self.call(self.assess_stage(), ledger=child, parent=self.ledger_dir)
        self.assertEqual(2, refused.returncode, refused.stderr)
        self.assertIn("a parent ledger needs a continue-plan call plan", refused.stderr)
        self.assertFalse(child.exists())

    def test_the_analysis_plans_are_read_as_before(self):
        self.assertEqual(0, self.call(self.stage(25)).returncode)
        self.assertEqual(25, json.loads((self.ledger_dir / "ledger-limit.json").read_text())["limit"])


if __name__ == "__main__":
    unittest.main()
