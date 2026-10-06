#!/bin/bash
# #78 analysis: one isolated model call from the host (macOS), with a ledger.
#
#   bash analysis_call.sh [--retry-of PREVIOUS] LEDGER_DIR NAME PROMPT_FILE COMMIT KIND [CREDENTIAL_FILE]
#                         [CALL_PLAN_FILE] [PARENT_LEDGER_DIR]
#
# KIND token   : a real call. CREDENTIAL_FILE is the approved dedicated oauth-token; it reaches the
#                guest only as non-PTY stdin of model_once.sh (run tmpfs + reaper), never argv or log.
# KIND sentinel: the dry path (analysis-dry: claude off PATH, no child can start); CREDENTIAL_FILE
#                is a fake sentinel.
#
# CALL_PLAN_FILE: the bundle's stage.json (extract-inputs). Its call_plan.max_model_calls becomes the
#                ledger limit instead of 12; a plan over 40 calls, a missing file or a malformed plan is
#                refused before anything starts (exit 2). The first call of a ledger records the limit
#                and the file's sha256 in LEDGER_DIR/ledger-limit.json; a later call whose limit or sha
#                differs (another plan, an edited file, no plan) is refused (exit 3).
#
# PARENT_LEDGER_DIR: continue a ledger whose adopted extract answers are reused (#82). CALL_PLAN_FILE
#                is then the stage.json of stage_inputs.py continue-plan: extract 0, the stages not yet
#                run, and call_plan.continues naming the extract stage.json the parent ledger is bound
#                to (its ledger-limit.json call_plan_sha256). The parent must hold ledger.jsonl and
#                ledger-limit.json and must not be LEDGER_DIR; its token calls (failed ones too) plus
#                this limit must stay within the absolute cap 40 (else exit 2). The first call records
#                the parent ledger's sha256 and call count in LEDGER_DIR/ledger-limit.json; a later call
#                finding the parent changed (grown or edited) is refused (exit 3). A continuation plan
#                without a parent, or a parent with an extract plan, is refused (exit 2).
#                A chain (call_plan.continues.parent_stage "continue": the parent ledger was itself a
#                continuation, and its continue stage.json is what this plan continues) also counts the
#                parent's ancestors: the parent's ledger-limit.json must record its own parent (calls, and
#                ancestors when it was a chain too), and ancestors + parent calls + this limit must stay
#                within 40 (else exit 2). The first call records the ancestors and the parent
#                ledger-limit.json sha256 too, so an ancestor record edited later is refused (exit 3). A
#                plan that is not a chain refuses a parent that records a parent of its own (exit 2).
#                A straight chain (#258): the first call also creates PARENT_LEDGER_DIR/continued-by.json
#                (O_EXCL; this ledger's real path and call plan sha; beside the parent ledger, not in its sha),
#                so the counts above are the whole chain. A parent another ledger already continues is refused
#                (exit 2: continue the latest ledger instead); a later call finding the claim changed, removed
#                or a link is refused (exit 3).
#                An assess plan (stage_inputs.py assess-inputs, #259: stage "assess", call_plan mode "assess" with
#                assess = max_model_calls >= 1 and none of the analysis stage keys) binds a new ledger the same
#                way; it continues no parent (exit 2 with PARENT_LEDGER_DIR).
#                A continue-plan of a run stopped during its extract calls (#280: call_plan.extract > 0) plans the
#                extract calls left: its extract calls and the completed ones must be the shards x lenses.
#
# Retry share (#280): a call plan may hold call_plan.retry {per_call, per_stage, stages, total} (retry_policy.py
#                retry_share); max_model_calls, the ledger limit, is then the planned calls plus total. A call
#                without --retry-of is refused (exit 3) once the ledger holds the planned calls (limit - total), so
#                the share is spent only by retries. A plan without retry (from before #280) has a share of 0.
# --retry-of PREVIOUS: this call is the next attempt of the call PREVIOUS of this ledger, whose strict verdict
#                (LEDGER_DIR/PREVIOUS/verdict.json) failed only on the answer's content. retry_policy.py admit
#                decides before anything starts: a plan without a share or a NAME other than <first>-r<attempt> is
#                exit 2; a previous call that is not a clean token call of the same PROMPT_FILE bytes, a verdict
#                missing, passed, of another prompt or failing anything else, a call already retried, or the
#                per-call, stage or plan share spent is exit 3. The ledger line of a retry also records attempt,
#                retry_of (the first call), previous, retry_stage, failure_sha256 (of that verdict.json) and
#                failed_items. harness/call_with_retry.py drives it.
#
# Steps: refuse when LEDGER_DIR/ledger.jsonl already holds the limit of calls (started calls count,
# failed ones too); stage PROMPT_FILE root-only in the guest (/var/lib/oss-research-staging/<NAME>/task.txt,
# root 0700/0600); run model_once.sh (egress pin for token, none for sentinel); show the log only
# after grep -F finds no credential value (exit 1); pick up the result tar, check it with
# verify_pickup.py (default contract) and judge-worker (analysis|dry); append one JSON line to the
# ledger: name, times, input sha256, result tar sha256, report sha256, observed models, pickup and
# judge exits, harness SUMMARY, unit. It never retries by itself and never edits a prompt or an answer.
set -u
RETRY_OF=""
if [ "${1:-}" = --retry-of ]; then
  RETRY_OF="${2:-}"; shift 2 || { echo "--retry-of needs the previous call's name" >&2; exit 2; }
  [[ "$RETRY_OF" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "--retry-of must name a call [a-z0-9-]" >&2; exit 2; }
  [ $# -ge 7 ] || { echo "a retry needs the call plan (CALL_PLAN_FILE) that holds its retry share" >&2; exit 2; }
fi
LEDGER_DIR="$1"; NAME="$2"; PROMPT_FILE="$3"; C="$4"; KIND="$5"; CREDENTIAL="${6:-}"; CALL_PLAN="${7:-}"
PARENT="${8:-}"
case "$KIND" in token|sentinel) ;; *) echo "KIND must be token or sentinel" >&2; exit 2;; esac
[ -z "$RETRY_OF" ] || [ "$KIND" = token ] || { echo "only a token call is retried" >&2; exit 2; }
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "NAME must be [a-z0-9-]" >&2; exit 2; }
[ -f "$CREDENTIAL" ] && [ ! -L "$CREDENTIAL" ] || { echo "credential file missing" >&2; exit 2; }
[ -f "$PROMPT_FILE" ] && [ ! -L "$PROMPT_FILE" ] || { echo "prompt file missing or a link" >&2; exit 2; }
LIMIT=12; PLAN_SHA=null; CONTINUES=-; CHAIN=-; PARENT_SHA=-; PARENT_CALLS=-; ANCESTORS=-; PARENT_LIMIT_SHA=-; RETRY_TOTAL=0
if [ $# -ge 7 ]; then
  [ -f "$CALL_PLAN" ] && [ ! -L "$CALL_PLAN" ] || { echo "call plan file missing or a link" >&2; exit 2; }
  read -r LIMIT PLAN_SHA CONTINUES CHAIN RETRY_TOTAL < <(PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "$CALL_PLAN" <<'PY'
import hashlib, json, sys
data = open(sys.argv[1], "rb").read()
def whole(v):
    return type(v) is int
def share_of(plan):
    # the #280 retry share: none in a plan from before it; else the four stages, a total within them, per_call >= 1
    if "retry" not in plan:
        return 0
    share = plan["retry"]
    stages = share["stages"]
    ok = isinstance(stages, dict) and set(stages) <= set(("extract", "synthesis", "verify", "final")) \
        and all(whole(v) and v >= 0 for v in stages.values()) and whole(share["total"]) and whole(share["per_call"]) \
        and 0 <= share["total"] <= sum(stages.values()) and share["per_call"] >= (1 if share["total"] else 0)
    return share["total"] if ok else None
try:
    stage = json.loads(data)
    plan = stage["call_plan"]
    total = plan["max_model_calls"]
    if stage.get("stage") == "assess":
        # an assess-inputs plan: one candidate assessment call per discovery, no analysis stage key.
        # A tuple, not a set literal: bash 3.2 brace-expands a comma list in braces inside this process substitution.
        analysis_keys = ("shards", "lenses", "extract", "synthesis", "verify", "final", "continues")
        ok = plan.get("mode") == "assess" and set(plan).isdisjoint(analysis_keys + ("retry",)) and whole(plan["assess"]) \
            and whole(total) and plan["assess"] >= 1 and total == plan["assess"] \
            and whole(stage.get("max_model_calls")) and stage["max_model_calls"] == total
        if ok:
            print(total, hashlib.sha256(data).hexdigest(), "-", "-", 0)
        sys.exit(0)
    parts = [plan[k] for k in ("shards", "lenses", "extract", "synthesis", "verify", "final")]
    planned = plan["shards"] * plan["lenses"]
    continues, chain = "-", "-"
    if "continues" in plan:
        # continue-plan: the extract calls made under the parent ledger are done; the later stages remain, and the
        # extract calls left when the parent stopped during them (#280)
        bound, done = plan["continues"]["extract_stage_sha256"], plan["continues"]["completed"]["extract"]
        ok = whole(plan["extract"]) and whole(done) and done >= 1 and plan["extract"] + done == planned \
            and type(bound) is str and len(bound) == 64 and all(c in "0123456789abcdef" for c in bound)
        continues = bound
        # a chain: the parent ledger was itself a continuation (its continue stage.json is "bound").
        stage_of_parent = plan["continues"].get("parent_stage")
        chain = "extract" if stage_of_parent is None else "continue" if stage_of_parent == "continue" else "?"
        ok = ok and chain != "?"
    else:
        ok = plan["extract"] == planned
    retry = share_of(plan)
    ok = ok and retry is not None and all(whole(v) and v >= 0 for v in parts + [total]) and total >= 1 \
        and plan["final"] == 1 and total == plan["extract"] + plan["synthesis"] + plan["verify"] + plan["final"] + retry \
        and stage.get("max_model_calls", total) == total and whole(stage.get("max_model_calls", total))
except (ValueError, TypeError, KeyError, AttributeError):
    ok = False
if ok:
    print(total, hashlib.sha256(data).hexdigest(), continues, chain, retry)
PY
)
  [ -n "$PLAN_SHA" ] && [ "$PLAN_SHA" != null ] || { echo "call plan malformed: $CALL_PLAN" >&2; exit 2; }
  [ "$LIMIT" -le 40 ] || { echo "call plan of $LIMIT calls is over the absolute cap 40" >&2; exit 2; }
  if [ $# -ge 8 ]; then
    [ "$CONTINUES" != - ] || { echo "a parent ledger needs a continue-plan call plan (call_plan.continues)" >&2; exit 2; }
    read -r PARENT_CALLS PARENT_SHA ANCESTORS PARENT_LIMIT_SHA < <(PYTHONDONTWRITEBYTECODE=1 python3 -I -B - \
        "$PARENT" "$LEDGER_DIR" "$CONTINUES" "$CHAIN" <<'PY'
import hashlib, json, os, sys
parent, child, bound, chain = sys.argv[1:5]
def regular(path):
    return os.path.isfile(path) and not os.path.islink(path)
if not parent or os.path.islink(parent) or not os.path.isdir(parent) \
        or os.path.realpath(parent) == os.path.realpath(child):
    sys.exit("parent ledger directory missing, a link, or the ledger itself: %s" % parent)
ledger, limit = os.path.join(parent, "ledger.jsonl"), os.path.join(parent, "ledger-limit.json")
if not regular(ledger) or not regular(limit):
    sys.exit("parent ledger directory must hold ledger.jsonl and ledger-limit.json: %s" % parent)
limit_bytes = open(limit, "rb").read()
try:
    limit_doc = json.loads(limit_bytes)
    recorded = limit_doc["call_plan_sha256"]
except (ValueError, TypeError, KeyError):
    sys.exit("parent ledger-limit.json unreadable: %s" % limit)
if recorded != bound:
    sys.exit("parent ledger is bound to call plan %s, this plan continues %s" % (recorded, bound))
own = limit_doc.get("parent")
if chain == "continue":
    # the parent was a continuation: its own parent and the ancestors before it count toward the cap too
    def count(v):
        return type(v) is int and v >= 0
    def sha(v):
        return type(v) is str and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
    if not (isinstance(own, dict) and sha(own.get("ledger_sha256")) and count(own.get("calls"))
            and count(own.get("ancestors", 0))):
        sys.exit("parent ledger-limit.json does not record its own parent (calls, ancestors): %s" % limit)
    ancestors = own["calls"] + own.get("ancestors", 0)
    limit_sha = hashlib.sha256(limit_bytes).hexdigest()
else:
    if own is not None:
        sys.exit("parent ledger is itself a continuation; a plan continuing it must say so (parent_stage)")
    ancestors, limit_sha = 0, "-"
claim = os.path.join(parent, "continued-by.json")
if os.path.lexists(claim) and not os.path.lexists(os.path.join(child, "ledger-limit.json")):
    # a straight chain (#258): a parent another ledger already continues is not continued again
    try:
        named = json.loads(open(claim).read()).get("ledger_dir") if regular(claim) else None
    except (ValueError, AttributeError):
        named = None
    if named != os.path.realpath(child):
        sys.exit("parent ledger is already continued by %s: continue the latest ledger of the chain "
                 "(the one no ledger continues yet) instead" % named)
data = open(ledger, "rb").read()
print(sum(b'"kind": "token"' in line for line in data.splitlines()), hashlib.sha256(data).hexdigest(),
      ancestors if chain == "continue" else "-", limit_sha)
PY
)
    [ -n "$PARENT_SHA" ] && [ "$PARENT_SHA" != - ] || { echo "parent ledger refused: $PARENT" >&2; exit 2; }
    if [ "$ANCESTORS" = - ]; then
      [ $((PARENT_CALLS + LIMIT)) -le 40 ] || {
        echo "$PARENT_CALLS parent calls + $LIMIT is over the absolute cap 40" >&2; exit 2; }
    else
      [ $((ANCESTORS + PARENT_CALLS + LIMIT)) -le 40 ] || {
        echo "$ANCESTORS ancestor calls + $PARENT_CALLS parent calls + $LIMIT is over the absolute cap 40" >&2; exit 2; }
    fi
  elif [ "$CONTINUES" != - ]; then
    echo "a continue-plan call plan needs its PARENT_LEDGER_DIR" >&2; exit 2
  fi
fi
# This machine's VM (#290; unset or empty keeps the default, as analysis/machine_settings.py does):
# OSS_RESEARCH_VM_ROOT is the host work root of the Lima VM, OSS_RESEARCH_VM_NAME its instance.
W=${OSS_RESEARCH_VM_ROOT:-/Users/lee-kyu-hwan/.oss-research-vm}
VM=${OSS_RESEARCH_VM_NAME:-oss-research-debian}
HARNESS_DIR=$(cd "$(dirname "$0")" && pwd)
L() { env -i HOME="$W/home" LIMA_HOME="$W/lima" TMPDIR="$W/tmp" PATH="$W/tool/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$W/tool/bin/limactl" "$@"; }
REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
S=/opt/oss-research/src-$C
mkdir -p -m 0700 "$LEDGER_DIR"
LEDGER="$LEDGER_DIR/ledger.jsonl"
BOUND="$LEDGER_DIR/ledger-limit.json"
PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "$BOUND" "$LEDGER" "$LIMIT" "$PLAN_SHA" "$PARENT_SHA" "$PARENT_CALLS" \
    "$ANCESTORS" "$PARENT_LIMIT_SHA" "$PARENT" "$LEDGER_DIR" <<'PY'
import json, os, sys
bound, ledger, limit, sha, parent_sha, parent_calls, ancestors, parent_limit_sha, parent, child = sys.argv[1:11]
now = {"limit": int(limit), "call_plan_sha256": None if sha == "null" else sha}
claim = mine = None
if parent_sha != "-":
    now["parent"] = {"ledger_sha256": parent_sha, "calls": int(parent_calls)}
    if ancestors != "-":
        now["parent"].update(ancestors=int(ancestors), limit_sha256=parent_limit_sha)
    # the parent's one child (#258): beside its ledger, not in the ledger sha
    claim = os.path.join(parent, "continued-by.json")
    mine = {"ledger_dir": os.path.realpath(child), "call_plan_sha256": sha}
def claimed():
    try:
        return json.loads(open(claim).read()) if os.path.isfile(claim) and not os.path.islink(claim) else None
    except ValueError:
        return None
if os.path.lexists(bound):
    try:
        recorded = json.loads(open(bound).read()) if not os.path.islink(bound) else None
    except ValueError:
        recorded = None
    if recorded != now:
        sys.exit("refused: the ledger limit is bound to %s, this call asks for %s" % (json.dumps(recorded), json.dumps(now)))
    if claim and claimed() != mine:
        sys.exit("refused: the parent ledger's continued-by.json no longer names this ledger: %s" % claim)
elif os.path.exists(ledger) and os.path.getsize(ledger) and now != {"limit": 12, "call_plan_sha256": None}:
    sys.exit("refused: the ledger already holds calls without a recorded ledger limit")
else:
    if claim:
        try:
            fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.write(fd, (json.dumps(mine) + "\n").encode())
            os.close(fd)
        except FileExistsError:
            other = claimed()
            if other != mine:
                print("parent ledger is already continued by %s: continue the latest ledger of the chain "
                      "(the one no ledger continues yet) instead"
                      % (other.get("ledger_dir") if isinstance(other, dict) else None), file=sys.stderr)
                sys.exit(2)
    fd = os.open(bound, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, (json.dumps(now) + "\n").encode())
    os.close(fd)
PY
rc=$?; [ "$rc" = 0 ] || exit $(( rc == 2 ? 2 : 3 ))
count=$(grep -c '"kind": "token"' "$LEDGER" 2>/dev/null); count=${count:-0}
if [ "$KIND" = token ] && [ "$count" -ge "$LIMIT" ]; then
  echo "refused: the ledger already holds $LIMIT model calls" >&2; exit 3
fi
RETRY=""
if [ -n "$RETRY_OF" ]; then
  RETRY=$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B "$HARNESS_DIR/../retry_policy.py" admit "$LEDGER_DIR" "$RETRY_OF" \
    "$NAME" "$PROMPT_FILE" "$CALL_PLAN")
  rc=$?; [ "$rc" = 0 ] || exit $(( rc == 2 ? 2 : 3 ))
elif [ "$KIND" = token ] && [ "$RETRY_TOTAL" -gt 0 ]; then
  planned=$(grep '"kind": "token"' "$LEDGER" 2>/dev/null | grep -vc '"attempt": ')
  if [ "${planned:-0}" -ge $((LIMIT - RETRY_TOTAL)) ]; then
    echo "refused: the ledger already holds $((LIMIT - RETRY_TOTAL)) planned model calls; the other $RETRY_TOTAL are" \
      "the retry share, for a retry (--retry-of) only" >&2; exit 3
  fi
fi
D="$LEDGER_DIR/$NAME"; mkdir -m 0700 "$D" || { echo "call directory exists: $D" >&2; exit 2; }
input_sha=$(shasum -a 256 "$PROMPT_FILE" | cut -c1-64)
STAGE=/var/lib/oss-research-staging/$NAME
L shell --workdir / "$VM" -- sudo bash -c "install -d -m 0700 /var/lib/oss-research-staging && \
  install -d -m 0700 $STAGE && umask 077 && cat > $STAGE/task.txt && chmod 0600 $STAGE/task.txt && \
  sha256sum $STAGE/task.txt | cut -c1-64" < "$PROMPT_FILE" > "$D/staged.sha256"
[ "$(cat "$D/staged.sha256")" = "$input_sha" ] || { echo "staged prompt differs" >&2; exit 4; }
MODE=analysis; EGRESS=pin; [ "$KIND" = sentinel ] && { MODE=analysis-dry; EGRESS=none; }
began=$(date -u +%FT%TZ)
L shell --workdir / "$VM" -- bash "$S/share/linux/model_once.sh" \
  "$KIND" "$S" /opt/oss-research/srt-0.0.77 /opt/oss-research/node-v24.20.0-linux-arm64/bin "$EGRESS" normal 330 300 \
  "$MODE" "$STAGE/task.txt" < "$CREDENTIAL" > "$D/harness.log" 2>&1
harness_rc=$?
ended=$(date -u +%FT%TZ)
L shell --workdir / "$VM" -- sudo rm -rf "$STAGE"
grep -qF -f "$CREDENTIAL" "$D/harness.log"; leak=$?
if [ "$leak" != 1 ] || [ ! -s "$D/harness.log" ]; then
  echo "log not proven clean (grep exit $leak): not shown, not picked up" >&2
  retried=""; [ -z "$RETRY" ] || retried=", ${RETRY:1:${#RETRY}-2}"
  printf '{"name": "%s", "kind": "%s", "started": "%s", "ended": "%s", "input_sha256": "%s", "log_clean": false%s}\n' \
    "$NAME" "$KIND" "$began" "$ended" "$input_sha" "$retried" >> "$LEDGER"
  exit 5
fi
unit=$(sed -n 's/^INFO R wrapper {"unit": "\(oss-research-[0-9a-f]*\)".*/\1/p' "$D/harness.log" | head -1)
summary=$(grep '^SUMMARY' "$D/harness.log" | tail -1)
tar_sha=""; pickup_rc=""; judge_rc=""
if [ -n "$unit" ]; then
  for f in record.json task.txt result.tar; do
    L shell --workdir / "$VM" -- sudo cat "/var/lib/oss-research-evidence/$unit/$f" > "$D/$f" 2>/dev/null || rm -f "$D/$f"
  done
  for f in "$D"/record.json "$D"/task.txt "$D"/result.tar; do
    [ -f "$f" ] || continue
    if grep -qF -f "$CREDENTIAL" "$f"; then echo "credential value found in $(basename "$f"): stop" >&2; exit 5; fi
  done
  JKIND=analysis; [ "$KIND" = sentinel ] && JKIND=dry
  if [ -f "$D/result.tar" ]; then
    tar_sha=$(shasum -a 256 "$D/result.tar" | cut -c1-64)
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B "$REPO/share/linux/verify_pickup.py" \
      "$D/result.tar" 67108864 "$D/result" > "$D/pickup.json"; pickup_rc=$?
    RESULT="$D/result"
  else
    RESULT="$D/no-result"
  fi
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B "$REPO/share/linux/model_once_verdict.py" \
    judge-worker "$D/record.json" "$RESULT" "$D/task.txt" "$JKIND" > "$D/judge.txt"; judge_rc=$?
fi
PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "$D" "$NAME" "$KIND" "$began" "$ended" "$input_sha" "$tar_sha" \
  "${pickup_rc:-null}" "${judge_rc:-null}" "$harness_rc" "$summary" "$unit" "$RETRY" >> "$LEDGER" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, name, kind, began, ended, input_sha, tar_sha, pickup, judge, harness, summary, unit, retry = sys.argv[1:14]
manifest = {}
try:
    manifest = json.loads((Path(d) / "result/manifest.json").read_text())
except (OSError, ValueError):
    pass
report = Path(d) / "result/report.md"
# a retry's attempt, first call, previous call, stage, failure sha and failed items (retry_policy.py admit, #280)
print(json.dumps(dict({
    "name": name, "kind": kind, "started": began, "ended": ended, "unit": unit or None,
    "input_sha256": input_sha, "prompt_sha256_recorded": manifest.get("prompt_sha256"),
    "result_tar_sha256": tar_sha or None,
    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest() if report.is_file() else None,
    "status": manifest.get("status"), "observed_models": manifest.get("observed_models"),
    "permission_mode": manifest.get("permission_mode"), "pickup_exit": None if pickup == "null" else int(pickup),
    "judge_exit": None if judge == "null" else int(judge), "harness_exit": int(harness),
    "harness_summary": summary, "log_clean": True}, **(json.loads(retry) if retry else {}))))
PY
chmod -R go-rwx "$D" "$LEDGER" "$BOUND"
tail -1 "$LEDGER"
