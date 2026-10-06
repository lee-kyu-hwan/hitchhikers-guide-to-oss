#!/bin/bash
# #78/#189 candidate-reads 실제 읽기 1단계(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo).
#
#   bash candidate_reads_live.sh COMMIT STAGE IN_DIR -- <entry 인자: --stage … --repo … 등>
#
# 정식 경로만 쓴다: linux-run --entry candidate-reads --egress pin(resolver 192.168.5.2) →
# executable_oss-research-candidate-reads(상한은 entry의 것) → relay(token 없음, GET 허용목록) →
# srt child relay-verify --require-proxy. mock·sitecustomize 없음. 모델·credential 없음. GitHub 쓰기 없음.
# IN_DIR은 호스트가 미리 만든 입력 디렉터리(ossresearch 소유, group·other 쓰기 불가)다.
# 결과는 cp -P(링크를 따라가지 않음)로 root 전용 증거 경로에 보존하고 tar로 묶는다. 회수 판정은 호스트
# verify_pickup.py --candidate-reads --upstream github-rest-via-relay가 한다. 실패·부분도 그대로 보존한다.
# 단계 하나만 실행하고 다시 시도하지 않는다.
set -u
C="$1"; STAGE="$2"; IN="$3"; shift 4
S=/opt/oss-research/src-$C; NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
state() { printf 'units=%s procs=%s pin_elements=%s pin_manifest=%s hosts_block=%s' \
  "$(systemctl list-units --all 'oss-research-*' --no-legend | grep -v oss-research-nft | grep -c .)" \
  "$(ps -u ossresearch -o pid= | grep -c .)" \
  "$(sudo nft -j list set inet oss_research egress4 | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(x["set"].get("elem",[])) for x in d["nftables"] if "set" in x))')" \
  "$(sudo test -e /run/oss-research-egress/manifest.json && echo yes || echo no)" \
  "$(grep -c 'BEGIN oss-research-egress-pin' /etc/hosts)"; }
echo "INFO before $(state)"
OUT=$(sudo mktemp -d /tmp/osr-live-out-XXXX); sudo chown ossresearch:ossresearch "$OUT"
echo "INFO start $(date -u +%FT%TZ)"
# The redirect runs as root (the record lives in root-only /run).
sudo bash -c 'env -i "$@" > /run/osr-live-record.json 2> /run/osr-live-wrapper.err' _ \
  PATH=$NODE:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=/opt/oss-research/srt-0.0.77 \
  OSS_RESEARCH_EGRESS_RESOLVER=192.168.5.2 PYTHONDONTWRITEBYTECODE=1 \
  python3 -I -B $S/dot_local/bin/executable_oss-research-linux-run --entry candidate-reads --egress pin --timeout 660 -- \
  --out-dir "$OUT" --input-dir "$IN" "$@" --timeout 600
rc=$?
echo "INFO end $(date -u +%FT%TZ) wrapper rc=$rc"
unit=$(sudo python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]).get("unit",""))' /run/osr-live-record.json 2>/dev/null)
E=/var/lib/oss-research-evidence/a78-candidate-reads-live-$STAGE-${unit:-no-unit-$$}
sudo install -d -m 0700 "$E" "$E/pack"
sudo cp /run/osr-live-record.json "$E/record.json"; sudo cp /run/osr-live-wrapper.err "$E/wrapper.err"
R=$OUT/run
DOC=discovery.json; [ "$STAGE" = locus-search ] && DOC=locus-search.json
for f in $DOC verification-manifest.json transport.json; do sudo cp -P "$R/output/$f" "$E/pack/" 2>/dev/null; done
for f in relay.log read-envelope.json stderr.log; do sudo cp -P "$R/result/$f" "$E/pack/" 2>/dev/null; done
sudo cp -P "$R/result/manifest.json" "$E/pack/run-manifest.json" 2>/dev/null
members=(); for f in $DOC verification-manifest.json transport.json relay.log run-manifest.json read-envelope.json; do
  sudo test -e "$E/pack/$f" && members+=("$f"); done
sudo tar -C "$E/pack" -cf "$E/$STAGE.tar" "${members[@]}"
sudo chmod -R go-rwx "$E"
echo "INFO kept $E members=${members[*]} links=$(sudo find "$E" -type l | wc -l)"
sudo python3 - /run/osr-live-record.json <<'PY'
import json, sys
r = json.loads(open(sys.argv[1]).read().strip().splitlines()[-1])
pin = r.get("egress_pin") or {}
print("INFO record", json.dumps({k: r.get(k) for k in ("unit", "entry", "egress", "exit_code", "failures")}))
print("INFO pin", json.dumps({k: (pin.get(k) or {}).get("exit") for k in ("apply", "verify", "clear")}),
      json.dumps((pin.get("apply") or {}).get("nft_elements")))
print("INFO inner", r.get("stdout", "").strip()[-800:])
print("INFO entry stderr", r.get("stderr", "").strip()[-800:])
PY
sudo rm -rf "$OUT" /run/osr-live-record.json /run/osr-live-wrapper.err
echo "INFO after $(state) outs=$(ls -d /tmp/osr-live-out-* 2>/dev/null | wc -l)"
exit $rc
