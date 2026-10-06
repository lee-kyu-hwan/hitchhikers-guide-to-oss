#!/bin/bash
# #78 actual E2E: turn an isolated collection (corpus.json + manifest.json) into the
# model run's input directory, on the host, mechanically. No network, no model, no credential.
#
#   prepare_input.sh <collected corpus.json> <collected manifest.json> <request.json> <new workflow run dir> <new input dir>
#
# request.json = request.template.json with the interval filled in (the collector's own interval).
# Output: <input dir>/task.txt = the analyzer's own blind-lens extraction prompt built from the
# collected corpus (no hand-written prompt, no path or credential in it). The worker gets
# --input-dir <input dir> --prompt-file <input dir>/task.txt.
set -euo pipefail
CORPUS="$1"; MANIFEST="$2"; REQUEST="$3"; RUN="$4"; INPUT="$5"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
PY=/opt/homebrew/bin/python3
WF="$REPO/dot_agents/skills/researching-open-source-contributions/scripts/oss_workflow.py"
[ ! -e "$RUN" ] && [ ! -e "$INPUT" ] || { echo "run and input dirs must not exist" >&2; exit 2; }
"$PY" -B "$WF" init --request "$REQUEST" --run-dir "$RUN"
"$PY" -B "$WF" adopt-collection --run-dir "$RUN" --corpus "$CORPUS" --manifest "$MANIFEST"
"$PY" -B "$WF" digest --run-dir "$RUN"
PROMPT="$RUN/stages/digest/gen-001/extract/prompts/extract-blind.md"
mkdir -m 0700 "$INPUT"
install -m 0444 "$PROMPT" "$INPUT/task.txt"
size=$(wc -c < "$INPUT/task.txt")
[ "$size" -le 65536 ] || { echo "prompt is $size bytes (> 64 KiB): lower max_per_repository" >&2; exit 2; }
if grep -q -E 'oss-research-credentials|oauth-token|CLAUDE_CODE_OAUTH_TOKEN' "$INPUT/task.txt"; then
  echo "credential marker in prompt" >&2; exit 2
fi
echo "input ready: $INPUT/task.txt ($size bytes, sha256 $(shasum -a 256 "$INPUT/task.txt" | cut -c1-64))"
