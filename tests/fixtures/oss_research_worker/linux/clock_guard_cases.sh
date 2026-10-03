#!/bin/bash
# unit wrapper 시계 검사 사례 실측(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
# 모두 --egress none, 가벼운 `--entry verify -- unit-check`만 쓴다. 네트워크·모델·토큰·pin 없음.
# 시스템 시계를 일부러 과거로 돌리고(RTC는 건드리지 않는다) wrapper가 하는 일을 본다:
#   A 120초 과거 → 재동기(resynced), record에 조치, 시계가 RTC와 맞음
#   B 3초 과거 → 오차 안(none), 아무것도 하지 않음
#   C 8일 과거 → 오차가 너무 커서 Blocked(offset-too-large), unit 시작 안 됨, 시계는 그대로(이후 직접 복구)
#   D 재설정 프로그램을 쓸 수 없게 한 반례(probe가 모듈 상수를 시험용 가짜로 바꾼다):
#     D1 신뢰되는 가짜가 실패(exit 1) → resync-failed  D2 group 쓰기 가능한 가짜 → bootstrap Blocked
#     D3 없는 경로 → bootstrap Blocked. 모두 launch 0회, 남는 unit·프로세스 0.
# 사례마다 끝에 시스템 시계를 RTC에서 직접 복구하고 차이를 적는다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
ENV=(env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1)
WRAP="python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run"
rtc() { sudo cat /sys/class/rtc/rtc0/since_epoch; }
offset() { sudo python3 -c "import time; print(round(time.time() - $(rtc), 3))"; }
restore() { sudo /usr/bin/date -u -s "@$(rtc)" > /dev/null; echo "   restored from RTC: offset now $(offset)"; }
state() { printf 'units=%s procs=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)"; }
summary() {  # $1 record file
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("   record", json.dumps({"unit": r.get("unit"), "exit_code": r.get("exit_code"), "failures": r.get("failures"), "clock": r.get("clock")}))' $1 2>&1 | head -3
}
echo "== before $(state) offset=$(offset)"
for CASE in A B C; do
  case $CASE in A) BACK=120 ;; B) BACK=3 ;; C) BACK=$((8*86400)) ;; esac
  OUT=$(sudo mktemp -d /tmp/osr-z-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  sudo /usr/bin/date -u -s "@$(( $(rtc) - BACK ))" > /dev/null
  echo "== $CASE system clock set $BACK s into the past; offset=$(offset)"
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 60 -- unit-check $OUT > /tmp/osr-zrec.$CASE 2> /tmp/osr-zerr.$CASE
  echo "   wrapper rc=$? $(state) stderr=$(tr -s '\n ' ' ' < /tmp/osr-zerr.$CASE | cut -c1-220)"
  summary /tmp/osr-zrec.$CASE
  echo "   offset after the wrapper: $(offset)"
  restore
  sudo rm -rf $OUT
done
P=$(sudo mktemp -d /root/clockprobe-XXXX); sudo chmod 755 $P
sudo bash -c "printf '#!/bin/sh\nexit 1\n' > $P/date-fails; chmod 755 $P/date-fails; printf '#!/bin/sh\nexit 0\n' > $P/date-writable; chmod 775 $P/date-writable"
for CASE in D1:$P/date-fails D2:$P/date-writable D3:$P/does-not-exist; do
  NAME=${CASE%%:*}; FAKE=${CASE#*:}
  echo "== $NAME clock-set program = $FAKE"
  sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 python3 -I -B $S/tests/fixtures/oss_research_worker/linux/clock_guard_probe.py $S $FAKE > /tmp/osr-zprobe.$NAME 2>&1
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("   probe", json.dumps(r))' /tmp/osr-zprobe.$NAME 2>&1 | cut -c1-900
  echo "   $(state) offset=$(offset)"
  restore
done
sudo rm -rf $P /tmp/osr-zrec.* /tmp/osr-zerr.* /tmp/osr-zprobe.*
echo "== final $(state) offset=$(offset) outs=$(ls -d /tmp/osr-z-out-* 2>/dev/null | wc -l) probes=$(sudo ls -d /root/clockprobe-* 2>/dev/null | wc -l)"
