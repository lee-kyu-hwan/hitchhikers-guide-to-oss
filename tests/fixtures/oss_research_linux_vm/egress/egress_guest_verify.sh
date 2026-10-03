#!/bin/bash
# #78 egress pin 게스트 실측. 전용 VM 안에서 관리 계정(vmadmin)으로 실행한다(내부에서 sudo 사용).
#
#   limactl shell --workdir / <인스턴스> -- bash <이 스크립트> <pin 도구 경로> <이 디렉터리 경로>
#
# 모든 대상은 게스트 안 로컬 fixture다:
#   - mock DNS: 127.0.0.77:53 (lo). 고정 이름 두 개만 답한다.
#   - mock TLS: network namespace `osr-t` 안의 주소. veth `osr-t0`/`osr-t1`로만 닿는다.
#     공인 모양 주소(140.82.112.5/6/7)는 /32 경로로 veth를 가리킨다. 시작 전에 `ip route get`이
#     veth를 가리키는지 확인하고, 아니면 멈춘다. 별도 테이블 `osr_test`가 이 주소들이
#     veth 밖으로 나가는 패킷을 drop한다(이중 안전장치).
#   - TLS는 이 검사용 fixture CA로 검증한다. 검증을 끄는 옵션은 쓰지 않는다.
# 공개 DNS·인터넷·모델·GitHub에는 요청하지 않는다. 기존 ossresearch·DNS·관리 서비스 차단 규칙은 바꾸지 않는다.
# 결과 줄: PASS/FAIL/SKIP/INFO. 마지막에 정리한다.
set -u
TOOL="${1:?pin tool path}"
FIX="${2:?fixture dir}"
ALLOW_IP=140.82.112.5
OTHER_IP=140.82.112.6
ANTH_IP=140.82.112.7
V6_IP=2001:db8:77::5
DNS_IP=127.0.0.77
WORK=/run/osr-t   # 0711: 조사 uid 가 ca.pem(0644)만 읽을 수 있다
R=(sudo -u ossresearch -H env -i HOME=/home/ossresearch PATH=/usr/bin:/bin)
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
skip() { echo "SKIP $*"; }
info() { echo "INFO $*"; }
PY=/usr/bin/python3

cleanup() {
  sudo "$PY" -B "$TOOL" clear >/dev/null 2>&1
  sudo pkill -f "^$PY -B $FIX/mock_dns.py" 2>/dev/null   # 앵커: sudo·pkill 자신은 맞지 않는다
  sudo ip netns pids osr-t 2>/dev/null | xargs -r sudo kill -9 2>/dev/null
  sudo ip netns del osr-t 2>/dev/null
  for ip in $ALLOW_IP $OTHER_IP $ANTH_IP; do sudo ip route del "$ip/32" 2>/dev/null; done
  sudo ip -6 route del "$V6_IP/128" 2>/dev/null
  sudo ip link del osr-t0 2>/dev/null
  sudo ip addr del "$DNS_IP/32" dev lo 2>/dev/null
  sudo nft delete table inet osr_test 2>/dev/null
  sudo rm -rf "$WORK"
}
trap cleanup EXIT
elems() { sudo nft -j list set inet oss_research egress4 | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps([e for x in d["nftables"] if "set" in x for e in x["set"].get("elem",[])]))'; }
counter() { sudo nft -a list chain inet oss_research output | grep '@egress4' | grep -o 'packets [0-9]*' | awk '{print $2}'; }

# resolver 메타데이터(A 통합용): 파일·상태만 읽는다. 질의하지 않는다.
info "M1 /etc/resolv.conf: $(grep -v '^#' /etc/resolv.conf | tr '\n' ';')"
info "M1 /run/systemd/resolve/resolv.conf: $(grep -v '^#' /run/systemd/resolve/resolv.conf 2>/dev/null | tr '\n' ';')"
info "M1 resolvectl dns: $(resolvectl dns 2>/dev/null | tr '\n' ';')"
info "M1 networkctl eth0 DNS: $(networkctl status eth0 --no-pager 2>/dev/null | grep -A2 -E '^ *DNS' | tr -s ' ' | tr '\n' ';')"
info "M1 ip route default: $(ip route show default | tr '\n' ';')"
info "versions: nft=$(sudo nft --version 2>&1) kernel=$(uname -r) openssl=$(openssl version 2>&1) python=$($PY --version 2>&1) curl=$(curl --version | head -1)"
info "tool: $TOOL sha256=$(sha256sum "$TOOL" | cut -c1-64)"
cleanup

# 0. 전제: provision이 egress4를 만들었고 비어 있다.
if [ "$(elems)" = "[]" ]; then pass P0-egress4-empty; else fail "P0-egress4-empty $(elems)"; fi
sudo nft list chain inet oss_research output | sed 's/^/INFO rule /'

# 1. nft JSON 시간 단위 실측: 문서 대역(TEST-NET-1) 원소를 시험 전용으로 넣고 원시 JSON을 기록한다.
sudo nft add element inet oss_research egress4 "{ 192.0.2.1 . 9 timeout 120s }"
raw="$(sudo nft -j list set inet oss_research egress4)"
info "J1-raw-json $raw"
unit="$(echo "$raw" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); e=[e for x in d["nftables"] if "set" in x for e in x["set"]["elem"]][0]["elem"]; t=e["timeout"]; x=e["expires"]; print("seconds" if t==120 else "milliseconds" if t==120000 else "unknown", t, x)')"
info "J1-unit $unit"
case "$unit" in seconds*) pass "J1-json-timeout-seconds ($unit)";; *) fail "J1-json-timeout-seconds ($unit)";; esac
sudo nft list set inet oss_research egress4 | grep -E 'timeout|expires' | sed 's/^/INFO J1-text /'
sudo nft flush set inet oss_research egress4

# 2. kernel 만료: 5초 원소가 사라지는가(프로세스와 무관한 lease).
sudo nft add element inet oss_research egress4 "{ 192.0.2.1 . 9 timeout 5s }"
[ "$(elems)" != "[]" ] && info "K1 element present before expiry"
sleep 7
if [ "$(elems)" = "[]" ]; then pass K1-kernel-expiry-5s; else fail "K1-kernel-expiry-5s $(elems)"; fi

# 3. 로컬 네트워크 fixture (netns + veth), 인터넷 경로가 없음을 먼저 증명한다.
sudo install -d -m 0700 "$WORK"
sudo nft -f - <<EOF
table inet osr_test {
  chain out {
    type filter hook output priority -10; policy accept;
    ip daddr { $ALLOW_IP, $OTHER_IP, $ANTH_IP } oifname != "osr-t0" counter drop
    ip6 daddr $V6_IP oifname != "osr-t0" counter drop
  }
}
EOF
sudo ip netns add osr-t
sudo ip link add osr-t0 type veth peer name osr-t1
sudo ip link set osr-t1 netns osr-t
sudo ip addr add 169.254.77.1/30 dev osr-t0
sudo ip -6 addr add fd77::1/64 dev osr-t0 nodad
sudo ip link set osr-t0 up
sudo ip netns exec osr-t ip addr add 169.254.77.2/30 dev osr-t1
sudo ip netns exec osr-t ip -6 addr add fd77::2/64 dev osr-t1 nodad
for ip in $ALLOW_IP $OTHER_IP $ANTH_IP; do sudo ip netns exec osr-t ip addr add "$ip/32" dev osr-t1; done
sudo ip netns exec osr-t ip -6 addr add "$V6_IP/128" dev osr-t1 nodad
sudo ip netns exec osr-t ip link set osr-t1 up
sudo ip netns exec osr-t ip link set lo up
sudo ip netns exec osr-t ip route add default via 169.254.77.1
sudo ip netns exec osr-t ip -6 route add default via fd77::1
for ip in $ALLOW_IP $OTHER_IP $ANTH_IP; do sudo ip route add "$ip/32" via 169.254.77.2 dev osr-t0; done
sudo ip -6 route add "$V6_IP/128" via fd77::2 dev osr-t0
routes_ok=1
for ip in $ALLOW_IP $OTHER_IP $ANTH_IP $V6_IP; do
  r="$(ip route get "$ip" 2>&1)"
  info "R0 route get $ip -> $r"
  echo "$r" | grep -q 'dev osr-t0' || routes_ok=0
done
if [ "$routes_ok" = 1 ]; then pass R0-test-addresses-route-only-to-veth; else fail R0-test-addresses-route-only-to-veth; echo "SUMMARY fails=$fails (aborted before traffic)"; exit 1; fi
if sudo ip netns exec osr-t ip route | grep -qv -E '^(default via 169.254.77.1|169.254.77.0/30)'; then fail "R1-netns-routes $(sudo ip netns exec osr-t ip route | tr '\n' ';')"; else pass R1-netns-has-only-veth-routes; fi

# 4. fixture CA와 인증서 (SAN api.github.com, api.anthropic.com, IP들)
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=osr-test-ca" -keyout "$WORK/ca.key" -out "$WORK/ca.pem" 2>/dev/null
sudo openssl req -newkey rsa:2048 -nodes -subj "/CN=api.github.com" -keyout "$WORK/leaf.key" -out "$WORK/leaf.csr" 2>/dev/null
printf 'subjectAltName=DNS:api.github.com,DNS:api.anthropic.com,IP:%s,IP:%s,IP:%s\n' $ALLOW_IP $OTHER_IP $V6_IP | sudo tee "$WORK/san.ext" >/dev/null
sudo openssl x509 -req -in "$WORK/leaf.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" -CAcreateserial -days 1 -extfile "$WORK/san.ext" -out "$WORK/leaf.pem" 2>/dev/null
sudo chmod 0644 "$WORK/ca.pem" "$WORK/leaf.pem"; sudo chmod 0711 "$WORK"
[ -s "$WORK/leaf.pem" ] && pass T0-fixture-ca-issued || fail T0-fixture-ca-issued
sudo bash -c "ip netns exec osr-t nohup $PY -B $FIX/mock_tls.py $WORK/leaf.pem $WORK/leaf.key \
  $ALLOW_IP:443 $ALLOW_IP:8443 $OTHER_IP:443 [$V6_IP]:443 >$WORK/tls.log 2>&1 &"
sudo ip addr add "$DNS_IP/32" dev lo

# 선행 조건이 깨지면 종속 항목은 PASS가 아니라 BLOCKED(실패로 센다)다.
blocked() { echo "BLOCKED $*"; fails=$((fails + 1)); }
EXPECTED="140.82.112.5 . 443,140.82.112.7 . 443"
elem_list() { sudo nft -j list set inet oss_research egress4 | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(",".join(sorted("%s . %s" % tuple(e["elem"]["val"]["concat"]) for x in d["nftables"] if "set" in x for e in x["set"].get("elem",[]))))'; }
# mock DNS 는 시작할 때 PID 를 받아 두고 그 PID 만 멈춘다.
DNS_PID=""
dns_start() { DNS_PID="$(sudo bash -c "nohup $PY -B $FIX/mock_dns.py $DNS_IP api.github.com=$ALLOW_IP api.anthropic.com=$ANTH_IP $* >>$WORK/dns.log 2>&1 & echo \$!")"; sleep 1; info "dns pid=$DNS_PID args=[$*]"; }
dns_stop() { [ -n "$DNS_PID" ] && sudo kill "$DNS_PID" 2>/dev/null; sleep 0.5; DNS_PID=""; }
dns_positive() { sudo "$PY" -c "
import socket,struct,sys
q=struct.pack('!HHHHHH',0x1234,0x0100,1,0,0,0)+b''.join(bytes([len(p)])+p.encode() for p in 'api.github.com'.split('.'))+b'\x00'+struct.pack('!HH',1,1)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q,('$DNS_IP',53)); r=s.recv(4096)
sys.exit(0 if socket.inet_aton('$ALLOW_IP') in r[12:] and r[:2]==q[:2] else 1)"; }
# 유효 A 질의를 보내 응답 id·답(선택적으로 기대 주소)을 확인한다. root 와 조사 uid 가 같은 payload 를 쓴다.
DNSQ='import socket,struct,sys
server,name,want=sys.argv[1],sys.argv[2],sys.argv[3]
q=struct.pack("!HHHHHH",0x4d2,0x0100,1,0,0,0)+b"".join(bytes([len(p)])+p.encode() for p in name.split("."))+b"\x00"+struct.pack("!HH",1,1)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(q,(server,53)); r=s.recv(4096)
ok=r[:2]==q[:2] and struct.unpack("!H",r[6:8])[0]>0 and (want=="-" or socket.inet_aton(want) in r[12:])
sys.exit(0 if ok else 1)'
dnsq_root() { sudo "$PY" -c "$DNSQ" "$@" 2>/dev/null; }
dnsq_uid() { "${R[@]}" timeout 5 "$PY" -c "$DNSQ" "$@" 2>/dev/null; }
reach443() { "${R[@]}" curl -sS -m 5 --cacert "$WORK/ca.pem" https://api.github.com/ >/dev/null 2>&1; }

dns_start
sleep 1
# 대조군: root(규칙 대상 아님)는 모든 fixture에 닿는다 → 서버는 살아 있다.
CTRL=1
for url in "https://$ALLOW_IP:443/" "https://$ALLOW_IP:8443/" "https://$OTHER_IP:443/" "https://[$V6_IP]:443/"; do
  if sudo curl -sS -m 5 --cacert "$WORK/ca.pem" "$url" >/dev/null 2>&1; then pass "C-root-reaches $url"; else fail "C-root-reaches $url"; CTRL=0; fi
done
DNSOK=0
if dns_positive; then pass F0-dns-fixture-answers-api.github.com; DNSOK=1; else fail F0-dns-fixture-answers-api.github.com; fi

# 5. pin 적용 (제품 도구, 제품 경로·검사 그대로)
APPLY=0
if [ "$DNSOK" = 1 ]; then
  out="$(sudo "$PY" -B "$TOOL" apply --run-id osr-t-1 --ttl 120 --resolver "$DNS_IP")"; rc=$?
  info "A1 apply rc=$rc $out"
  if [ "$rc" = 0 ] && [ "$(elem_list)" = "$EXPECTED" ]; then pass "A1-apply-and-elements [$(elem_list)]"; APPLY=1; else fail "A1-apply-and-elements rc=$rc [$(elem_list)]"; fi
  out="$(sudo "$PY" -B "$TOOL" verify --run-id osr-t-1)"; rc=$?
  info "A2 verify rc=$rc $out"
  [ "$APPLY" = 1 ] && [ "$rc" = 0 ] && pass A2-verify-live || fail A2-verify-live
else
  blocked "A1-apply-and-elements (F0 failed)"; blocked "A2-verify-live (F0 failed)"
fi
info "A3 manifest $(sudo cat /run/oss-research-egress/manifest.json 2>/dev/null | tr -d '\n' | cut -c1-600)"
info "A3 perms $(sudo stat -c '%a %U %n' /run/oss-research-egress /run/oss-research-egress/manifest.json 2>&1 | tr '\n' ';')"
info "A3 hosts $(grep -A4 'BEGIN oss-research-egress-pin' /etc/hosts | tr '\n' ';')"
info "A3 elems $(elems)"

# 6. 조사 uid 트래픽 (srt 밖, uid 층만). 거부 시험은 양성(T1)과 root 대조군이 선 뒤에만 의미가 있다.
POS=0
if [ "$APPLY" = 1 ]; then
  c0="$(counter)"
  if reach443; then pass T1-pinned-name-443-allowed; POS=1; else fail T1-pinned-name-443-allowed; fi
  c1="$(counter)"
  [ "$POS" = 1 ] && [ "${c1:-0}" -gt "${c0:-0}" ] && pass "T1b-allowed-by-egress4-rule (counter $c0->$c1, not lo)" || fail "T1b-allowed-by-egress4-rule ($c0->$c1)"
else
  blocked "T1-pinned-name-443-allowed (no applied pin)"; blocked "T1b-allowed-by-egress4-rule (no applied pin)"
fi
if [ "$POS" = 1 ] && [ "$CTRL" = 1 ]; then
  if "${R[@]}" curl -sS -m 5 https://api.github.com/ >/dev/null 2>&1; then fail T2-system-ca-rejects-fixture-cert; else pass T2-system-ca-rejects-fixture-cert; fi
  if "${R[@]}" curl -sS -m 5 --cacert "$WORK/ca.pem" "https://$ALLOW_IP:8443/" >/dev/null 2>&1; then fail T3-other-port-denied; else pass T3-other-port-denied; fi
  if "${R[@]}" curl -sS -m 5 --cacert "$WORK/ca.pem" "https://$OTHER_IP:443/" >/dev/null 2>&1; then fail T4-other-ip-denied; else pass T4-other-ip-denied; fi
  if "${R[@]}" curl -sS -m 5 --cacert "$WORK/ca.pem" "https://[$V6_IP]:443/" >/dev/null 2>&1; then fail T5-ipv6-denied; else pass T5-ipv6-denied; fi
else
  for t in T2-system-ca-rejects-fixture-cert T3-other-port-denied T4-other-ip-denied T5-ipv6-denied; do blocked "$t (T1 or root controls failed)"; done
fi
# T6: 같은 유효 질의로 root 는 mock DNS 에서 답을 받고 조사 uid 는 거부된다.
if dnsq_root "$DNS_IP" api.github.com "$ALLOW_IP"; then
  pass T6a-root-gets-mock-dns-answer
  if dnsq_uid "$DNS_IP" api.github.com "$ALLOW_IP"; then fail T6-uid-same-dns-query-denied; else pass T6-uid-same-dns-query-denied; fi
else
  fail T6a-root-gets-mock-dns-answer; blocked "T6-uid-same-dns-query-denied (no root positive)"
fi
# T7: resolved stub(127.0.0.53:53)은 대조군을 세울 수 없다. provision 의 `meta skuid "systemd-resolve" reject`
#     가 resolved 소켓의 응답(SYN-ACK·UDP 답)까지 막아 root 연결도 timeout 된다(5차 실행 실측,
#     resolved-stub-probe-2.log). 그래서 판정에 쓰지 않고 N/A 로 기록한다. 조사 uid 의 53 거부 자체는
#     T6(같은 유효 질의, root 답·uid 거부)가 같은 nft 53 규칙으로 보인다.
tcp53() { "$@" timeout 5 "$PY" -c 'import socket; socket.create_connection(("127.0.0.53",53),3).close()' 2>/dev/null; }
tcp53 sudo; r_root=$?
tcp53 "${R[@]}"; r_uid=$?
echo "NA T7-resolved-stub-control-unavailable (root tcp53 rc=$r_root, uid tcp53 rc=$r_uid; stub unusable for every uid under the resolved egress block)"
dnsq_root 127.0.0.53 localhost 127.0.0.1; info "T7obs root UDP A localhost via stub rc=$? (observation only)"
# 관측만(판정에 쓰지 않는다): NXDOMAIN 만으로도 실패하므로 경로 차단 증거가 아니다.
"${R[@]}" timeout 20 getent ahostsv4 example.invalid >/dev/null 2>&1; info "T7obs getent example.invalid as uid rc=$? (observation only)"
info "T8 osr_test drops (should stay 0): $(sudo nft list table inet osr_test | grep -o 'packets [0-9]*' | tr '\n' ' ')"

# 7. 다른 run 충돌과 clear 보호 (활성 pin osr-t-1 이 있어야 의미가 있다)
if [ "$APPLY" = 1 ]; then
  out="$(sudo "$PY" -B "$TOOL" apply --run-id osr-t-2 --ttl 120 --resolver "$DNS_IP")"; rc=$?
  info "X1 $out"
  [ "$rc" = 2 ] && echo "$out" | grep -q 'holds an active pin' && pass X1-other-run-refused || fail X1-other-run-refused
  out="$(sudo "$PY" -B "$TOOL" clear --run-id osr-t-2)"; rc=$?
  [ "$rc" = 2 ] && pass X2-clear-other-run-refused || fail "X2-clear-other-run-refused rc=$rc $out"
  sudo "$PY" -B "$TOOL" verify --run-id osr-t-1 >/dev/null && [ "$(elem_list)" = "$EXPECTED" ] && pass X3-original-pin-intact || fail X3-original-pin-intact
else
  for t in X1-other-run-refused X2-clear-other-run-refused X3-original-pin-intact; do blocked "$t (no active pin)"; done
fi

# 8. apply 중 SIGKILL. 선행: 활성 pin(osr-t-1)과 허용 443이 있어야 한다. DNS 응답을 늦춰 resolve 단계에 붙잡고,
#    이 시험이 시작한 apply 프로세스의 정확한 PID 만 kill -9 한다(패턴 kill 없음). 그 PID 종료를 확인한다.
S_READY=0
if [ "$APPLY" = 1 ] && sudo "$PY" -B "$TOOL" verify --run-id osr-t-1 >/dev/null && [ "$(elem_list)" = "$EXPECTED" ] && reach443; then
  pass "S0-precondition-active-pin-and-443 [$(elem_list)]"; S_READY=1
else
  blocked "S0-precondition-active-pin-and-443"
fi
if [ "$S_READY" = 1 ]; then
  dns_stop
  dns_start --delay api.anthropic.com=6
  lines_before="$(sudo wc -l < $WORK/dns.log)"
  APID="$(sudo bash -c "$PY -B $TOOL apply --run-id osr-t-1 --ttl 120 --resolver $DNS_IP >$WORK/apply-killed.out 2>&1 & echo \$!")"
  waited=0
  until sudo tail -n +"$((lines_before + 1))" $WORK/dns.log | grep -q 'answered api.github.com' || [ $waited -ge 50 ]; do sleep 0.1; waited=$((waited + 1)); done
  sleep 0.3
  cmd="$(sudo cat /proc/$APID/cmdline 2>/dev/null | tr '\0' ' ')"
  state="$(sudo awk '{print $3}' /proc/$APID/stat 2>/dev/null)"
  mid="$(elem_list)"
  info "S1 target pid=$APID state=$state cmd=[$cmd] elements-before-kill=[$mid] dns-log-after-start=[$(sudo tail -n +$((lines_before + 1)) $WORK/dns.log | tr '\n' ';')]"
  if echo "$cmd" | grep -q -- "$TOOL apply --run-id osr-t-1" && [ "$mid" = "" ]; then
    pass "S1a-target-is-this-apply-in-resolve-and-allow-already-closed (pid $APID)"
    sudo kill -9 "$APID"
    gone=0; for i in $(seq 1 30); do sudo test -e /proc/$APID || { gone=1; break; }; sleep 0.1; done
    info "S1 after kill: /proc/$APID exists=$((1 - gone)) apply-output=[$(sudo cat $WORK/apply-killed.out | tr '\n' ';')]"
    if [ "$gone" = 1 ] && [ "$(elem_list)" = "" ] && ! sudo test -e /run/oss-research-egress/manifest.json; then
      pass S1-sigkill-of-exact-apply-pid-leaves-closed
      if reach443; then fail S2-no-egress-after-kill; else pass S2-no-egress-after-kill; fi
      sudo "$PY" -B "$TOOL" verify >/dev/null; [ $? = 3 ] && pass S3-verify-refuses-after-kill || fail S3-verify-refuses-after-kill
    else
      fail "S1-sigkill-of-exact-apply-pid-leaves-closed gone=$gone [$(elem_list)]"; blocked "S2-no-egress-after-kill"; blocked "S3-verify-refuses-after-kill"
    fi
  else
    fail "S1a-target-is-this-apply-in-resolve-and-allow-already-closed pid=$APID cmd=[$cmd] mid=[$mid]"
    [ -n "$APID" ] && sudo kill -9 "$APID" 2>/dev/null
    blocked S1-sigkill-of-exact-apply-pid-leaves-closed; blocked S2-no-egress-after-kill; blocked S3-verify-refuses-after-kill
  fi
  dns_stop
  dns_start
else
  for t in S1a S1-sigkill-of-exact-apply-pid-leaves-closed S2-no-egress-after-kill S3-verify-refuses-after-kill; do blocked "$t (S0 failed)"; done
fi
sudo "$PY" -B "$TOOL" clear >/dev/null

# 9. 잔여 lease: 60초 pin 을 정리하지 않고 두면 kernel 이 지운다(프로세스 없음).
#    시작 때 원소와 양성 443 이 있고, +30초에도 원소가 남아 있어야만 만료 PASS 를 인정한다(빈 집합을 만료로 세지 않는다).
out="$(sudo "$PY" -B "$TOOL" apply --run-id osr-t-3 --ttl 60 --resolver "$DNS_IP")"; rc=$?
L_READY=0
if [ "$rc" = 0 ] && [ "$(elem_list)" = "$EXPECTED" ] && reach443; then pass "L0-apply-ttl60-elements-and-443 [$(elem_list)]"; L_READY=1; else fail "L0-apply-ttl60-elements-and-443 rc=$rc $out [$(elem_list)]"; fi
info "L1 elems at start $(elems)"
if [ "$L_READY" = 1 ]; then
  sleep 30
  info "L1 elems at +30s $(elems)"
  if [ "$(elem_list)" = "$EXPECTED" ] && sudo "$PY" -B "$TOOL" verify --run-id osr-t-3 >/dev/null; then pass L1-elements-and-verify-midway; L1=1; else fail L1-elements-and-verify-midway; L1=0; fi
  sleep 33
  if [ "$L1" = 1 ]; then
    if [ "$(elem_list)" = "" ]; then pass L2-lease-expired-without-cleanup; else fail "L2-lease-expired-without-cleanup [$(elem_list)]"; fi
    if reach443; then fail L3-no-egress-after-expiry; else pass L3-no-egress-after-expiry; fi
    out="$(sudo "$PY" -B "$TOOL" verify --run-id osr-t-3)"; [ $? = 3 ] && pass "L4-verify-refuses-expired" || fail "L4-verify-refuses-expired $out"
  else
    for t in L2-lease-expired-without-cleanup L3-no-egress-after-expiry L4-verify-refuses-expired; do blocked "$t (L1 failed)"; done
  fi
else
  for t in L1-elements-and-verify-midway L2-lease-expired-without-cleanup L3-no-egress-after-expiry L4-verify-refuses-expired; do blocked "$t (L0 failed)"; done
fi

# 10. clear: 원소·양성 443 이 있는 상태에서 닫아야 의미가 있다.
out="$(sudo "$PY" -B "$TOOL" apply --run-id osr-t-4 --ttl 120 --resolver "$DNS_IP")"; rc=$?
if [ "$rc" = 0 ] && [ "$(elem_list)" = "$EXPECTED" ] && reach443; then
  pass Z0-precondition-pin-and-443
  out="$(sudo "$PY" -B "$TOOL" clear --run-id osr-t-4)"; rc=$?
  [ "$rc" = 0 ] && [ "$(elem_list)" = "" ] && ! grep -q 'BEGIN oss-research-egress-pin' /etc/hosts && ! sudo test -e /run/oss-research-egress/manifest.json \
    && pass Z1-clear-closes-everything || fail "Z1-clear-closes-everything rc=$rc $out"
  if reach443; then fail Z2-no-egress-after-clear; else pass Z2-no-egress-after-clear; fi
else
  fail "Z0-precondition-pin-and-443 rc=$rc"; blocked Z1-clear-closes-everything; blocked Z2-no-egress-after-clear
fi

dns_stop
info "logs: $(sudo tail -n 4 $WORK/dns.log $WORK/tls.log 2>/dev/null | tr '\n' ';' | cut -c1-700)"
skip "F1-real-nft-add-failure: 게스트 규칙을 바꾸지 않고는 nft add 실패를 만들 수 없다(오프라인 주입 시험으로 대신)"
echo "SUMMARY fails=${fails}"
[ "$fails" = 0 ]
