#!/bin/bash
# #192 후속: 문서 위주 조사를 위한 수집 entry 보완의 게스트 실측(관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
#
#   limactl shell --workdir / <vm> -- bash /opt/oss-research/src-<COMMIT>/tests/fixtures/oss_research_worker/linux/collect_docs_cases.sh COMMIT
#
# 모두 --egress none, loopback mock만(외부 요청 없음, 토큰 없음). 정식 entry(--entry verify 의 unit_inner collect-case /
# collect-issues-case)가 실제 relay·srt 안에서 돈다. 사례:
#   PR 수집  docs-only-title · docs-only-label(검색 형태 + 제목/경로 규칙, 요청 4건 중 1건 통과), cap-five(PR 6건 중 5건),
#            unmerged(--outcome closed-unmerged: 검색에 is:unmerged, 부정 토큰 없음), empty(검색 0건: complete·records 0),
#            quota(응답의 x-ratelimit-* 가 transport 기록에 남는다)
#   이슈     closed(--state closed --date-field closed), cap-15(cap 15 + 예산 70)
#   거부     --max-per-repo 6, --docs-only body, collect-issues --request-budget 91 (relay 전에 거부, 출력 0)
# 수집 사례의 산출물은 cp -P(링크를 따라가지 않는다)로 root 전용 증거 경로에 보존하고 tar로 묶는다. 회수 판정은 호스트
# verify_pickup.py(--collect/--issues, loopback-mock)가 한다. 사례마다 끝에 unit·프로세스 0 을 본다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
E=/var/lib/oss-research-evidence/collect-docs-$C
sudo install -d -m 0700 $E
state() { printf 'units=%s procs=%s' "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)"; }
ENV=(env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1)
WRAP="python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run"
KEYS='("succeeded","kept","status","manifest","upstream_requests","upstream_unmatched","upstream_authorization","corpus_records","collection_status","repository_status","outcome","docs_only","docs_filter","matched_count","request_count","search_queries","transport_rate_limits","transport_requests","transport_credential_scope","github_token")'
fails=0
echo "== before $(state)"
run_case() {  # $1 mode (collect-case|collect-issues-case) $2 case $3 kind (collect|issues)
  local MODE=$1 CASE=$2 KIND=$3
  OUT=$(sudo mktemp -d /tmp/osr-cd-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 200 -- $MODE $OUT $CASE > /tmp/osr-cd-rec.$CASE 2> /tmp/osr-cd-err.$CASE
  rc=$?
  D=$E/$CASE; sudo install -d -m 0700 $D; sudo cp /tmp/osr-cd-rec.$CASE $D/record.json
  R=$OUT/run
  sudo install -d -m 0700 $D/pack
  if [ $KIND = collect ]; then
    for f in corpus.json collection-manifest.json inventory.md transport.json; do sudo cp -P $R/output/$f $D/pack/; done
    for f in relay.log collection-envelope.json; do sudo cp -P $R/result/$f $D/pack/; done
    sudo cp -P $R/result/manifest.json $D/pack/run-manifest.json
    sudo tar -C $D/pack -cf $D/$KIND.tar corpus.json collection-manifest.json inventory.md transport.json relay.log run-manifest.json collection-envelope.json
  else
    for f in corpus.json collection-manifest.json transport.json; do sudo cp -P $R/output/$f $D/pack/; done
    for f in relay.log collection-envelope.json; do sudo cp -P $R/result/$f $D/pack/; done
    sudo cp -P $R/result/manifest.json $D/pack/run-manifest.json
    sudo tar -C $D/pack -cf $D/$KIND.tar corpus.json collection-manifest.json transport.json relay.log run-manifest.json collection-envelope.json
  fi
  echo "== $CASE ($MODE) wrapper rc=$rc links=$(sudo find $D -type l | wc -l) $(state)"
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("   record", r["unit"], r["entry"], "exit", r["exit_code"], r["failures"]); i=json.loads(r["stdout"].strip().splitlines()[-1]); keys='"$KEYS"'; print("   inner", json.dumps({k: i.get(k) for k in keys})[:1800]); print("   stderr_tail", repr(i.get("stderr_tail","")[-200:]))' /tmp/osr-cd-rec.$CASE 2>&1 | head -4
  sudo rm -rf $OUT /tmp/osr-cd-rec.$CASE /tmp/osr-cd-err.$CASE
}
for CASE in docs-only-title docs-only-label cap-five unmerged empty quota; do run_case collect-case $CASE collect; done
for CASE in closed cap-15; do run_case collect-issues-case $CASE issues; done

refuse() {  # $1 label $2 entry, rest: entry arguments
  local LABEL=$1 ENTRY=$2; shift 2
  OUT=$(sudo mktemp -d /tmp/osr-cd-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  sudo "${ENV[@]}" $WRAP --entry $ENTRY --egress none --timeout 60 -- --out-dir $OUT "$@" > /tmp/osr-cd-rec.refuse 2>&1
  echo "== refuse $LABEL wrapper rc=$? out_entries=$(sudo ls -A $OUT | wc -l) $(state)"
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("   record", r["entry"], "exit", r["exit_code"], r["failures"], repr(" ".join(r.get("stderr","").split())[-160:]))' /tmp/osr-cd-rec.refuse
  sudo cp /tmp/osr-cd-rec.refuse $E/refuse-$LABEL.record.json; sudo rm -rf $OUT /tmp/osr-cd-rec.refuse
}
PR="--repo eslint/eslint --start-at 2026-09-01T00:00:00Z --end-at 2026-09-08T00:00:00Z"
refuse cap-6 collect $PR --max-per-repo 6
refuse docs-only-body collect $PR --docs-only body
refuse issue-budget-91 collect-issues --repo eslint/eslint --date-field updated --state open --start-at 2026-07-05T00:00:00Z --end-at 2026-10-03T00:00:00Z --request-budget 91
refuse issue-open-closed collect-issues --repo eslint/eslint --date-field closed --state open --start-at 2026-07-05T00:00:00Z --end-at 2026-10-03T00:00:00Z
sudo chmod -R go-rwx $E
echo "== final $(state) outs=$(ls -d /tmp/osr-cd-out-* 2>/dev/null | wc -l) fails=$fails"
