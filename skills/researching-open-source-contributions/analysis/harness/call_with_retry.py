#!/usr/bin/env python3
"""#280: one planned model call of the analysis, its strict check and adoption, and its bounded retry.

  call_with_retry.py --ledger LEDGER_DIR --name NAME --prompt PROMPT_FILE --commit COMMIT
      --credential CREDENTIAL_FILE --call-plan CALL_PLAN_FILE [--parent-ledger PARENT_LEDGER_DIR]
      --answer-kind extract|synthesis|verify|final|assess --adopt-to ANSWER.result.json [--gate COMMAND]
      [-- VERIFY_ARG ...]

Each attempt: --gate (when given; e.g. a cost check) must exit 0; analysis_call.sh makes the call (attempt 1 as
NAME, attempt n as NAME-r<n> with --retry-of the attempt before it); its ledger line must be a clean call of the
worker (status succeeded, the requested model and permission mode of run_limits.json worker observed, pickup,
judge and harness exit 0, the log clean, the prompt sha recorded = the input sha); verify_e2e_result.py checks
the answer under strict limits (--run-dir LEDGER_DIR/<attempt> --prompt PROMPT_FILE --answer-kind KIND
VERIFY_ARG... --out LEDGER_DIR/<attempt>/verdict.json --adopt-to ANSWER) and adopts it when it passes.

A failed verdict is retried (retry_policy.classify: an answer-content failure only) with the same PROMPT_FILE,
while the plan's retry share allows it (per call, per stage, in all; analysis_call.sh and retry_policy.py admit
enforce it, this driver stops at the per-call share). Nothing else is retried. Nothing is edited: the prompt, the
answer and the limits stay as they are.

Exit 0: adopted. 10: a call that is not clean (harness exit, or a ledger line that is not clean), not retried.
11: a strict verdict not retried (not retryable, or the retry share spent). 12: the gate refused. 2: bad usage.
Each attempt is printed as one JSON line on stdout, then the summary line {name, adopted, attempts, stop}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.dont_write_bytecode = True

import retry_policy  # noqa: E402

HARNESS = HERE / "analysis_call.sh"
VERIFIER = HERE.parent / "verify_e2e_result.py"
WORKER = json.loads((HERE.parent / "run_limits.json").read_text(encoding="utf-8"))["worker"]


def ledger_line(ledger: Path, name: str) -> dict | None:
    """The last ledger line of the call NAME."""
    found = None
    if ledger.is_file():
        for text in ledger.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(text)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("name") == name:
                found = entry
    return found


def call_problems(line: dict | None, prompt_sha: str) -> list:
    """What makes a ledger line other than a clean call of the worker on this prompt (empty when it is one)."""
    if line is None:
        return ["no ledger line"]
    problems = retry_policy.clean(line, prompt_sha)
    if line.get("observed_models") != [WORKER["model"]]:
        problems.append("observed models %r, not [%r]" % (line.get("observed_models"), WORKER["model"]))
    if line.get("permission_mode") != WORKER["permission_mode"]:
        problems.append("permission mode %r" % (line.get("permission_mode"),))
    return problems


def stop(summary: dict, code: int, reason: str) -> int:
    summary["stop"] = reason
    print("call_with_retry: %s: %s" % (summary["name"], reason), file=sys.stderr)
    print(json.dumps(summary))
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ledger", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--prompt", required=True, type=Path)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--credential", required=True)
    ap.add_argument("--call-plan", required=True, type=Path)
    ap.add_argument("--parent-ledger")
    ap.add_argument("--answer-kind", required=True, choices=("extract", "synthesis", "verify", "final", "assess"))
    ap.add_argument("--adopt-to", required=True, type=Path)
    ap.add_argument("--gate", help="a command run before every attempt (sh -c); a nonzero exit stops the call")
    ap.add_argument("verify_args", nargs="*", help="after --: the verifier's own arguments (--corpus, --lens, ...)")
    a = ap.parse_args(argv)
    try:
        share = retry_policy.plan_share(json.loads(a.call_plan.read_text(encoding="utf-8"))["call_plan"])
        prompt_sha = hashlib.sha256(a.prompt.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("call_with_retry: unreadable call plan or prompt: %s" % error, file=sys.stderr)
        return 2
    per_call = share["per_call"] if share else 0
    summary = {"name": a.name, "adopted": False, "attempts": []}
    previous = None
    for attempt in range(1, per_call + 2):
        name = a.name if attempt == 1 else "%s-r%d" % (a.name, attempt)
        if a.gate and subprocess.run(["sh", "-c", a.gate]).returncode != 0:
            return stop(summary, 12, "the gate refused before %s: not called" % name)
        argv = ["bash", str(HARNESS)] + (["--retry-of", previous] if previous else []) + [
            str(a.ledger), name, str(a.prompt), a.commit, "token", a.credential, str(a.call_plan)] \
            + ([a.parent_ledger] if a.parent_ledger else [])
        done = subprocess.run(argv, capture_output=True, text=True)
        sys.stderr.write(done.stderr)
        record = {"name": name, "harness_exit": done.returncode}
        summary["attempts"].append(record)
        if done.returncode != 0:
            print(json.dumps(record))
            if previous and done.returncode == 3:
                return stop(summary, 11, "the retry of %s was refused (%s): not retried"
                                         % (previous, done.stderr.strip().splitlines()[-1:]))
            return stop(summary, 10, "analysis_call.sh exit %d (staging, transport, log, ledger limit or plan "
                                     "binding): not retried" % done.returncode)
        problems = call_problems(ledger_line(a.ledger / "ledger.jsonl", name), prompt_sha)
        if problems:
            record["call_problems"] = problems
            print(json.dumps(record))
            return stop(summary, 10, "the call is not clean (%s): not retried" % "; ".join(problems))
        out = a.ledger / name / "verdict.json"
        checked = subprocess.run([sys.executable, "-I", "-B", str(VERIFIER), "--run-dir", str(a.ledger / name),
                                  "--prompt", str(a.prompt), "--answer-kind", a.answer_kind, *a.verify_args,
                                  "--out", str(out), "--adopt-to", str(a.adopt_to)], capture_output=True, text=True)
        sys.stderr.write(checked.stderr)
        try:
            verdict = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdict = None
        record["verify_exit"] = checked.returncode
        if checked.returncode == 0 and isinstance(verdict, dict) and verdict.get("ok") is True:
            record["adopted_to"] = verdict.get("adopted_to")
            print(json.dumps(record))
            summary["adopted"] = True
            summary["stop"] = "adopted"
            print(json.dumps(summary))
            return 0
        retryable, why, failed = retry_policy.classify(verdict)
        record.update(retryable=retryable, why=why, failed_items=failed)
        print(json.dumps(record))
        if not retryable:
            return stop(summary, 11, "strict verdict %s: not retried" % why)
        previous = name
    return stop(summary, 11, "the per-call retry share (%d) is spent: the last attempt failed too" % per_call)


if __name__ == "__main__":
    raise SystemExit(main())
