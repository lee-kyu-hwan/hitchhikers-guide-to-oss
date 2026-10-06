#!/bin/bash
# 후보 verifier 읽기 entry(candidate-reads) 사례 실측(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 인자: 배포된 커밋.
# 모두 --egress none, loopback mock만(외부 요청 없음). 실제 api.github.com을 대상으로 한 실행은 하지 않는다
# (relay가 이름을 해석하면 게스트 resolver로 DNS 질의가 나갈 수 있다). 그래서 정식 entry를 유효한 인자로 unit 안에서
# 돌리는 사례는 없다: 정식 entry는 인자 거부(relay 전)와 unit 밖 Blocked만 본다.
#  - mock(--entry verify, unit_inner.py candidate-reads-case): tests/test_oss_research_relay_verify의 loopback mock
#    api.github.com. normal(discover, 모든 읽기 응답 → complete)·anonymous-code-search(익명 코드 검색 401 → partial, 양성)·
#    locus-search(discover 뒤 locus-search)·
#    drop-verifier-file(REVISION_PATHS 한 파일을 읽기 집합에서 뺌)·drop-verifier(verifier 스크립트)·
#    drop-adapter-dependency(relay-collect)·repo-missing(대상 저장소 route 404)·upstream-hang·
#    write-methods(srt 안에서 쓰기 메서드를 relay로 보냄)·direct-regression(proxy를 뺀 직접 접속)·
#    static-rest(--static-reads rest로 README.md·docs/rules를 contents route로 읽는 discover → complete, #256)·
#    static-budget(규칙 문서 30개·예산 58: 정적 읽기 뒤 검색에서 예산 소진 → static_reads 보존, partial, #266).
#  - 부모 EOF: hang 중 wrapper를 SIGKILL.
#  - 정식 entry(--entry candidate-reads): 범위 밖 인자·입력은 relay 전에 exit 2, unit 밖 직접 실행은 Blocked(exit 3).
# 증거: /var/lib/oss-research-evidence/candidate-reads-<commit>/<case>/{record.json, <stage>.tar, ...}.
# 회수 복사는 cp -P(링크를 따라가지 않는다): 링크는 tar 멤버로 남아 pickup이 거부한다.
set -u
C=$1; S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
E=/var/lib/oss-research-evidence/candidate-reads-$C
sudo install -d -m 0700 $E
ANALYSIS=$S/skills/verifying-open-source-contribution-candidates/tests/fixtures/common/analysis.json
ENV=(env -i PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 PYTHONDONTWRITEBYTECODE=1)
WRAP="python3 -I -B $S/bin/oss-research-linux-run"
state() { printf 'units=%s procs=%s pin_elements=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)" \
  "$(sudo nft -j list set inet oss_research egress4 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(x["set"].get("elem",[])) for x in d["nftables"] if "set" in x))')"; }
pack() {  # $1 stage run dir, $2 case dir, $3 stage name (discover|locus-search), $4 tar name
  local doc=discovery.json; [ $3 = locus-search ] && doc=locus-search.json
  sudo install -d -m 0700 $2/pack-$4
  for f in $doc verification-manifest.json transport.json; do sudo cp -P $1/output/$f $2/pack-$4/ 2>/dev/null; done
  for f in relay.log read-envelope.json; do sudo cp -P $1/result/$f $2/pack-$4/ 2>/dev/null; done
  sudo cp -P $1/result/manifest.json $2/pack-$4/run-manifest.json 2>/dev/null
  members=(); for f in $doc verification-manifest.json transport.json relay.log run-manifest.json read-envelope.json; do
    sudo test -e $2/pack-$4/$f && members+=($f); done
  sudo tar -C $2/pack-$4 -cf $2/$4.tar "${members[@]}"
}
summary() {  # $1 record
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["unit"], r["entry"], r["exit_code"], r["failures"])
try:
    i=json.loads(r["stdout"].strip().splitlines()[-1])
except Exception:
    i={}
print(json.dumps({k: i.get(k) for k in ("stage","status","manifest","upstream_requests","upstream_non_get","upstream_authorization","upstream_proxy_authorization","output_files","policy_network","regression")})[:2400]); print("stderr_tail", repr(i.get("stderr_tail", r.get("stderr", ""))[-300:]))' $1 2>&1 | head -5
}
echo "== before $(state)"
for CASE in normal anonymous-code-search locus-search drop-verifier-file drop-verifier drop-adapter-dependency repo-missing upstream-hang write-methods direct-regression static-rest static-budget; do
  OUT=$(sudo mktemp -d /tmp/osr-v-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 200 -- candidate-reads-case $OUT $CASE > /tmp/osr-vrec.$CASE 2>/tmp/osr-verr.$CASE
  rc=$?
  D=$E/$CASE; sudo install -d -m 0700 $D; sudo cp /tmp/osr-vrec.$CASE $D/record.json
  case $CASE in
    locus-search) pack $OUT/locus/run $D locus-search locus; pack $OUT/discover/run $D discover discover ;;
    write-methods|direct-regression) sudo cp -a $OUT/probe $D/probe 2>/dev/null ;;
    *) pack $OUT/discover/run $D discover discover ;;
  esac
  sudo cp -a $OUT/discover/run/result $D/result 2>/dev/null
  echo "== mock $CASE wrapper rc=$rc links=$(sudo find $D -type l | wc -l) $(state)"
  summary /tmp/osr-vrec.$CASE
  sudo rm -rf $OUT /tmp/osr-vrec.$CASE /tmp/osr-verr.$CASE
done
# parent EOF: SIGKILL the wrapper during a hanging read
OUT=$(sudo mktemp -d /tmp/osr-v-out-XXXX); sudo chown ossresearch:ossresearch $OUT
sudo "${ENV[@]}" $WRAP --entry verify --egress none --timeout 300 -- candidate-reads-case $OUT upstream-hang > /tmp/osr-vrec.eof 2>&1 &
sleep 12
echo "== parent-eof before kill: $(state) srt_procs=$(pgrep -u ossresearch -fc srt || true)"
sudo pkill -KILL -f "oss-research-linux-run --entry verify" ; sleep 5
echo "== parent-eof after SIGKILL+5s: $(state)"
sudo rm -rf $OUT /tmp/osr-vrec.eof
# production entry through the wrapper: out-of-range arguments and inputs are refused before any relay (no network)
IN=$(sudo mktemp -d /tmp/osr-v-in-XXXX); sudo chown ossresearch:ossresearch $IN; sudo chmod 755 $IN
sudo -u ossresearch cp $ANALYSIS $IN/analysis.json; sudo chmod 644 $IN/analysis.json
refuse() {  # $1 label, rest: entry arguments after --out-dir/--input-dir
  local label=$1; shift
  OUT=$(sudo mktemp -d /tmp/osr-v-out-XXXX); sudo chown ossresearch:ossresearch $OUT
  sudo "${ENV[@]}" $WRAP --entry candidate-reads --egress none --timeout 60 -- --out-dir $OUT --input-dir $IN "$@" > /tmp/osr-vrec.bad 2>&1
  echo "== entry refuses [$label] wrapper rc=$? $(state) out_entries=$(sudo ls -A $OUT | wc -l)"
  sudo python3 -c 'import json,sys; r=json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]); print("record", r["entry"], r["exit_code"], r["failures"], repr(" ".join(r.get("stderr","").split())[-170:]))' /tmp/osr-vrec.bad
  sudo cp /tmp/osr-vrec.bad $E/refuse-$(echo $label | tr ' /' '--').record.json; sudo rm -rf $OUT /tmp/osr-vrec.bad
}
refuse "candidates 6" --stage discover --repo octo/demo --pattern PAT-001 --max-candidates-total 6
refuse "budget 61" --stage discover --repo octo/demo --pattern PAT-001 --request-budget 61
refuse "no pattern" --stage discover --repo octo/demo
refuse "six patterns" --stage discover --repo octo/demo --pattern PAT-001 --pattern PAT-002 --pattern PAT-003 --pattern PAT-004 --pattern PAT-005 --pattern PAT-006
refuse "pattern not in analysis" --stage discover --repo octo/demo --pattern PAT-099
refuse "clue on discover" --stage discover --repo octo/demo --pattern PAT-001 --clue x
refuse "repo with an option" --stage discover --repo "octo/demo --allow-clone" --pattern PAT-001
refuse "clone option" --stage discover --repo octo/demo --pattern PAT-001 --allow-clone
refuse "upstream option" --stage discover --repo octo/demo --pattern PAT-001 --upstream http://127.0.0.1:1
refuse "static dir dot-dot" --stage discover --repo octo/demo --pattern PAT-001 --static-reads rest --static-dir docs/../x
refuse "static budget 57" --stage discover --repo octo/demo --pattern PAT-001 --static-reads rest --request-budget 57
refuse "unknown stage" --stage record --repo octo/demo --pattern PAT-001
refuse "locus-search without its inputs" --stage locus-search --repo octo/demo --clue x
sudo touch $IN/extra.json; sudo chmod 644 $IN/extra.json
refuse "extra input file" --stage discover --repo octo/demo --pattern PAT-001
sudo rm -f $IN/extra.json; sudo chmod 777 $IN
refuse "input directory writable by other" --stage discover --repo octo/demo --pattern PAT-001
sudo chmod 755 $IN
# outside the unit: a valid request, run directly as the research user (no wrapper, no unit marker)
OUT=$(sudo mktemp -d /tmp/osr-v-out-XXXX); sudo chown ossresearch:ossresearch $OUT
sudo -u ossresearch env -i HOME=/home/ossresearch PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 \
  python3 -I -B $S/bin/oss-research-candidate-reads --out-dir $OUT --input-dir $IN --stage discover --repo octo/demo --pattern PAT-001 \
  < /dev/null > /tmp/osr-vrec.out 2>&1
echo "== entry outside the unit rc=$? $(state) out_entries=$(sudo ls -A $OUT | wc -l) stderr=$(tr -s '\n ' ' ' < /tmp/osr-vrec.out | cut -c1-200)"
sudo cp /tmp/osr-vrec.out $E/outside-unit.txt
sudo rm -rf $OUT $IN /tmp/osr-vrec.out
sudo chmod -R go-rwx $E
echo "== final $(state) outs=$(ls -d /tmp/osr-v-out-* /tmp/osr-v-in-* 2>/dev/null | wc -l)"
