"""One run directory takes one command at a time (workflow contract, Concurrency; hitchhikers-guide-to-oss#5 S1).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s <skill>/tests -p test_run_lock.py -v
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import offline_fixture as fx  # noqa: E402  (installs the guards first)

import fcntl  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from test_oss_workflow import Flow  # noqa: E402

wf = fx.workflow


class HeldRun:
    """Another command holding the run: the same advisory lock, taken from this test."""

    def __init__(self, run_dir):
        self.fd = os.open(str(run_dir / wf.RUN_MARKER), os.O_RDONLY)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        os.close(self.fd)


class RunLockTests(unittest.TestCase):
    def _initialized(self):
        flow = Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        return flow, paths

    def _snapshot(self, flow):
        return {name: (flow.run_dir / name).read_bytes() for name in (wf.STATE, wf.RUN_MARKER, wf.REQUEST)}

    def test_a_command_on_a_held_run_is_refused_and_leaves_no_trace(self):
        flow, paths = self._initialized()
        before = self._snapshot(flow)
        held = HeldRun(flow.run_dir)
        try:
            for argv in (("adopt-collection", "--corpus", str(paths["corpus"]), "--manifest", str(paths["manifest"])),
                         ("digest",), ("status",), ("report",)):
                code, stdout, stderr = flow.cli(*argv)
                self.assertEqual(2, code, (argv, stdout, stderr))
                self.assertIn("run-busy", stderr)
                self.assertIn("one at a time", stderr)
                self.assertEqual("", stdout)
            # Nothing was read into a new generation, written, or logged: the event log belongs to the holder.
            self.assertEqual(before, self._snapshot(flow))
            self.assertFalse((flow.run_dir / "stages").exists())
            self.assertFalse((flow.run_dir / "report.md").exists())
            # init re-entry of the same request also waits its turn.
            code, _, stderr = flow.cli("init", "--request", str(flow.request_path))
            self.assertEqual(2, code, stderr)
            self.assertIn("run-busy", stderr)
        finally:
            held.release()
        # Released: the same command now runs and is the first generation.
        flow.adopt_collection(paths)
        self.assertEqual(1, flow.state()["stages"]["collect"]["active"])
        self.assertEqual([], [e for e in flow.state()["events"] if e["result"] == "rejected"])

    def test_two_adopts_of_one_run_at_the_same_time_leave_exactly_one_generation(self):
        """S1: the second adopt is refused while the first holds the run; afterwards it is the usual reuse."""
        flow, paths = self._initialized()
        entered, release = threading.Event(), threading.Event()
        real = wf.HANDLERS["adopt-collection"]

        def gated(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(30), "the test never released the first adopt")
            return real(*args, **kwargs)

        first = {}

        def run_first():
            with mock.patch.dict(wf.HANDLERS, {"adopt-collection": gated}):
                first["result"] = flow.cli("adopt-collection", "--corpus", str(paths["corpus"]),
                                           "--manifest", str(paths["manifest"]), "--inventory", str(paths["inventory"]))

        thread = threading.Thread(target=run_first)
        thread.start()
        try:
            self.assertTrue(entered.wait(30), "the first adopt never started")
            code, stdout, stderr = flow.cli("adopt-collection", "--corpus", str(paths["corpus"]),
                                            "--manifest", str(paths["manifest"]), "--inventory", str(paths["inventory"]))
            self.assertEqual((2, ""), (code, stdout), stderr)
            self.assertIn("run-busy", stderr)
        finally:
            release.set()
            thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(0, first["result"][0], first["result"])
        state = flow.state()
        self.assertEqual(1, len(state["stages"]["collect"]["generations"]))
        self.assertEqual(1, state["stages"]["collect"]["active"])
        self.assertEqual([], [e for e in state["events"] if e["result"] == "rejected"])
        self.assertEqual(["gen-001"], sorted(p.name for p in (flow.run_dir / "stages" / "collect").iterdir()))
        # Run one after the other, the second adopt of the same bytes is a reuse, not a new generation.
        self.assertEqual("reused", [e for e in self._events_after(flow, paths)][-1]["result"])
        self.assertEqual(1, len(flow.state()["stages"]["collect"]["generations"]))

    def _events_after(self, flow, paths):
        flow.adopt_collection(paths)
        return flow.state()["events"]

    def test_the_lock_is_released_when_a_command_ends(self):
        flow, paths = self._initialized()
        flow.adopt_collection(paths)
        held = HeldRun(flow.run_dir)  # would raise BlockingIOError if the finished command still held it
        held.release()
        self.assertEqual(0, flow.cli("status")[0])

    def test_contract_and_skill_state_the_rule(self):
        contract = (fx.SKILL_DIR / "references" / "workflow-contract.md").read_text(encoding="utf-8")
        skill = (fx.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## Concurrency", contract)
        self.assertIn("Sequential only", contract)
        self.assertIn("`run-busy`", contract)
        self.assertIn("one command at a time", skill)


if __name__ == "__main__":
    unittest.main()
