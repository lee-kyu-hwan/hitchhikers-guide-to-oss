#!/bin/bash
# 모델 gate 사례(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
# 비AI: sentinel 자격(가짜 값)만 쓰고 claude를 PATH에서 뺀다. 그래서 어떤 사례도 모델을 launch할 수 없다.
# 모두 --egress none. 기대 결과는 G4(permission mode auto)만 입력 거부(exit 2, #4 항목 1: auto는 CLI에서
# 받지 않는다)이고 나머지는 전부 Blocked(exit 3)이다. 어느 사례도 run 디렉터리를 만들지 않아야 한다.
# 이전 판(a189g-host/gate_cases.sh·gate_forged.sh)에 relay·staged·다른 model 사례를 더했다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
T=$(sudo mktemp -d /tmp/osr-g-XXXX); sudo chown ossresearch:ossresearch $T
sudo -u ossresearch bash -c "umask 077; mkdir -p $T/cred/claude-profile2 $T/input && chmod 700 $T/cred/claude-profile2 && printf 'sentinel-gate-token-%040d\n' 0 > $T/cred/claude-profile2/oauth-token && chmod 600 $T/cred/claude-profile2/oauth-token && echo summarize > $T/input/task.txt && chmod 755 $T $T/input && chmod 644 $T/input/task.txt"
BASE="--profile claude-profile2 --input-dir $T/input --prompt-file $T/input/task.txt --credential-file $T/cred/claude-profile2/oauth-token --runtime-prefix /opt/oss-research/srt-0.0.77"
ARGS="$BASE --model claude-opus-5-5 --effort medium"
WRAP=(sudo env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1 python3 -I -B $S/bin/oss-research-linux-run)
state() { printf 'units=%s procs=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)"; }
show() {  # $1 label, $2 run dir
  python3 -c 'import json,sys; r=json.loads(sys.stdin.read().strip().splitlines()[-1]); print("record", r.get("entry"), "exit", r["exit_code"], r["failures"], "|", " ".join(r["stderr"].split())[:300])'
  echo "run_dir_exists=$(test -e $2 && echo yes || echo no) $(state)"
}
echo "== before $(state)"
echo "== G1 unit-check inside the wrapper unit"
O=$(sudo mktemp -d /tmp/osr-g-out-XXXX); sudo chown ossresearch:ossresearch $O
"${WRAP[@]}" --entry verify --egress none --timeout 30 -- unit-check $O | python3 -c 'import json,sys; r=json.loads(sys.stdin.read().strip().splitlines()[-1]); print(r["unit"], r["exit_code"], r["stdout"].strip())'
sudo rm -rf $O
echo "== G2 direct run outside the wrapper (range otherwise matches, no marker)"
sudo -u ossresearch env -i HOME=/home/ossresearch PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 python3 -I -B $S/bin/oss-research-worker $ARGS --run-dir $T/run-g2 < /dev/null; echo "rc=$? run_dir_exists=$(test -e $T/run-g2 && echo yes || echo no)"
echo "== G3 forged marker, stdin pipe closes at once (parent EOF guard)"
echo | sudo -u ossresearch env -i HOME=/home/ossresearch PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_UNIT=oss-research-0123456789abcdef python3 -I -B $S/bin/oss-research-worker $ARGS --run-dir $T/run-g3; echo "rc=$? run_dir_exists=$(test -e $T/run-g3 && echo yes || echo no)"
echo "== G3b forged marker, an open pipe on stdin, outside any unit"
sleep 20 | sudo -u ossresearch env -i HOME=/home/ossresearch PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_UNIT=oss-research-0123456789abcdef python3 -I -B $S/bin/oss-research-worker $ARGS --run-dir $T/run-g3b; echo "rc=${PIPESTATUS[1]} run_dir_exists=$(test -e $T/run-g3b && echo yes || echo no)"
echo "== G4 wrapper --entry worker, permission mode auto (refused as input: exit 2)"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $ARGS --permission-mode auto --run-dir $T/run-g4 | show G4 $T/run-g4
echo "== G5 wrapper --entry worker, relay requested"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $ARGS --relay-repo eslint/eslint --run-dir $T/run-g5 | show G5 $T/run-g5
echo "== G6 wrapper --entry worker, staged deep-research"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $ARGS --execution-profile deep-research --run-dir $T/run-g6 | show G6 $T/run-g6
echo "== G7 wrapper --entry worker, staged corpus-extract"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $ARGS --execution-profile corpus-extract --run-dir $T/run-g7 | show G7 $T/run-g7
echo "== G8 wrapper --entry worker, another model"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $BASE --model claude-sonnet-5-5 --effort medium --run-dir $T/run-g8 | show G8 $T/run-g8
echo "== G9 wrapper --entry worker, in scope, claude off PATH (stops at the runtime pin)"
"${WRAP[@]}" --entry worker --egress none --timeout 60 -- $ARGS --run-dir $T/run-g9 | show G9 $T/run-g9
sudo rm -rf $T
echo "== after $(state) tmp_osr_g=$(ls -d /tmp/osr-g-* 2>/dev/null | wc -l)"
