#!/bin/bash
# 읽기 전용 GitHub 토큰을 relay에만 주입하는 경로의 게스트 실측(관리 계정 vmadmin이 실행, 안에서 sudo).
#
#   limactl shell --workdir / <vm> -- bash /opt/oss-research/src-<COMMIT>/tests/fixtures/oss_research_worker/linux/github_token_cases.sh COMMIT \
#       < <한 줄짜리 가짜 토큰(sentinel) 파일, 호스트의 임시 디렉터리>
#
# 모두 --egress none, loopback mock upstream, sentinel(가짜 값)만. 실제 토큰·api.github.com·모델은 없다.
# sentinel 은 stdin 으로만 받는다(argv 에 나오지 않는다). 모델 토큰과 같은 절차로 사례마다 run 전용 tmpfs
# (/run/oss-research-credentials/<RID>/github-readonly/token: 디렉터리 ossresearch 0700, 파일 0600, O_EXCL)에 설치하고,
# 설치 전에 이 run 경로만 지우는 reaper(systemd timer)를 걸어 활성을 확인하며, 끝나면 지운다.
#   T4~T7  (#192 후속) collect docs-only·빈 검색·응답 quota 헤더, collect-issues closed: 같은 sentinel 계약(요청 전부가 Bearer, 값 0회)
#   T1~T3  collect(normal)·collect-issues(complete; T2n 은 익명 401 mock의 normal=partial)·candidate-reads(normal): mock upstream이 받은 요청 전부가 Bearer <sentinel>
#          (mock 쪽에서만 대조), 호스트 회수용 tar, 산출물·record·로그에 sentinel 0회
#   A      인자 없는 collect normal: 익명 그대로(Authorization 0)
#   P      token-probe: srt child는 파일을 읽지 못한다(경로를 알려 줘도), relay는 쓴다
#   R      wrapper SIGKILL(hang 중): unit·프로세스 0, 이후 정리
#   K      설치 직후 harness SIGKILL: reaper가 사본을 지운다
#   B      entry 인자 거부(0644 파일, 출력 디렉터리 안의 경로): relay 전 exit 2
# 사례마다 끝에 tmpfs 사본·reaper·unit·프로세스가 0임을 본다.
set -u
if [ "${1:-}" = kill-after-install ]; then MODE_KILL=yes; shift; else MODE_KILL=no; fi
C="${1:?commit}"; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
PY=/usr/bin/python3
WRAP="$PY -I -B $S/bin/oss-research-linux-run"
ENV=(env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1)
CTOP=/run/oss-research-credentials
GT=/run/osr-gt
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
info() { echo "INFO $*"; }
[ -t 0 ] && { echo "FAIL stdin is a tty"; exit 1; }
IFS= read -r SENT
case "$SENT" in *SENTINEL*) ;; *) echo "FAIL the fixture token must hold SENTINEL"; exit 1;; esac
state() { printf 'units=%s procs=%s creds=%s reapers=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)" \
  "$(sudo find $CTOP -mindepth 1 2>/dev/null | grep -c .)" \
  "$(systemctl list-units --all 'osr-cred-reaper-*' --no-legend | grep -c .)"; }

install_token() {  # $1 reaper seconds; sets RID CDIR PDIR TOKEN REAPER
  RID="$(tr -d '-' < /proc/sys/kernel/random/uuid | cut -c1-16)"
  CDIR="$CTOP/$RID"; PDIR="$CDIR/github-readonly"; TOKEN="$PDIR/token"; REAPER="osr-cred-reaper-$RID"
  sudo systemd-run --quiet --collect --unit="$REAPER" --on-active="${1}s" \
    "$PY" -I -B -c 'import os,sys
for p in sys.argv[1:2]:
    try: os.unlink(p)
    except FileNotFoundError: pass
for p in sys.argv[2:]:
    try: os.rmdir(p)
    except OSError: pass' "$TOKEN" "$PDIR" "$CDIR" || { echo "FAIL reaper-armed"; return 1; }
  [ "$(systemctl is-active "$REAPER.timer")" = active ] || { echo "FAIL reaper-not-active (no token written)"; return 1; }
  printf '%s\n' "$SENT" | sudo "$PY" -I -B -c '
import os, pwd, stat, sys
ctop, cdir, pdir, cred = sys.argv[1:5]
user = pwd.getpwnam("ossresearch")
try:
    os.mkdir(ctop, 0o711)
except FileExistsError:
    pass
for path in ("/", "/run", ctop):
    info = os.lstat(path)
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, path
assert stat.S_IMODE(os.lstat(ctop).st_mode) == 0o711 and not os.listdir(ctop), "top not private or not empty"
data = sys.stdin.buffer.read(514)
if not 20 <= len(data) <= 513:
    sys.exit("token size out of bounds")
os.mkdir(cdir, 0o711); os.chown(cdir, 0, 0); os.chmod(cdir, 0o711)
os.mkdir(pdir, 0o700); os.chown(pdir, user.pw_uid, user.pw_gid); os.chmod(pdir, 0o700)
fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    os.write(fd, data); os.fchown(fd, user.pw_uid, user.pw_gid); os.fchmod(fd, 0o600)
finally:
    os.close(fd)
' "$CTOP" "$CDIR" "$PDIR" "$TOKEN" || { echo "FAIL token-installed"; return 1; }
  meta="$(sudo stat -c '%F %U %a %h' "$TOKEN")"; pmeta="$(sudo stat -c '%U %a' "$PDIR")"
  case "$meta" in "regular file ossresearch 600 1") ;; *) echo "FAIL token-meta [$meta]"; return 1;; esac
  [ "$pmeta" = "ossresearch 700" ] || { echo "FAIL token-dir-meta [$pmeta]"; return 1; }
  info "installed run $RID token[$meta] dir[$pmeta] reaper $REAPER (${1}s)"
}
remove_token() {
  sudo "$PY" -I -B -c '
import os, sys
for p in sys.argv[1:2]:
    try: os.unlink(p)
    except FileNotFoundError: pass
for p in sys.argv[2:]:
    try: os.rmdir(p)
    except FileNotFoundError: pass' "$TOKEN" "$PDIR" "$CDIR"
  sudo systemctl stop "$REAPER.timer" "$REAPER.service" > /dev/null 2>&1
}

if [ "$MODE_KILL" = yes ]; then
  install_token 40 || exit 1
  echo "RID=$RID"
  echo "INFO harness SIGKILLs itself; the reaper must remove the token"
  kill -KILL $$
fi

sudo install -d -m 0700 $GT
printf '%s\n' "$SENT" | sudo sh -c "umask 077; cat > $GT/pattern"
cleanup() { sudo rm -rf $GT /tmp/osr-gt-*; }
trap cleanup EXIT
# positive control: grep -a must find the sentinel inside a tar, or "0 hits" would mean nothing.
sudo install -d -m 0700 $GT/control
printf 'x %s y\n' "$SENT" | sudo tee $GT/control/m > /dev/null
sudo tar -C $GT/control -cf $GT/control.tar m
if sudo grep -raqF -f $GT/pattern $GT/control.tar; then pass "control: grep -a finds the sentinel inside a tar"; else fail "control: grep does not find the sentinel inside a tar"; fi

stage_pack() {  # $1 stage run dir, $2 case dir, $3 kind: collect|issues|reads-discover
  case $3 in
    collect) sudo install -d -m 0700 $2/pack
      for f in corpus.json collection-manifest.json inventory.md transport.json; do sudo cp -P $1/output/$f $2/pack/; done
      for f in relay.log collection-envelope.json; do sudo cp -P $1/result/$f $2/pack/; done
      sudo cp -P $1/result/manifest.json $2/pack/run-manifest.json
      sudo tar -C $2/pack -cf $2/pack.tar corpus.json collection-manifest.json inventory.md transport.json relay.log run-manifest.json collection-envelope.json ;;
    issues) sudo install -d -m 0700 $2/pack
      for f in corpus.json collection-manifest.json transport.json; do sudo cp -P $1/output/$f $2/pack/; done
      for f in relay.log collection-envelope.json; do sudo cp -P $1/result/$f $2/pack/; done
      sudo cp -P $1/result/manifest.json $2/pack/run-manifest.json
      sudo tar -C $2/pack -cf $2/pack.tar corpus.json collection-manifest.json transport.json relay.log run-manifest.json collection-envelope.json ;;
    reads-discover) sudo install -d -m 0700 $2/pack
      for f in discovery.json verification-manifest.json transport.json; do sudo cp -P $1/output/$f $2/pack/; done
      for f in relay.log read-envelope.json; do sudo cp -P $1/result/$f $2/pack/; done
      sudo cp -P $1/result/manifest.json $2/pack/run-manifest.json
      sudo tar -C $2/pack -cf $2/pack.tar discovery.json verification-manifest.json transport.json relay.log run-manifest.json read-envelope.json ;;
  esac
}
summary() {  # $1 record file
  sudo "$PY" -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1])
try:
    i=json.loads(r["stdout"].strip().splitlines()[-1])
except Exception:
    i={}
keys=("github_token","upstream_checked","upstream_with_bearer","upstream_without_the_bearer","upstream_with_any_authorization","upstream_authorization","upstream_proxy_authorization","upstream_requests","status","succeeded","parent_can_read","corpus_records","docs_filter","transport_rate_limits","transport_requests","transport_credential_scope")
print("   record", r["unit"], r["entry"], "exit", r["exit_code"], r["failures"], "clock", (r.get("clock") or {}).get("action"))
print("   inner", json.dumps({k: i.get(k) for k in keys if k in i}))
if "probe" in i: print("   probe", json.dumps(i["probe"]))
print("   stderr_tail", repr(i.get("stderr_tail", r.get("stderr", ""))[-200:]))' "$1" 2>&1 | head -6
}
no_sentinel() {  # $1 label, rest: paths
  local label=$1; shift
  sudo grep -raqF -f $GT/pattern "$@" > /dev/null 2>&1; rc=$?
  if [ $rc = 1 ]; then pass "$label: the sentinel is in none of $*"; else fail "$label: grep rc=$rc (0 = the sentinel is there)"; fi
}
settled() {  # $1 label: nothing left of the run
  local s; s="$(state)"
  case "$s" in "units=0 procs=0 creds=0 reapers=0") pass "$1: $s";; *) fail "$1: $s";; esac
}

E=/var/lib/oss-research-evidence/github-token-$C
sudo install -d -m 0700 $E
echo "== before $(state)"
for CASE in T1:collect-case:collect:collect:normal T2:collect-issues-case:issues:collect-issues:complete T2n:collect-issues-case:issues:collect-issues:normal T3:candidate-reads-case:reads-discover:candidate-reads:normal A:collect-case:collect:anonymous:normal T4:collect-case:collect:collect-docs:docs-only-title T5:collect-case:collect:collect-empty:empty T6:collect-case:collect:collect-quota:quota T7:collect-issues-case:issues:collect-issues-closed:closed; do
  IFS=: read -r NAME MODE KIND LABEL CASENAME <<< "$CASE"
  echo "== $NAME $LABEL $CASENAME ($MODE)"
  OUT=$(sudo mktemp -d /tmp/osr-gt-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  ARGS=()
  if [ "$NAME" != A ]; then install_token 300 || { fail "$NAME install"; continue; }; ARGS=($TOKEN); fi
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 200 -- $MODE $OUT $CASENAME "${ARGS[@]}" > /tmp/osr-gt-rec.$NAME 2> /tmp/osr-gt-err.$NAME
  echo "   wrapper rc=$?"
  summary /tmp/osr-gt-rec.$NAME
  D=$E/$NAME; sudo install -d -m 0700 $D; sudo cp /tmp/osr-gt-rec.$NAME $D/record.json
  case $KIND in
    reads-discover) stage_pack $OUT/discover/run $D reads-discover ;;
    *) stage_pack $OUT/run $D $KIND ;;
  esac
  sudo tar -C $OUT -cf $D/artifacts.tar . 2>/dev/null
  no_sentinel "$NAME" $OUT /tmp/osr-gt-rec.$NAME /tmp/osr-gt-err.$NAME $D
  if [ "$NAME" != A ]; then remove_token; fi
  settled "$NAME after"
  sudo rm -rf $OUT
done

echo "== P token-probe: what an srt child can read of the token file"
OUT=$(sudo mktemp -d /tmp/osr-gt-out-XXXX); sudo chown ossresearch:ossresearch $OUT
install_token 300 && {
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 200 -- token-probe $OUT $TOKEN > /tmp/osr-gt-rec.P 2> /tmp/osr-gt-err.P
  echo "   wrapper rc=$?"
  summary /tmp/osr-gt-rec.P
  D=$E/P; sudo install -d -m 0700 $D; sudo cp /tmp/osr-gt-rec.P $D/record.json
  no_sentinel P $OUT /tmp/osr-gt-rec.P /tmp/osr-gt-err.P $D
  remove_token
}
settled "P after"
sudo rm -rf $OUT

echo "== R wrapper SIGKILL during a hanging read with the token installed"
OUT=$(sudo mktemp -d /tmp/osr-gt-out-XXXX); sudo chown ossresearch:ossresearch $OUT
install_token 300 && {
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 300 -- collect-case $OUT upstream-hang $TOKEN > /tmp/osr-gt-rec.R 2>&1 &
  sleep 12
  echo "   before kill: $(state)"
  sudo pkill -KILL -f "oss-research-linux-run --entry verify"; sleep 5
  echo "   after SIGKILL+5s: $(state | sed 's/creds=[0-9]*/creds=held-by-harness/')"
  if sudo test -f "$TOKEN"; then pass "R: the token copy is still the harness's own to remove (control)"; else fail "R: token copy vanished"; fi
  no_sentinel R $OUT /tmp/osr-gt-rec.R
  remove_token
}
settled "R after"
sudo rm -rf $OUT

echo "== K harness SIGKILL right after the install: the reaper removes the copy"
KOUT="$(printf '%s\n' "$SENT" | bash "$0" kill-after-install "$C" 2>/dev/null)"
KRID="$(printf '%s\n' "$KOUT" | sed -n 's/^RID=//p')"
echo "   child output: $(printf '%s' "$KOUT" | tr '\n' ' ')"
if [ -n "$KRID" ] && sudo test -f "$CTOP/$KRID/github-readonly/token"; then pass "K: the copy is there after the harness died (control)"; else fail "K: no copy after the harness died"; fi
started=$(date +%s); gone=no
while [ $(( $(date +%s) - started )) -lt 100 ]; do
  if ! sudo test -e "$CTOP/$KRID"; then gone=yes; break; fi
  sleep 2
done
echo "   copy gone=$gone after $(( $(date +%s) - started )) s (reaper armed for 40 s)"
[ "$gone" = yes ] && pass "K: the reaper removed the copy" || fail "K: the copy is still there"
sleep 3
settled "K after"

echo "== B entry refuses a bad token file before any relay"
OUT=$(sudo mktemp -d /tmp/osr-gt-out-XXXX); sudo chown ossresearch:ossresearch $OUT
INTERVAL="--start-at 2026-09-01T00:00:00Z --end-at 2026-09-08T00:00:00Z"
refuse() {  # $1 label $2 token path
  sudo "${ENV[@]}" $WRAP --entry collect --egress none --timeout 60 -- --out-dir $OUT --repo octo/demo $INTERVAL --github-token-file "$2" > /tmp/osr-gt-rec.B 2>&1
  echo "   [$1] wrapper rc=$?"
  sudo "$PY" -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("   record", r["entry"], "exit", r["exit_code"], r["failures"], repr(" ".join(r.get("stderr","").split())[-150:]))' /tmp/osr-gt-rec.B
  echo "   out entries: $(sudo ls -A $OUT | wc -l) $(state)"
  no_sentinel "B $1" /tmp/osr-gt-rec.B
}
install_token 300 && {
  sudo chmod 0644 "$TOKEN"; refuse "mode 0644" "$TOKEN"
  sudo chmod 0600 "$TOKEN"; refuse "absent path under the output directory" "$OUT/token"
  refuse "relative path" "token"
  remove_token
}
settled "B after"
sudo rm -rf $OUT
sudo chmod -R go-rwx $E
echo "== final $(state) fails=$fails outs=$(ls -d /tmp/osr-gt-out-* 2>/dev/null | wc -l)"
[ "$fails" = 0 ] && echo "SUMMARY fails=0" || echo "SUMMARY fails=$fails"
