#!/bin/bash
# #189 unit wrapper --egress pin 비AI 통합 실측 (게스트, 관리 계정 vmadmin 이 실행. 내부에서 sudo).
#
#   bash egress_integration.sh <소스 트리 /opt/oss-research/src-<commit>> <srt prefix> <node bin dir>
#
# 대상은 모두 게스트 안 로컬 fixture다(B의 mock_dns.py·mock_tls.py를 그대로 쓴다):
#   - mock DNS 127.0.0.77:53: api.github.com=140.82.112.5, api.anthropic.com=140.82.112.7
#   - mock TLS: netns osr-a 안 140.82.112.7:443, :8443 (이 검사용 fixture CA)
#   - 사설 주소로 풀리는 fixture 이름 denied-resolve.test (/etc/hosts 표지 블록, 10.255.255.1)
# 공인 주소 모양 IP는 /32 경로로 veth만 가리키고, osr_a_test 테이블이 veth 밖으로 가는
# 패킷을 drop한다(시작 전 ip route get 확인). 공개 DNS·인터넷·모델·GitHub에는 요청하지 않는다.
# 결과 줄: PASS/FAIL/BLOCKED/INFO. 마지막에 정리한다.
set -u
SRC="${1:?source tree}"
PREFIX="${2:?srt prefix}"
NODE_BIN="${3:?node bin dir}"
WRAPPER="$SRC/bin/oss-research-linux-run"
TOOL="$SRC/bin/oss-research-egress-pin"
FIX="$SRC/tests/fixtures/oss_research_linux_vm/egress"
ANTH_IP=140.82.112.7
GH_IP=140.82.112.5
DNS_IP=127.0.0.77
PRIVATE_IP=10.255.255.1
WORK=/run/osr-a
PY=/usr/bin/python3
MARK_BEGIN="# BEGIN osr-a-fixture"
MARK_END="# END osr-a-fixture"
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
blocked() { echo "BLOCKED $*"; fails=$((fails + 1)); }
info() { echo "INFO $*"; }

cleanup() {
  sudo "$PY" -B "$TOOL" clear >/dev/null 2>&1
  [ -n "${DNS_PID:-}" ] && sudo kill "$DNS_PID" 2>/dev/null
  sudo ip netns pids osr-a 2>/dev/null | xargs -r sudo kill -9 2>/dev/null
  sudo ip netns del osr-a 2>/dev/null
  sudo ip route del "$ANTH_IP/32" 2>/dev/null
  sudo ip link del osr-a0 2>/dev/null
  sudo ip addr del "$DNS_IP/32" dev lo 2>/dev/null
  sudo nft delete table inet osr_a_test 2>/dev/null
  sudo sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" /etc/hosts
  sudo rm -rf "$WORK"
}
trap cleanup EXIT
elems() { sudo nft -j list set inet oss_research egress4 | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(len([e for x in d["nftables"] if "set" in x for e in x["set"].get("elem",[])]))'; }
state_clean() { [ "$(elems)" = 0 ] && ! sudo test -e /run/oss-research-egress/manifest.json \
  && ! grep -q "BEGIN oss-research-egress-pin" /etc/hosts; }

cleanup
info "source $SRC wrapper sha256=$(sha256sum "$WRAPPER" | cut -c1-64) tool sha256=$(sha256sum "$TOOL" | cut -c1-64)"
if state_clean; then pass P0-no-pin-before; else fail "P0-no-pin-before elems=$(elems)"; fi

# 1. 로컬 네트워크 fixture. 인터넷 경로가 없음을 먼저 확인한다.
sudo nft -f - <<EOF
table inet osr_a_test {
  chain out {
    type filter hook output priority -10; policy accept;
    ip daddr { $ANTH_IP, $GH_IP } oifname != "osr-a0" counter drop
  }
}
EOF
sudo ip netns add osr-a
sudo ip link add osr-a0 type veth peer name osr-a1
sudo ip link set osr-a1 netns osr-a
sudo ip addr add 169.254.78.1/30 dev osr-a0
sudo ip link set osr-a0 up
sudo ip netns exec osr-a ip addr add 169.254.78.2/30 dev osr-a1
sudo ip netns exec osr-a ip addr add "$ANTH_IP/32" dev osr-a1
sudo ip netns exec osr-a ip link set osr-a1 up
sudo ip netns exec osr-a ip link set lo up
sudo ip netns exec osr-a ip route add default via 169.254.78.1
sudo ip route add "$ANTH_IP/32" via 169.254.78.2 dev osr-a0
r="$(ip route get "$ANTH_IP" 2>&1)"; info "R0 route get $ANTH_IP -> $r"
if echo "$r" | grep -q 'dev osr-a0'; then pass R0-model-address-routes-only-to-veth
else fail R0-model-address-routes-only-to-veth; echo "SUMMARY fails=$fails (aborted before traffic)"; exit 1; fi

# 2. fixture CA와 인증서, mock TLS(443·8443), mock DNS, 사설로 풀리는 이름.
sudo install -d -m 0700 "$WORK"
# Python 3.13 은 X.509 strict 검증이 기본이다: CA·leaf 에 표준 확장을 둔다(검증은 끄지 않는다).
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=osr-a-ca" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" -keyout "$WORK/ca.key" -out "$WORK/ca.pem" 2>/dev/null
sudo openssl req -newkey rsa:2048 -nodes -subj "/CN=api.anthropic.com" -keyout "$WORK/leaf.key" -out "$WORK/leaf.csr" 2>/dev/null
printf 'subjectAltName=DNS:api.anthropic.com,IP:%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n' "$ANTH_IP" | sudo tee "$WORK/san.ext" >/dev/null
sudo openssl x509 -req -in "$WORK/leaf.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" -CAcreateserial -days 1 -extfile "$WORK/san.ext" -out "$WORK/leaf.pem" 2>/dev/null
sudo chmod 0644 "$WORK/ca.pem"; sudo chmod 0711 "$WORK"
sudo bash -c "ip netns exec osr-a nohup $PY -B $FIX/mock_tls.py $WORK/leaf.pem $WORK/leaf.key $ANTH_IP:443 $ANTH_IP:8443 >$WORK/tls.log 2>&1 &"
sudo ip addr add "$DNS_IP/32" dev lo
DNS_PID="$(sudo bash -c "nohup $PY -B $FIX/mock_dns.py $DNS_IP api.github.com=$GH_IP api.anthropic.com=$ANTH_IP >$WORK/dns.log 2>&1 & echo \$!")"
printf '%s\n%s denied-resolve.test\n%s\n' "$MARK_BEGIN" "$PRIVATE_IP" "$MARK_END" | sudo tee -a /etc/hosts >/dev/null
sleep 1
# 대조: root(규칙 대상 아님)는 두 포트 모두에 닿는다 → 8443 거부는 서버 부재가 아니다.
CTRL=1
for port in 443 8443; do
  if sudo curl -sS -m 5 --cacert "$WORK/ca.pem" --resolve "api.anthropic.com:$port:$ANTH_IP" "https://api.anthropic.com:$port/" >/dev/null 2>&1
  then pass "C1-root-reaches-fixture-$port"; else fail "C1-root-reaches-fixture-$port"; CTRL=0; fi
done

# 3. wrapper --egress pin, 비AI entry egress (제품 policy + fixture CA 읽기 + fixture 이름 하나).
OUT="$(sudo mktemp -d /tmp/osr-a-out-XXXX)"; sudo chown ossresearch:ossresearch "$OUT"
record="$(sudo env -i PATH="$NODE_BIN:/usr/bin:/bin" LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX="$PREFIX" \
  OSS_RESEARCH_EGRESS_RESOLVER="$DNS_IP" PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -I -B "$WRAPPER" --entry verify --egress pin --timeout 120 -- egress "$OUT" "$WORK/ca.pem")"; rc=$?
echo "$record" | tail -1 | sudo tee "$WORK/record.json" >/dev/null
info "W1 wrapper rc=$rc record=$(tail -c 4000 "$WORK/record.json")"
# 판정은 linux/egress_verdict.py(읽기 전용 JSON 판정기, 오프라인 시험 있음)가 한다.
VERDICT="$SRC/tests/fixtures/oss_research_worker/linux/egress_verdict.py"
if [ "$CTRL" = 1 ]; then
  "$PY" -I -B "$VERDICT" "$WORK/record.json" | cut -c1-700
  fails=$((fails + $("$PY" -I -B "$VERDICT" "$WORK/record.json" | grep -cvE '^PASS ')))
else
  blocked "E-all (C1 root controls failed)"
fi
if state_clean; then pass P1-no-pin-after; else fail "P1-no-pin-after elems=$(elems)"; fi
unit="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("unit",""))' "$WORK/record.json")"
if [ -n "$unit" ] && [ "$(systemctl show -p LoadState --value "$unit")" = not-found ] \
   && [ ! -e "/sys/fs/cgroup/system.slice/$unit.service" ]; then pass U1-unit-gone; else fail "U1-unit-gone $unit"; fi
sudo rm -rf "$OUT"
echo "SUMMARY fails=$fails"
exit $([ "$fails" = 0 ] && echo 0 || echo 1)
