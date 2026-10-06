"""The bounded retry of a model call whose answer broke a model output rule (#280).

retry_policy.py is the one policy: the retry share a call plan holds (run_limits.json model_retry), which strict
verdicts may be retried (an answer-content failure only: R2, R3, R5, R6, Q1), and the admission analysis_call.sh
asks before a call that is the next attempt of a failed one (--retry-of). harness/call_with_retry.py drives one
planned call: the harness call, the ledger line check, the strict check and adoption, and a retry of the same
prompt bytes while the verdict is retryable and the plan's share allows it.

The S1-S3 cases run the real harness and driver as a copy whose VM root is a fake limactl (as
tests/test_oss_research_worker_linux.py AnalysisCallLedgerTests does), with a fake pickup verifier, a fake
judge and a fake strict checker beside them: each fake model answer names the verdict it gets. No VM, model,
network or token is used; the 2026-10-06 split gate run's verdicts are read when this machine has them.

  /opt/homebrew/bin/python3 -B -m unittest dot_agents/skills/researching-open-source-contributions/analysis/test_model_retry.py
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import retry_policy  # noqa: E402
import machine_settings as machine  # noqa: E402

SPLIT_GATE = machine.VM_LOGS / "a78-split-gate-20261006T055754Z"
BUNDLE = "dot_agents/skills/researching-open-source-contributions/analysis"
MODEL = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))["worker"]["model"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The fake strict checker: the answer's "_fake" names the verdict (pass, Q1, R3, R6, M5). Its items are the real
# verifier's ids; P3 records the prompt sha as the real one does.
FAKE_VERIFIER = r'''
import argparse, hashlib, json, os, sys

def verdict_document(mode, prompt_sha, kind):
    items = [{"id": "C1-corpus-nonempty", "status": "PASS"}, {"id": "P3-prompt-sha256", "status": "INFO",
             "detail": prompt_sha}, {"id": "M1-run-succeeded", "status": "PASS"},
             {"id": "M5-observed-models", "status": "FAIL" if mode == "M5" else "PASS"},
             {"id": "P4-prompt-binding", "status": "PASS"}, {"id": "R1-report-present", "status": "PASS"},
             {"id": "R2-report-json", "status": "PASS"},
             {"id": "R3-report-schema", "status": "FAIL" if mode == "R3" else "PASS",
              "detail": "the answer is 8342 bytes as a prompt holds it (> extract_answer_max_bytes 7680)"
              if mode == "R3" else ""}]
    if mode == "R3":
        items.append({"id": "Q1", "status": "BLOCKED", "detail": "schema failed"})
    else:
        items.append({"id": "R6-search-clues", "status": "FAIL" if mode == "R6" else "PASS"})
        items.append({"id": "Q1-quotes-verbatim", "status": "FAIL" if mode == "Q1" else "PASS",
                      "detail": "12/14 verified (answer holds 14), check_extraction exit 1" if mode == "Q1" else ""})
    return {"ok": mode == "pass", "result_class": "evidence" if mode == "pass" else "invalid", "answer_kind": kind,
            "limits": "strict", "items": items}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for name in ("--run-dir", "--prompt", "--answer-kind", "--out", "--adopt-to", "--corpus", "--lens"):
        ap.add_argument(name)
    a = ap.parse_args()
    report = open(os.path.join(a.run_dir, "result", "report.md"), "rb").read()
    document = verdict_document(json.loads(report).get("_fake", "pass"),
                                hashlib.sha256(open(a.prompt, "rb").read()).hexdigest(), a.answer_kind)
    if document["ok"] and a.adopt_to:
        fd = os.open(a.adopt_to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        os.write(fd, report)
        os.close(fd)
        document["adopted_to"] = a.adopt_to
    open(a.out, "w").write(json.dumps(document))
    sys.exit(0 if document["ok"] else 1)
'''
FAKE_PICKUP = r'''
import json, os, sys, tarfile
os.makedirs(sys.argv[3])
with tarfile.open(sys.argv[1]) as tar:
    tar.extractall(sys.argv[3], filter="data")
print(json.dumps({"ok": True}))
'''
FAKE_JUDGE = "print('PASS')\n"
# Builds a unit's evidence from the next queued fake answer: {"_fake": ..., "models": [...]} (models default: the
# worker model), as the guest leaves it for the harness's sudo cat.
MAKE_RESULT = r'''
import hashlib, io, json, os, sys, tarfile
answer = json.loads(open(sys.argv[1]).read())
staged = open(sys.argv[2], "rb").read()
models = answer.pop("models", [sys.argv[4]])
report = json.dumps(answer, ensure_ascii=False).encode()
os.makedirs(sys.argv[3])
manifest = json.dumps({"status": "succeeded", "observed_models": models, "permission_mode": "dontAsk",
                       "prompt_sha256": hashlib.sha256(staged).hexdigest(),
                       "report_sha256": hashlib.sha256(report).hexdigest()}).encode()
with tarfile.open(os.path.join(sys.argv[3], "result.tar"), "w") as tar:
    for name, data in (("manifest.json", manifest), ("report.md", report)):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
open(os.path.join(sys.argv[3], "record.json"), "w").write("{}")
open(os.path.join(sys.argv[3], "task.txt"), "wb").write(staged)
'''
FAKE_LIMACTL = r'''#!/bin/bash
V="$(cd "$(dirname "$0")/../.." && pwd)"
echo "$*" >> "$V/calls.log"
n=$(( $(cat "$V/count" 2>/dev/null || echo 0) + 1 ))
case "$*" in
  *"cat > "*task.txt*)
    cat > "$V/staged.txt"
    if grep -q '"_fake": "stage-differs"' "$V/queue/$n.json"; then echo 0; else shasum -a 256 "$V/staged.txt" | cut -c1-64; fi ;;
  *model_once.sh*)
    cat > /dev/null; echo "$n" > "$V/count"
    unit=$(printf 'oss-research-%016x' "$n")
    python3 "$V/make_result.py" "$V/queue/$n.json" "$V/staged.txt" "$V/evidence/$unit" "$MODEL"
    echo "INFO R wrapper {\"unit\": \"$unit\"}"
    echo "SUMMARY fails=0" ;;
  *"sudo cat /var/lib/oss-research-evidence/"*)
    f="${*: -1}"; cat "$V/evidence/${f#/var/lib/oss-research-evidence/}" ;;
esac
exit 0
'''.replace("$MODEL", MODEL)


class RetryFixture(unittest.TestCase):
    """A repository copy holding the real harness, driver, retry policy and run limits, and the fakes."""

    VM_ROOT = "W=${OSS_RESEARCH_VM_ROOT:-/Users/lee-kyu-hwan/.oss-research-vm}\n"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osrl-retry-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.vm = self.root / "vm"
        (self.vm / "tool/bin").mkdir(parents=True)
        (self.vm / "queue").mkdir()
        limactl = self.vm / "tool/bin/limactl"
        limactl.write_text(FAKE_LIMACTL)
        limactl.chmod(0o755)
        (self.vm / "make_result.py").write_text(MAKE_RESULT)
        bundle = self.root / "repo" / BUNDLE
        (bundle / "harness").mkdir(parents=True)
        harness = (HERE / "harness/analysis_call.sh").read_text()
        self.assertEqual(1, harness.count(self.VM_ROOT))
        (bundle / "harness/analysis_call.sh").write_text(harness.replace(self.VM_ROOT, "W=%s\n" % self.vm))
        for name in ("harness/call_with_retry.py", "retry_policy.py", "run_limits.json"):
            shutil.copy(HERE / name, bundle / name)
        (bundle / "verify_e2e_result.py").write_text(FAKE_VERIFIER)
        linux = self.root / "repo/dot_local/share/oss-research/linux"
        linux.mkdir(parents=True)
        (linux / "verify_pickup.py").write_text(FAKE_PICKUP)
        (linux / "model_once_verdict.py").write_text(FAKE_JUDGE)
        self.harness = bundle / "harness/analysis_call.sh"
        self.driver = bundle / "harness/call_with_retry.py"
        self.ledger_dir = self.root / "ledger"
        self.ledger = self.ledger_dir / "ledger.jsonl"
        self.answers = self.root / "answers"
        self.answers.mkdir()
        self.prompt = self.root / "task.txt"
        self.prompt.write_text("extract the patterns of these PRs\n")
        self.credential = self.root / "cred"
        self.credential.write_text("sentinel-" + "c" * 40 + "\n")
        self.queued = 0
        self.plan = self.stage()

    def stage(self, name="stage.json", share=None, **changes):
        """An extract stage.json of 2 extract, 2 synthesis, 1 verify and 1 final calls with its retry share."""
        calls = {"extract": 2, "synthesis": 2, "verify": 1, "final": 1}
        retry = retry_policy.retry_share(calls) if share is None else share
        plan = {"mode": "extraction-only", "shards": 1, "lenses": 2, **calls, "retry": retry,
                "max_model_calls": sum(calls.values()) + retry["total"], "formula": "..."}
        plan.update(changes)
        path = self.root / name
        path.write_text(json.dumps({"stage": "extract", "max_model_calls": plan["max_model_calls"], "call_plan": plan}))
        return path

    def queue(self, *answers):
        """The fake model's next answers, in call order: "pass", "Q1", ... or a dict (models, _fake)."""
        for answer in answers:
            self.queued += 1
            document = {"_fake": answer} if isinstance(answer, str) else dict(answer)
            document.setdefault("lens", "blind")
            (self.vm / "queue" / ("%d.json" % self.queued)).write_text(json.dumps(document))

    def drive(self, name="extract-blind-01", plan=None, prompt=None, extra=()):
        argv = [sys.executable, "-I", "-B", str(self.driver), "--ledger", str(self.ledger_dir), "--name", name,
                "--prompt", str(prompt or self.prompt), "--commit", "0" * 40, "--credential", str(self.credential),
                "--call-plan", str(plan or self.plan), "--answer-kind", "extract",
                "--adopt-to", str(self.answers / (name + ".result.json")), *extra,
                "--", "--corpus", str(self.root / "corpus.json"), "--lens", "blind"]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)

    def call(self, name, retry_of=None, plan=None, prompt=None):
        argv = ["bash", str(self.harness)] + ([] if retry_of is None else ["--retry-of", retry_of]) \
            + [str(self.ledger_dir), name, str(prompt or self.prompt), "0" * 40, "token", str(self.credential),
               str(plan or self.plan)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def verdict(self, name, mode, kind="extract", prompt_sha=None):
        """The fake checker's verdict of a call, written where the driver leaves it."""
        scope = {}
        exec(FAKE_VERIFIER, scope)
        document = scope["verdict_document"](mode, prompt_sha or sha256(self.prompt.read_bytes()), kind)
        (self.ledger_dir / name / "verdict.json").write_text(json.dumps(document))

    def lines(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()] if self.ledger.exists() else []

    def model_calls(self):
        log = self.vm / "calls.log"
        return sum("model_once.sh" in line for line in log.read_text().splitlines()) if log.exists() else 0


class DriverTests(RetryFixture):
    """S1-S3: the driver retries an answer-content failure once with the same prompt bytes, and nothing else."""

    def test_s1_a_quote_failure_is_retried_once_with_the_same_prompt_and_the_retry_is_adopted(self):
        self.queue("Q1", "pass")
        done = self.drive()
        self.assertEqual(0, done.returncode, done.stderr)
        first, retry = self.lines()
        self.assertEqual(("extract-blind-01", "extract-blind-01-r2"), (first["name"], retry["name"]))
        self.assertNotIn("attempt", first)
        failed = (self.ledger_dir / "extract-blind-01/verdict.json").read_bytes()
        self.assertEqual({"attempt": 2, "retry_of": "extract-blind-01", "previous": "extract-blind-01",
                          "retry_stage": "extract", "failure_sha256": sha256(failed),
                          "failed_items": ["Q1-quotes-verbatim"]},
                         {key: retry[key] for key in ("attempt", "retry_of", "previous", "retry_stage",
                                                      "failure_sha256", "failed_items")})
        prompt_sha = sha256(self.prompt.read_bytes())
        self.assertEqual([prompt_sha] * 2, [line["input_sha256"] for line in (first, retry)])
        adopted = (self.answers / "extract-blind-01.result.json").read_bytes()
        self.assertEqual(retry["report_sha256"], sha256(adopted))
        self.assertTrue(json.loads((self.ledger_dir / "extract-blind-01-r2/verdict.json").read_text())["ok"])
        summary = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual((True, ["extract-blind-01", "extract-blind-01-r2"]),
                         (summary["adopted"], [attempt["name"] for attempt in summary["attempts"]]))

    def test_s1_a_size_failure_with_its_quotes_blocked_is_retried(self):
        self.queue("R3", "pass")
        done = self.drive()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["R3-report-schema"], self.lines()[1]["failed_items"])

    def test_a_first_answer_that_passes_is_one_ledger_line(self):
        self.queue("pass")
        done = self.drive()
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["extract-blind-01"], [line["name"] for line in self.lines()])

    def test_s2_the_per_call_share_spent_stops_the_call(self):
        self.queue("Q1", "Q1", "pass")
        done = self.drive()
        self.assertEqual(11, done.returncode, done.stderr)
        self.assertIn("the per-call retry share (1) is spent", done.stderr)
        self.assertEqual(2, len(self.lines()))
        self.assertEqual(2, self.model_calls())
        self.assertFalse((self.answers / "extract-blind-01.result.json").exists())
        # nor can the harness be asked for a third attempt by hand
        self.verdict("extract-blind-01-r2", "Q1")
        refused = self.call("extract-blind-01-r3", retry_of="extract-blind-01-r2")
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertIn("per-call retry share (1)", refused.stderr)
        self.assertEqual(2, len(self.lines()))

    def test_s2_the_plan_share_spent_stops_a_later_call(self):
        share = {"per_call": 1, "per_stage": 3, "stages": {"extract": 1, "synthesis": 0, "verify": 0, "final": 0},
                 "total": 1}
        plan = self.stage("one.json", share=share)
        self.queue("Q1", "pass", "R3", "pass")
        self.assertEqual(0, self.drive("extract-blind-01", plan=plan).returncode)
        done = self.drive("extract-blind-02", plan=plan)
        self.assertEqual(11, done.returncode, done.stderr)
        self.assertIn("the extract retry share (1) is spent", done.stderr)
        self.assertEqual(["extract-blind-01", "extract-blind-01-r2", "extract-blind-02"],
                         [line["name"] for line in self.lines()])

    def test_s3_a_failure_outside_the_answer_is_not_retried(self):
        cases = {"observed model": ({"_fake": "pass", "models": ["claude-sonnet-5-5"]}, 10, 1),
                 "observed model in the verdict": ("M5", 11, 1),
                 "staging (transport)": ("stage-differs", 10, 0)}
        for label, (answer, code, lines) in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.ledger_dir, ignore_errors=True)
                self.queue(answer, "pass")
                before = self.model_calls()
                done = self.drive()
                self.assertEqual(code, done.returncode, done.stderr)
                self.assertIn("not retried", done.stderr)
                self.assertEqual(lines, len(self.lines()))
                self.assertEqual(lines, self.model_calls() - before)
                self.assertNotIn("attempt", "".join(json.dumps(line) for line in self.lines()))
                (self.vm / "count").write_text(str(self.queued))   # drop the unused "pass"

    def test_s3_a_log_holding_the_credential_is_not_retried(self):
        self.queue("pass", "pass")
        self.credential.write_text("SUMMARY\n")
        done = self.drive()
        self.assertEqual(10, done.returncode, done.stderr)
        self.assertEqual([False], [line["log_clean"] for line in self.lines()])

    def test_s3_a_ledger_bound_to_another_plan_is_not_retried(self):
        self.queue("pass")
        self.assertEqual(0, self.drive("extract-blind-01").returncode)
        self.queue("pass")
        done = self.drive("extract-blind-02", plan=self.stage("other.json", formula="another plan"))
        self.assertEqual(10, done.returncode, done.stderr)
        self.assertIn("ledger limit", done.stderr)
        self.assertEqual(1, len(self.lines()))

    def test_a_refusing_gate_stops_before_any_call(self):
        self.queue("pass")
        done = self.drive(extra=("--gate", "false"))
        self.assertEqual(12, done.returncode, done.stderr)
        self.assertEqual(0, self.model_calls())
        self.assertFalse(self.ledger_dir.exists())


class AdmissionTests(RetryFixture):
    """analysis_call.sh --retry-of PREVIOUS: the admission retry_policy.py gives, before anything starts."""

    def failed_call(self, mode="Q1", name="extract-blind-01"):
        self.queue(mode)
        done = self.call(name)
        self.assertEqual(0, done.returncode, done.stderr)
        self.verdict(name, mode)

    def test_a_retry_is_admitted_and_recorded_with_its_attempt_and_failure(self):
        self.failed_call("R6")
        self.queue("pass")
        done = self.call("extract-blind-01-r2", retry_of="extract-blind-01")
        self.assertEqual(0, done.returncode, done.stderr)
        retry = self.lines()[1]
        self.assertEqual((2, "extract-blind-01", ["R6-search-clues"]),
                         (retry["attempt"], retry["retry_of"], retry["failed_items"]))

    def test_refusals_before_the_call(self):
        self.failed_call()
        other = self.root / "other.txt"
        other.write_text("another prompt\n")
        cases = {
            "another name": (dict(name="extract-blind-01-retry", retry_of="extract-blind-01"), 2, "is named"),
            "no such call": (dict(name="extract-blind-09-r2", retry_of="extract-blind-09"), 3, "no token call"),
            "other prompt bytes": (dict(name="extract-blind-01-r2", retry_of="extract-blind-01", prompt=other), 3,
                                   "same prompt bytes"),
            "another plan": (dict(name="extract-blind-01-r2", retry_of="extract-blind-01",
                                  plan=self.stage("another.json", formula="another")), 3, "ledger limit"),
        }
        for label, (kwargs, code, message) in cases.items():
            with self.subTest(label):
                refused = self.call(**kwargs)
                self.assertEqual(code, refused.returncode, refused.stderr)
                self.assertIn(message, refused.stderr)
        self.assertEqual(1, len(self.lines()))
        self.assertEqual(1, self.model_calls())

    def test_a_plan_without_a_retry_share_admits_no_retry(self):
        plans = {"an earlier plan": self.stage("earlier.json", share={"total": 0}),
                 "a zero share": self.stage("zero.json", share={"per_call": 1, "per_stage": 3, "total": 0,
                                                                "stages": {"extract": 0, "synthesis": 0, "verify": 0,
                                                                           "final": 0}})}
        earlier = json.loads(plans["an earlier plan"].read_text())
        del earlier["call_plan"]["retry"]                       # a stage.json from before #280
        plans["an earlier plan"].write_text(json.dumps(earlier))
        for label, plan in plans.items():
            with self.subTest(label):
                shutil.rmtree(self.ledger_dir, ignore_errors=True)
                self.queue("Q1")
                self.assertEqual(0, self.call("extract-blind-01", plan=plan).returncode)
                self.verdict("extract-blind-01", "Q1")
                refused = self.call("extract-blind-01-r2", retry_of="extract-blind-01", plan=plan)
                self.assertEqual(2, refused.returncode, refused.stderr)
                self.assertIn("holds no retry share", refused.stderr)
                self.assertEqual(1, len(self.lines()))

    def test_a_retry_needs_a_call_plan(self):
        refused = subprocess.run(["bash", str(self.harness), "--retry-of", "extract-blind-01", str(self.ledger_dir),
                                  "extract-blind-01-r2", str(self.prompt), "0" * 40, "token", str(self.credential)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(2, refused.returncode, refused.stderr)
        self.assertIn("a retry needs the call plan", refused.stderr)
        self.assertFalse(self.ledger_dir.exists())

    def test_a_verdict_that_is_missing_passed_or_not_retryable_is_refused(self):
        for mode in (None, "pass", "M5"):
            with self.subTest(mode):
                shutil.rmtree(self.ledger_dir, ignore_errors=True)
                self.queue("Q1")
                self.assertEqual(0, self.call("extract-blind-01").returncode)
                if mode is not None:
                    self.verdict("extract-blind-01", mode)
                refused = self.call("extract-blind-01-r2", retry_of="extract-blind-01")
                self.assertEqual(3, refused.returncode, refused.stderr)
                self.assertIn("not retried", refused.stderr)
                self.assertEqual(1, len(self.lines()))

    def test_a_verdict_of_another_prompt_is_refused(self):
        self.failed_call()
        self.verdict("extract-blind-01", "Q1", prompt_sha="0" * 64)
        refused = self.call("extract-blind-01-r2", retry_of="extract-blind-01")
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertIn("checked another prompt", refused.stderr)

    def test_a_previous_call_that_was_not_clean_is_refused(self):
        self.queue("pass")
        self.credential.write_text("SUMMARY\n")
        self.assertEqual(5, self.call("extract-blind-01").returncode)
        self.credential.write_text("sentinel-" + "c" * 40 + "\n")
        (self.ledger_dir / "extract-blind-01" / "verdict.json").write_text("{}")
        refused = self.call("extract-blind-01-r2", retry_of="extract-blind-01")
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertIn("not a clean call", refused.stderr)

    def test_a_call_is_retried_once_only(self):
        self.failed_call()
        self.queue("pass", "pass")
        self.assertEqual(0, self.call("extract-blind-01-r2", retry_of="extract-blind-01").returncode)
        refused = self.call("extract-blind-01-r2", retry_of="extract-blind-01")
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertIn("already retried", refused.stderr)

    def test_planned_calls_stop_at_the_plan_and_the_share_is_for_retries_only(self):
        share = {"per_call": 1, "per_stage": 3, "stages": {"extract": 1, "synthesis": 0, "verify": 0, "final": 0},
                 "total": 1}
        plan = self.stage("small.json", share=share, lenses=1, extract=1, synthesis=0, verify=0, final=1,
                          max_model_calls=3)
        self.queue("Q1", "pass", "pass")
        self.assertEqual(0, self.call("extract-blind-01", plan=plan).returncode)
        self.verdict("extract-blind-01", "Q1")
        self.assertEqual(0, self.call("final", plan=plan).returncode)
        refused = self.call("final-2", plan=plan)
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertEqual("refused: the ledger already holds 2 planned model calls; the other 1 are the retry share, "
                         "for a retry (--retry-of) only\n", refused.stderr)
        self.assertEqual(0, self.call("extract-blind-01-r2", retry_of="extract-blind-01", plan=plan).returncode)
        refused = self.call("final-3", plan=plan)
        self.assertEqual(3, refused.returncode, refused.stderr)
        self.assertEqual("refused: the ledger already holds 3 model calls\n", refused.stderr)
        self.assertEqual(3, len(self.lines()))


class ClassifyTests(unittest.TestCase):
    """Which strict verdicts are retried: an answer-content failure of an analysis answer, nothing else."""

    def verdict(self, failed=(), blocked=(), **changes):
        items = [{"id": "M5-observed-models", "status": "PASS"}, {"id": "P3-prompt-sha256", "status": "INFO"}]
        items += [{"id": item, "status": "FAIL"} for item in failed]
        items += [{"id": item, "status": "BLOCKED"} for item in blocked]
        document = {"ok": not failed and not blocked, "limits": "strict", "answer_kind": "extract", "items": items}
        document.update(changes)
        return document

    def test_answer_content_failures_are_retryable(self):
        for failed in (["Q1-quotes-verbatim"], ["R3-report-schema"], ["R2-report-json"], ["R5-input-scope"],
                       ["R6-search-clues"], ["R3-report-schema", "R6-search-clues"]):
            with self.subTest(failed):
                retryable, _, items = retry_policy.classify(self.verdict(failed, blocked=["Q1"]))
                self.assertEqual((True, failed), (retryable, items))
        for kind in ("synthesis", "verify", "final"):
            self.assertTrue(retry_policy.classify(self.verdict(["R3-report-schema"], answer_kind=kind))[0])

    def test_everything_else_is_not_retried(self):
        cases = {
            "passed": self.verdict(),
            "observed model": self.verdict(["M5-observed-models"]),
            "prompt binding beside a quote failure": self.verdict(["P4-prompt-binding", "Q1-quotes-verbatim"]),
            "credential marker in the report": self.verdict(["R0-report-has-no-credential-marker"]),
            "report not the manifest's": self.verdict(["R1-report-present"]),
            "result tree": self.verdict(["S1-result-tree-safe"]),
            "legacy limits": self.verdict(["Q1-quotes-verbatim"], limits="legacy"),
            "an assess answer": self.verdict(["R3-report-schema"], answer_kind="assess"),
            "not adopted": dict(self.verdict(), ok=False, adopt_error="not adopted: File exists"),
            "nothing failed but not ok": dict(self.verdict(), ok=False),
            "only blocked": self.verdict(blocked=["Q1"]),
            "not a verdict": [],
        }
        for label, document in cases.items():
            with self.subTest(label):
                retryable, reason, _ = retry_policy.classify(document)
                self.assertFalse(retryable)
                self.assertTrue(reason)

    @unittest.skipUnless((SPLIT_GATE / "ledger/extract-blind-03/verdict.json").is_file(),
                         "the 2026-10-06 split gate run is only on the machine that made it")
    def test_the_split_gate_violations_are_retryable_and_its_retry_passed(self):
        def read(name):
            return json.loads((SPLIT_GATE / "ledger" / name / "verdict.json").read_text(encoding="utf-8"))
        self.assertEqual((True, ["Q1-quotes-verbatim"]), retry_policy.classify(read("extract-structure-01"))[::2])
        self.assertEqual((True, ["R3-report-schema"]), retry_policy.classify(read("extract-blind-03"))[::2])
        self.assertFalse(retry_policy.classify(read("extract-structure-01-r2"))[0])


class ShareTests(unittest.TestCase):
    def test_run_limits_hold_the_one_retry_policy(self):
        limits = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
        self.assertEqual({"per_call": 1, "per_stage": 3, "total": 5}, limits["model_retry"])
        self.assertEqual(limits["model_retry"], retry_policy.POLICY)

    def test_the_share_is_per_call_per_stage_and_in_total_at_most_the_policy(self):
        self.assertEqual({"per_call": 1, "per_stage": 3, "stages": {"extract": 3, "synthesis": 3, "verify": 3,
                                                                     "final": 1}, "total": 5},
                         retry_policy.retry_share({"extract": 15, "synthesis": 8, "verify": 3, "final": 1}))
        self.assertEqual({"per_call": 1, "per_stage": 3, "stages": {"extract": 0, "synthesis": 0, "verify": 2,
                                                                     "final": 1}, "total": 3},
                         retry_policy.retry_share({"extract": 0, "synthesis": 0, "verify": 2, "final": 1}))
        self.assertEqual(0, retry_policy.retry_share({"extract": 0, "synthesis": 0, "verify": 0, "final": 0})["total"])


if __name__ == "__main__":
    unittest.main()
