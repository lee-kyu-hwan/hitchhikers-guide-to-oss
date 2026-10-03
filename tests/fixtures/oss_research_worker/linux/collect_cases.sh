#!/bin/bash
# #189 정식 수집 entry 사례 실측(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
# 모두 --egress none, loopback mock만(외부 요청 없음). normal·drop-tz·drop-collector·upstream-missing·
# upstream-hang, 부모 EOF(wrapper SIGKILL), 정식 --entry collect 인자 거부. 증거는 /var/lib/oss-research-evidence/collect-entry-<commit>/.
# 회수 복사는 cp -P(링크를 따라가지 않는다): 링크는 tar 멤버로 남아 pickup이 거부한다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
E=/var/lib/oss-research-evidence/collect-entry-$C
sudo install -d -m 0700 $E
state() { printf 'units=%s procs=%s ' "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" "$(ps -u ossresearch -o pid= | grep -c .)"; }
for CASE in normal drop-tz drop-collector upstream-missing upstream-hang; do
  OUT=$(sudo mktemp -d /tmp/osr-c-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  if [ $CASE = normal ]; then ARGS=(collect $OUT); T=200; else ARGS=(collect-case $OUT $CASE); T=200; fi
  sudo env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1 \
    python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run --entry verify --egress none --timeout $T -- "${ARGS[@]}" > /tmp/osr-rec.$CASE 2>/tmp/osr-err.$CASE
  rc=$?
  D=$E/$CASE; sudo install -d -m 0700 $D; sudo cp /tmp/osr-rec.$CASE $D/record.json
  R=$OUT/run
  sudo cp -a $R/result $D/result 2>/dev/null
  if [ $CASE = normal ]; then
    sudo install -d -m 0700 $D/pack
    sudo cp -P $R/output/corpus.json $R/output/collection-manifest.json $R/output/inventory.md $R/output/transport.json $R/result/relay.log $R/result/collection-envelope.json $D/pack/ && sudo cp -P $R/result/manifest.json $D/pack/run-manifest.json && sudo tar -C $D/pack -cf $D/collect.tar corpus.json collection-manifest.json inventory.md transport.json relay.log run-manifest.json collection-envelope.json
    sudo cp -a $OUT/direct $D/direct 2>/dev/null
  fi
  echo "== $CASE wrapper rc=$rc links=$(sudo find $D -type l | wc -l) $(state)"
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["unit"], r["exit_code"], r["failures"]); i=json.loads(r["stdout"].strip().splitlines()[-1]); print(json.dumps({k: i.get(k) for k in ("succeeded","manifest","upstream_requests","upstream_unmatched","upstream_authorization","upstream_proxy_authorization","output_files","direct_regression")})[:1500]); print("stderr_tail", repr(i.get("stderr_tail","")[-300:]))' /tmp/osr-rec.$CASE 2>&1 | head -5
  sudo rm -rf $OUT /tmp/osr-rec.$CASE /tmp/osr-err.$CASE
done
# parent EOF: SIGKILL the wrapper during a hanging collection
OUT=$(sudo mktemp -d /tmp/osr-c-out-XXXX); sudo chown ossresearch:ossresearch $OUT
sudo env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1 \
  python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run --entry verify --egress none --timeout 300 -- collect-case $OUT upstream-hang > /tmp/osr-rec.eof 2>&1 &
sleep 12
echo "== parent-eof before kill: $(state) srt_procs=$(pgrep -u ossresearch -fc srt || true)"
sudo pkill -KILL -f "executable_oss-research-linux-run --entry verify" ; sleep 5
echo "== parent-eof after SIGKILL+5s: $(state)"
sudo rm -rf $OUT /tmp/osr-rec.eof
# production entry wiring through the wrapper: bad input refused before any relay (no network)
OUT=$(sudo mktemp -d /tmp/osr-c-out-XXXX); sudo chown ossresearch:ossresearch $OUT
sudo env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1 \
  python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run --entry collect --egress none --timeout 60 -- \
  --out-dir $OUT --repo eslint/eslint --start-at 2026-09-01T00:00:00Z --end-at 2026-09-08T00:00:00Z --max-per-repo 4 > /tmp/osr-rec.bad 2>&1
echo "== entry collect bad-input wrapper rc=$? $(state) out_entries=$(sudo ls -A $OUT | wc -l)"
sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["entry"], r["exit_code"], r["failures"], repr(r.get("stderr",""))[:300])' /tmp/osr-rec.bad
sudo cp /tmp/osr-rec.bad $E/entry-collect-bad-input.record.json; sudo rm -rf $OUT /tmp/osr-rec.bad
sudo chmod -R go-rwx $E
echo "== final $(state) outs=$(ls -d /tmp/osr-c-out-* 2>/dev/null | wc -l)"
