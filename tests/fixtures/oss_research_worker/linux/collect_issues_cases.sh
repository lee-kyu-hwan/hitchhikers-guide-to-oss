#!/bin/bash
# 비AI 이슈 수집 entry 사례 실측(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
# 모두 --egress none, 외부 요청 없음.
#  - mock(--entry verify, unit_inner.py collect-issues): tests/test_oss_research_relay_issues의 loopback mock
#    api.github.com. normal(익명 GraphQL 401 → partial, 양성)·complete(GraphQL 200)·drop-tz·drop-collector·
#    drop-sibling·drop-relay-collect·upstream-missing·upstream-hang·rate-limit-429(GraphQL 429 Retry-After 31: relay 기한 30초 초과,
#    collector가 31초씩 실제 대기)·graphql-zero-quota(실측 익명 403 limit 0: 이슈당 1요청, 대기 없음).
#  - sentinel(--entry collect-issues, 제품 entry 그대로): mock 없음. relay가 upstream에 나가지 못하므로 failed로
#    기록되고 호스트 pickup이 거부해야 한다. 입력 오류(--date-field 없음, cap 21)는 relay 전 거부.
# 증거: /var/lib/oss-research-evidence/collect-issues-<commit>/<case>/{record.json, result/, issues.tar}.
# 회수 복사는 cp -P(링크를 따라가지 않는다): 링크는 tar 멤버로 남아 pickup이 거부한다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
E=/var/lib/oss-research-evidence/collect-issues-$C
sudo install -d -m 0700 $E
ENV=(env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1)
WRAP="python3 -I -B $S/bin/oss-research-linux-run"
state() { printf 'units=%s procs=%s pin_elements=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)" \
  "$(sudo nft -j list set inet oss_research egress4 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(x["set"].get("elem",[])) for x in d["nftables"] if "set" in x))')"; }
pack() {  # $1 run dir, $2 case dir
  sudo install -d -m 0700 $2/pack
  for f in corpus.json collection-manifest.json transport.json; do sudo cp -P $1/output/$f $2/pack/ 2>/dev/null; done
  for f in relay.log collection-envelope.json; do sudo cp -P $1/result/$f $2/pack/ 2>/dev/null; done
  sudo cp -P $1/result/manifest.json $2/pack/run-manifest.json 2>/dev/null
  members=(); for f in corpus.json collection-manifest.json transport.json relay.log run-manifest.json collection-envelope.json; do
    sudo test -e $2/pack/$f && members+=($f); done
  sudo tar -C $2/pack -cf $2/issues.tar "${members[@]}"
}
summary() {  # $1 record
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["unit"], r["entry"], r["exit_code"], r["failures"])
try:
    i=json.loads(r["stdout"].strip().splitlines()[-1])
except Exception:
    i={}
print(json.dumps({k: i.get(k) for k in ("status","manifest","upstream_requests","upstream_posts","upstream_authorization","upstream_proxy_authorization","output_files","policy_network","direct_regression","graphql_transport","graphql_events")})[:2000]); print("stderr_tail", repr(i.get("stderr_tail", r.get("stderr", ""))[-300:]))' $1 2>&1 | head -5
}
echo "== before $(state)"
for CASE in normal complete drop-tz drop-collector drop-sibling drop-relay-collect upstream-missing upstream-hang rate-limit-429 graphql-zero-quota; do
  OUT=$(sudo mktemp -d /tmp/osr-i-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  if [ $CASE = normal ]; then ARGS=(collect-issues $OUT); else ARGS=(collect-issues-case $OUT $CASE); fi
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 200 -- "${ARGS[@]}" > /tmp/osr-irec.$CASE 2>/tmp/osr-ierr.$CASE
  rc=$?
  D=$E/mock-$CASE; sudo install -d -m 0700 $D; sudo cp /tmp/osr-irec.$CASE $D/record.json
  sudo cp -a $OUT/run/result $D/result 2>/dev/null
  pack $OUT/run $D
  [ $CASE = normal ] && sudo cp -a $OUT/direct $D/direct 2>/dev/null
  echo "== mock $CASE wrapper rc=$rc links=$(sudo find $D -type l | wc -l) $(state)"
  summary /tmp/osr-irec.$CASE
  sudo rm -rf $OUT /tmp/osr-irec.$CASE /tmp/osr-ierr.$CASE
done
# sentinel: the production entry, no mock, egress none. The relay cannot reach GitHub.
OUT=$(sudo mktemp -d /tmp/osr-i-out-XXXX); sudo chown ossresearch:ossresearch $OUT
sudo "${ENV[@]}" $WRAP --entry collect-issues --egress none --timeout 200 -- \
  --out-dir $OUT --repo eslint-markdown/eslint-markdown --date-field created --state open \
  --start-at 2026-03-28T00:00:00Z --end-at 2026-09-28T00:00:00Z --max-per-repo 20 --request-budget 60 \
  --timeout 120 > /tmp/osr-irec.sentinel 2>/tmp/osr-ierr.sentinel
rc=$?
D=$E/sentinel-egress-none; sudo install -d -m 0700 $D; sudo cp /tmp/osr-irec.sentinel $D/record.json
sudo cp -a $OUT/run/result $D/result 2>/dev/null; pack $OUT/run $D
echo "== sentinel entry collect-issues egress none wrapper rc=$rc links=$(sudo find $D -type l | wc -l) $(state)"
summary /tmp/osr-irec.sentinel
sudo rm -rf $OUT /tmp/osr-irec.sentinel /tmp/osr-ierr.sentinel
# bad input through the wrapper: refused before any relay
for BAD in no-date-field cap-21; do
  OUT=$(sudo mktemp -d /tmp/osr-i-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  EXTRA=(--date-field created --max-per-repo 20); [ $BAD = no-date-field ] && EXTRA=(--max-per-repo 20)
  [ $BAD = cap-21 ] && EXTRA=(--date-field created --max-per-repo 21)
  sudo "${ENV[@]}" $WRAP --entry collect-issues --egress none --timeout 60 -- \
    --out-dir $OUT --repo eslint-markdown/eslint-markdown --state open \
    --start-at 2026-03-28T00:00:00Z --end-at 2026-09-28T00:00:00Z "${EXTRA[@]}" > /tmp/osr-irec.bad 2>&1
  echo "== bad input $BAD wrapper rc=$? $(state) out_entries=$(sudo ls -A $OUT | wc -l)"
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["entry"], r["exit_code"], r["failures"], repr(r.get("stderr",""))[-200:])' /tmp/osr-irec.bad
  sudo cp /tmp/osr-irec.bad $E/bad-input-$BAD.record.json; sudo rm -rf $OUT /tmp/osr-irec.bad
done
sudo chmod -R go-rwx $E
echo "== final $(state) outs=$(ls -d /tmp/osr-i-out-* 2>/dev/null | wc -l)"
