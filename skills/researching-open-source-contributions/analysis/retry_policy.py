#!/usr/bin/env python3
"""#280: the bounded retry of a model call whose answer broke a model output rule.

One policy (run_limits.json model_retry) and its three users:

  stage_inputs.py      retry_share(): the retry share every analysis call plan holds (call_plan.retry), counted in
                       max_model_calls (the ledger limit) and in the worst-case plan against the absolute cap 40
  harness/analysis_call.sh --retry-of PREVIOUS
                       ``retry_policy.py admit``: whether this call may be the next attempt of PREVIOUS, before
                       anything starts; its answer becomes the retry fields of the ledger line
  harness/call_with_retry.py
                       classify(): whether a strict verdict is retried

Retried: an extract, synthesis, verify or final answer whose strict verdict (verify_e2e_result.py, limits strict)
failed only on answer-content items (RETRYABLE_ITEMS: R2 not one JSON object, R3 shape, size and counts, R5 input
scope, R6 search clues, Q1 quotes), its other items passing or blocked by that failure. A retry sends the same
prompt bytes again (same input sha256), its answer goes through the same strict check, and nothing is edited:
not the prompt, not the answer, not a limit.

Not retried: a harness exit (staging, transport, log not proven clean, ledger limit, call plan binding), a call
whose ledger line is not clean (measured launch, pickup, judge, observed model), a verdict failing any other item
(C, P, S, M, R0, R1, A items), an assess answer, a legacy verdict, a verdict that was ok but not adopted, and a
refusal of a gate run before the call (cost). Those stop the run as before.

Each call has at most ``per_call`` retries (attempts <name>-r2 .. -r<1+per_call>); each stage at most
``stages[stage]`` (``per_stage``, and no more than its calls x per_call); the plan at most ``total`` in all.

  retry_policy.py admit LEDGER_DIR PREVIOUS NAME PROMPT_FILE CALL_PLAN_FILE
      prints the retry fields as one JSON object; exit 2 when the call cannot be a retry (no retry share in the
      plan, a name other than <first call>-r<attempt>), 3 when this retry is refused (no such clean call of this
      prompt, its verdict missing or not retryable, the per-call, stage or plan share spent).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
POLICY = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))["model_retry"]
STAGES = ("extract", "synthesis", "verify", "final")
STAGE_OF_KIND = {"extract": "extract", "synthesis": "synthesis", "verify": "verify", "final": "final"}
RETRYABLE_ITEMS = ("R2-report-json", "R3-report-schema", "R5-input-scope", "R6-search-clues", "Q1-quotes-verbatim")
VERDICT_MAX_BYTES = 256 * 1024


class Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def retry_share(calls: dict, policy: dict | None = None) -> dict:
    """The retry share of a plan of ``calls`` per stage: per_call retries of a call, at most per_stage (and the
    stage's calls x per_call) in a stage, at most total in the plan."""
    policy = POLICY if policy is None else policy
    stages = {stage: min(policy["per_stage"], calls.get(stage, 0) * policy["per_call"]) for stage in STAGES}
    return {"per_call": policy["per_call"], "per_stage": policy["per_stage"], "stages": stages,
            "total": min(policy["total"], sum(stages.values()))}


def classify(verdict) -> tuple[bool, str, list]:
    """(retryable, why, failed item ids) of a strict verdict document."""
    if not isinstance(verdict, dict) or not isinstance(verdict.get("items"), list):
        return False, "not a verdict document", []
    if verdict.get("ok") is True:
        return False, "the verdict passed", []
    if verdict.get("limits") != "strict":
        return False, "not a strict verdict (limits %r)" % (verdict.get("limits"),), []
    if verdict.get("answer_kind") not in STAGE_OF_KIND:
        return False, "a %r answer is not retried" % (verdict.get("answer_kind"),), []
    failed = [item.get("id") for item in verdict["items"] if isinstance(item, dict) and item.get("status") == "FAIL"]
    if not failed:
        return False, "no item failed (%s)" % (verdict.get("adopt_error") or "not ok"), []
    other = [item for item in failed if item not in RETRYABLE_ITEMS]
    if other:
        return False, "failed beyond the answer's content: %s" % ", ".join(map(str, other)), failed
    return True, "an answer-content failure: %s" % ", ".join(failed), failed


def plan_share(plan: dict) -> dict | None:
    """The retry share a call plan holds, or None (a plan from before #280, or a share of 0)."""
    share = plan.get("retry")
    if not isinstance(share, dict) or type(share.get("total")) is not int or share["total"] < 1:
        return None
    return share


def regular_bytes(path: Path, label: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise Refused(3, "%s unreadable (%s): not retried" % (label, error.strerror))
    with os.fdopen(fd, "rb") as handle:
        if not os.path.isfile(path) or os.fstat(handle.fileno()).st_size > VERDICT_MAX_BYTES:
            raise Refused(3, "%s is not a bounded regular file: not retried" % label)
        return handle.read()


def clean(line: dict, prompt_sha: str) -> list:
    """What makes a ledger line other than a clean call of this prompt (empty when it is one)."""
    want = {"kind": "token", "status": "succeeded", "log_clean": True, "harness_exit": 0, "pickup_exit": 0,
            "judge_exit": 0}
    problems = ["%s %r" % (key, line.get(key)) for key, value in want.items() if line.get(key) != value]
    if line.get("input_sha256") != prompt_sha or line.get("prompt_sha256_recorded") != prompt_sha:
        problems.append("another prompt (the retry sends the same prompt bytes)")
    return problems


def admit(ledger_dir: Path, previous: str, name: str, prompt_sha: str, plan: dict) -> dict:
    """The retry fields of NAME as the next attempt of PREVIOUS in this ledger, or Refused."""
    share = plan_share(plan)
    if share is None:
        raise Refused(2, "this call plan holds no retry share: a call is not retried under it")
    lines = []
    ledger = ledger_dir / "ledger.jsonl"
    if ledger.is_file() and not ledger.is_symlink():
        for text in ledger.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(text)
            except ValueError:
                entry = None
            lines.append(entry if isinstance(entry, dict) else {})
    mine = [line for line in lines if line.get("name") == previous and line.get("kind") == "token"]
    if len(mine) != 1:
        raise Refused(3, "the ledger holds no token call %s to retry (%d lines): not retried" % (previous, len(mine)))
    line = mine[0]
    root, attempt = line.get("retry_of", previous), line.get("attempt", 1) + 1
    if name != "%s-r%d" % (root, attempt):
        raise Refused(2, "a retry of %s is named %s-r%d, not %s" % (previous, root, attempt, name))
    if any(other.get("retry_of") == root and other.get("attempt", 0) >= attempt for other in lines):
        raise Refused(3, "%s was already retried: not retried again" % previous)
    if attempt - 1 > share["per_call"]:
        raise Refused(3, "the per-call retry share (%d) of %s is spent" % (share["per_call"], root))
    problems = clean(line, prompt_sha)
    if problems:
        raise Refused(3, "%s is not a clean call of this prompt (%s): not retried (the retry sends the same prompt "
                         "bytes)" % (previous, "; ".join(problems)))
    data = regular_bytes(ledger_dir / previous / "verdict.json", "the strict verdict of %s" % previous)
    try:
        verdict = json.loads(data)
    except ValueError:
        verdict = None
    retryable, why, failed = classify(verdict)
    if not retryable:
        raise Refused(3, "the verdict of %s is not retried: %s" % (previous, why))
    checked = {item.get("id"): item for item in verdict["items"] if isinstance(item, dict)}
    if checked.get("P3-prompt-sha256", {}).get("detail") != prompt_sha:
        raise Refused(3, "the verdict of %s checked another prompt: not retried" % previous)
    stage = STAGE_OF_KIND[verdict["answer_kind"]]
    retries = [other for other in lines if "attempt" in other and other.get("kind") == "token"]
    allowed = share.get("stages", {}).get(stage, 0)
    if sum(other.get("retry_stage") == stage for other in retries) >= allowed:
        raise Refused(3, "the %s retry share (%d) is spent" % (stage, allowed))
    if len(retries) >= share["total"]:
        raise Refused(3, "the retry share of the plan (%d) is spent" % share["total"])
    return {"attempt": attempt, "retry_of": root, "previous": previous, "retry_stage": stage,
            "failure_sha256": hashlib.sha256(data).hexdigest(), "failed_items": failed}


def main(argv: list) -> int:
    if len(argv) != 6 or argv[0] != "admit":
        print("usage: retry_policy.py admit LEDGER_DIR PREVIOUS NAME PROMPT_FILE CALL_PLAN_FILE", file=sys.stderr)
        return 2
    ledger_dir, previous, name, prompt, plan_file = argv[1:]
    try:
        prompt_sha = hashlib.sha256(Path(prompt).read_bytes()).hexdigest()
        plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))["call_plan"]
        record = admit(Path(ledger_dir), previous, name, prompt_sha, plan)
    except Refused as refusal:
        print("refused: %s" % refusal, file=sys.stderr)
        return refusal.code
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("refused: unreadable prompt or call plan (%s)" % error, file=sys.stderr)
        return 2
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
