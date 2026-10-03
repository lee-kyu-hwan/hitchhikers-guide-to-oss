#!/bin/bash
# #78 analysis: one isolated model call from the host (macOS), with a ledger.
#
#   bash analysis_call.sh LEDGER_DIR NAME PROMPT_FILE COMMIT KIND [CREDENTIAL_FILE]
#
# KIND token   : a real call. CREDENTIAL_FILE is the approved dedicated oauth-token; it reaches the
#                guest only as non-PTY stdin of model_once.sh (run tmpfs + reaper), never argv or log.
# KIND sentinel: the dry path (analysis-dry: claude off PATH, no child can start); CREDENTIAL_FILE
#                is a fake sentinel.
#
# Steps: refuse when LEDGER_DIR/ledger.jsonl already holds 12 calls (started calls count, failed
# ones too); stage PROMPT_FILE root-only in the guest (/var/lib/oss-research-staging/<NAME>/task.txt,
# root 0700/0600); run model_once.sh (egress pin for token, none for sentinel); show the log only
# after grep -F finds no credential value (exit 1); pick up the result tar, check it with
# verify_pickup.py (default contract) and judge-worker (analysis|dry); append one JSON line to the
# ledger: name, times, input sha256, result tar sha256, report sha256, observed models, pickup and
# judge exits, harness SUMMARY, unit. It never retries and never edits a prompt or an answer.
set -u
LEDGER_DIR="$1"; NAME="$2"; PROMPT_FILE="$3"; C="$4"; KIND="$5"; CREDENTIAL="${6:-}"
case "$KIND" in token|sentinel) ;; *) echo "KIND must be token or sentinel" >&2; exit 2;; esac
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || { echo "NAME must be [a-z0-9-]" >&2; exit 2; }
[ -f "$CREDENTIAL" ] && [ ! -L "$CREDENTIAL" ] || { echo "credential file missing" >&2; exit 2; }
[ -f "$PROMPT_FILE" ] && [ ! -L "$PROMPT_FILE" ] || { echo "prompt file missing or a link" >&2; exit 2; }
W=/Users/lee-kyu-hwan/.oss-research-vm
L() { env -i HOME="$W/home" LIMA_HOME="$W/lima" TMPDIR="$W/tmp" PATH="$W/tool/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$W/tool/bin/limactl" "$@"; }
REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
S=/opt/oss-research/src-$C
mkdir -p -m 0700 "$LEDGER_DIR"
LEDGER="$LEDGER_DIR/ledger.jsonl"
if [ "$KIND" = token ] && [ "$(grep -c '"kind": "token"' "$LEDGER" 2>/dev/null || echo 0)" -ge 12 ]; then
  echo "refused: the ledger already holds 12 model calls" >&2; exit 3
fi
D="$LEDGER_DIR/$NAME"; mkdir -m 0700 "$D" || { echo "call directory exists: $D" >&2; exit 2; }
input_sha=$(shasum -a 256 "$PROMPT_FILE" | cut -c1-64)
STAGE=/var/lib/oss-research-staging/$NAME
L shell --workdir / oss-research-debian -- sudo bash -c "install -d -m 0700 /var/lib/oss-research-staging && \
  install -d -m 0700 $STAGE && umask 077 && cat > $STAGE/task.txt && chmod 0600 $STAGE/task.txt && \
  sha256sum $STAGE/task.txt | cut -c1-64" < "$PROMPT_FILE" > "$D/staged.sha256"
[ "$(cat "$D/staged.sha256")" = "$input_sha" ] || { echo "staged prompt differs" >&2; exit 4; }
MODE=analysis; EGRESS=pin; [ "$KIND" = sentinel ] && { MODE=analysis-dry; EGRESS=none; }
began=$(date -u +%FT%TZ)
L shell --workdir / oss-research-debian -- bash "$S/tests/fixtures/oss_research_worker/linux/model_once.sh" \
  "$KIND" "$S" /opt/oss-research/srt-0.0.77 /opt/oss-research/node-v24.20.0-linux-arm64/bin "$EGRESS" normal 330 300 \
  "$MODE" "$STAGE/task.txt" < "$CREDENTIAL" > "$D/harness.log" 2>&1
harness_rc=$?
ended=$(date -u +%FT%TZ)
L shell --workdir / oss-research-debian -- sudo rm -rf "$STAGE"
grep -qF -f "$CREDENTIAL" "$D/harness.log"; leak=$?
if [ "$leak" != 1 ] || [ ! -s "$D/harness.log" ]; then
  echo "log not proven clean (grep exit $leak): not shown, not picked up" >&2
  printf '{"name": "%s", "kind": "%s", "started": "%s", "ended": "%s", "input_sha256": "%s", "log_clean": false}\n' \
    "$NAME" "$KIND" "$began" "$ended" "$input_sha" >> "$LEDGER"
  exit 5
fi
unit=$(sed -n 's/^INFO R wrapper {"unit": "\(oss-research-[0-9a-f]*\)".*/\1/p' "$D/harness.log" | head -1)
summary=$(grep '^SUMMARY' "$D/harness.log" | tail -1)
tar_sha=""; pickup_rc=""; judge_rc=""
if [ -n "$unit" ]; then
  for f in record.json task.txt result.tar; do
    L shell --workdir / oss-research-debian -- sudo cat "/var/lib/oss-research-evidence/$unit/$f" > "$D/$f" 2>/dev/null || rm -f "$D/$f"
  done
  for f in "$D"/record.json "$D"/task.txt "$D"/result.tar; do
    [ -f "$f" ] || continue
    if grep -qF -f "$CREDENTIAL" "$f"; then echo "credential value found in $(basename "$f"): stop" >&2; exit 5; fi
  done
  JKIND=analysis; [ "$KIND" = sentinel ] && JKIND=dry
  if [ -f "$D/result.tar" ]; then
    tar_sha=$(shasum -a 256 "$D/result.tar" | cut -c1-64)
    PYTHONDONTWRITEBYTECODE=1 python3 -I -B "$REPO/tests/fixtures/oss_research_worker/linux/verify_pickup.py" \
      "$D/result.tar" 67108864 "$D/result" > "$D/pickup.json"; pickup_rc=$?
    RESULT="$D/result"
  else
    RESULT="$D/no-result"
  fi
  PYTHONDONTWRITEBYTECODE=1 python3 -I -B "$REPO/tests/fixtures/oss_research_worker/linux/model_once_verdict.py" \
    judge-worker "$D/record.json" "$RESULT" "$D/task.txt" "$JKIND" > "$D/judge.txt"; judge_rc=$?
fi
PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "$D" "$NAME" "$KIND" "$began" "$ended" "$input_sha" "$tar_sha" \
  "${pickup_rc:-null}" "${judge_rc:-null}" "$harness_rc" "$summary" "$unit" >> "$LEDGER" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, name, kind, began, ended, input_sha, tar_sha, pickup, judge, harness, summary, unit = sys.argv[1:13]
manifest = {}
try:
    manifest = json.loads((Path(d) / "result/manifest.json").read_text())
except (OSError, ValueError):
    pass
report = Path(d) / "result/report.md"
print(json.dumps({
    "name": name, "kind": kind, "started": began, "ended": ended, "unit": unit or None,
    "input_sha256": input_sha, "prompt_sha256_recorded": manifest.get("prompt_sha256"),
    "result_tar_sha256": tar_sha or None,
    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest() if report.is_file() else None,
    "status": manifest.get("status"), "observed_models": manifest.get("observed_models"),
    "permission_mode": manifest.get("permission_mode"), "pickup_exit": None if pickup == "null" else int(pickup),
    "judge_exit": None if judge == "null" else int(judge), "harness_exit": int(harness),
    "harness_summary": summary, "log_clean": True}))
PY
chmod -R go-rwx "$D" "$LEDGER"
tail -1 "$LEDGER"
