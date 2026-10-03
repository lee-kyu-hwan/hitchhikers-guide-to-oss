#!/bin/bash
# #78 Linux VM 비AI preflight. 게스트 안에서 관리 계정(vmadmin)으로 실행한다.
#
#   limactl shell --workdir / <인스턴스> -- bash -s -- <호스트 fixture 포트> <lima 키 지문> < preflight_guest.sh
#
# 호스트 fixture: 호스트 127.0.0.1 에만 bind 한 임시 HTTP 서버(게스트에서는 192.168.5.2).
# 실제 호스트 서비스(Orca·Claude·redis 등)나 외부 서비스에는 요청하지 않는다.
# 외부 주소 시험은 TEST-NET-3(203.0.113.10)이며 게스트 nft 가 로컬에서 거부한다.
# 결과 줄: "PASS <id> ..." / "FAIL <id> ..." / "INFO <id> ...". FAIL 이 하나라도 있으면 exit 1.

set -u
PORT="${1:?host fixture port}"
LIMA_FP="${2:?lima key fingerprint}"
R=(sudo -u ossresearch -H env -i HOME=/home/ossresearch PATH=/usr/bin:/bin)
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
info() { echo "INFO $*"; }
expect_ok() { local id="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$id"; else fail "$id (rc=$?)"; fi; }
expect_fail() { local id="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$id (예상과 달리 성공)"; else pass "$id"; fi; }
counters() { sudo nft list table inet oss_research | grep -o 'packets [0-9]*' | awk '{s=s $2 ","} END{print s}'; }

info os "$(. /etc/os-release; echo "$PRETTY_NAME") kernel=$(uname -r) arch=$(uname -m)"
info build "$(cat /etc/cloud/build.info 2>/dev/null | tr '\n' ' ')"
info packages "$(dpkg-query -W -f='${Package}=${Version} ' nftables bubblewrap socat ripgrep ca-certificates openssh-server systemd apparmor 2>/dev/null)"
info resources "cpus=$(nproc) mem_mb=$(free -m | awk '/Mem:/{print $2}') root=$(df -h / | awk 'NR==2{print $2}')"

# 마운트·공유
if findmnt -rn -t virtiofs,9p,fuse.sshfs,fuse,nfs,nfs4,cifs >/dev/null; then fail M1-no-host-mounts; else pass M1-no-host-mounts; fi
info M2-mounts "$(findmnt -rn -o TARGET,FSTYPE -t ext4,vfat,iso9660,btrfs,xfs | tr '\n' ';')"
expect_fail M3-ossresearch-cidata "${R[@]}" ls /mnt/lima-cidata

# SSH 키
fps="$(ssh-keygen -lf /home/vmadmin/.ssh/authorized_keys 2>/dev/null | awk '{print $2}' | sort -u)"
if [ "$fps" = "$LIMA_FP" ]; then pass K1-authorized-keys-only-lima; else fail "K1-authorized-keys-only-lima got=[$fps]"; fi
expect_fail K2-ossresearch-no-ssh-dir sudo test -e /home/ossresearch/.ssh
if sudo test -s /root/.ssh/authorized_keys; then fail K3-root-no-keys; else pass K3-root-no-keys; fi

# 환경·socket
if [ -z "${SSH_AUTH_SOCK:-}" ]; then pass E1-no-ssh-auth-sock; else fail E1-no-ssh-auth-sock; fi
n="$(sudo grep -l -a SSH_AUTH_SOCK /proc/[0-9]*/environ 2>/dev/null | wc -l)"
[ "$n" = 0 ] && pass E2-no-process-ssh-auth-sock || fail "E2-no-process-ssh-auth-sock n=$n"
envnames="$("${R[@]}" env | cut -d= -f1 | sort | tr '\n' ' ')"
[ "$envnames" = "HOME PATH " ] && pass E3-ossresearch-env-minimal || fail "E3-ossresearch-env-minimal [$envnames]"
if grep -qiE 'proxy|token|secret|anthropic|github' /etc/environment; then fail E4-etc-environment; else pass E4-etc-environment; fi
expect_fail S1-no-host-services test -e /run/host-services
if systemctl list-unit-files 2>/dev/null | grep -qi lima-guestagent; then fail S2-no-guest-agent; else pass S2-no-guest-agent; fi
info S3-listening-unix "$(sudo ss -xlnH | awk '{print $5}' | sort -u | tr '\n' ' ')"
info S4-world-writable-sockets "$(sudo find /run /var/run -xdev -type s -perm -0002 2>/dev/null | sort -u | tr '\n' ' ')"

# 계정·권한
[ "$(id -Gn ossresearch)" = "ossresearch" ] && pass U1-ossresearch-groups || fail "U1-ossresearch-groups $(id -Gn ossresearch)"
expect_fail U2-ossresearch-no-sudo "${R[@]}" sudo -n true
expect_fail U3-ossresearch-no-vmadmin-home "${R[@]}" ls /home/vmadmin
sudo passwd -S ossresearch | awk '{print $2}' | grep -qx L && pass U4-ossresearch-locked || fail U4-ossresearch-locked
expect_fail U5-ossresearch-no-write-in "${R[@]}" touch /srv/oss-research/in/.probe
expect_ok U6-ossresearch-write-out "${R[@]}" bash -c 'touch /srv/oss-research/out/.probe && rm /srv/oss-research/out/.probe'
expect_fail U7-ossresearch-no-nft-change "${R[@]}" /usr/sbin/nft flush ruleset
info U8-vmadmin-groups "$(id -Gn vmadmin)"

# 네트워크 (조사 uid 는 lo 외 전부 거부, DNS 는 lo 포함 거부)
before="$(counters)"
expect_fail N1-ossresearch-host-loopback "${R[@]}" curl -sS -m 5 "http://192.168.5.2:${PORT}/N1-ossresearch"
expect_fail N2-ossresearch-host-name "${R[@]}" curl -sS -m 5 "http://host.lima.internal:${PORT}/N2-ossresearch"
expect_fail N3-ossresearch-external "${R[@]}" timeout 5 bash -c 'exec 3<>/dev/tcp/203.0.113.10/443'
expect_fail N4-ossresearch-gateway-dns "${R[@]}" timeout 5 bash -c 'exec 3<>/dev/tcp/192.168.5.2/53'
expect_fail N5-ossresearch-local-dns-stub "${R[@]}" timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.53/53'
expect_fail N6-ossresearch-resolve-name "${R[@]}" timeout 20 getent ahostsv4 host.lima.internal
if command -v resolvectl >/dev/null; then
  expect_fail N7-ossresearch-resolvectl "${R[@]}" timeout 20 resolvectl query host.lima.internal
else
  info N7-ossresearch-resolvectl "resolvectl 없음"
fi
expect_ok N8-ossresearch-loopback-allowed "${R[@]}" timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/22'
after="$(counters)"
info N9-nft-counters "before=${before} after=${after}"
[ "$before" != "$after" ] && pass N9-nft-counter-increased || fail N9-nft-counter-increased
expect_ok C1-vmadmin-control-host-loopback curl -sS -m 5 "http://192.168.5.2:${PORT}/C1-vmadmin-control"
elems="$(sudo nft -j list set inet oss_research egress4 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(x["set"].get("elem",[])) for x in d["nftables"] if "set" in x))' 2>/dev/null)"
[ "$elems" = "0" ] && pass N10-egress-set-exists-and-empty || fail "N10-egress-set-exists-and-empty [$elems]"
if grep -q '^# BEGIN oss-research-egress-pin' /etc/hosts; then fail N11-no-stale-hosts-pin; else pass N11-no-stale-hosts-pin; fi
info R1-resolv "$(grep -v '^#' /etc/resolv.conf | tr '\n' ' ')"
if [ -S /run/snapd.socket ] && "${R[@]}" curl -s -m 3 --unix-socket /run/snapd.socket http://localhost/v2/system-info >/dev/null 2>&1; then
  fail P1-no-snapd-api
else
  pass P1-no-snapd-api
fi

# namespace·cgroup
bwrap_ok=0
if out="$("${R[@]}" bwrap --unshare-all --die-with-parent --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp true 2>&1)"; then
  pass B1-bwrap-unshare-all
  bwrap_ok=1
else
  fail "B1-bwrap-unshare-all: ${out}"
fi
# B1 이 실패하면 bwrap 자체가 뜨지 않으므로 B2 는 의미가 없다(거짓 PASS 방지).
if [ "$bwrap_ok" = 0 ]; then
  info B2-bwrap-netns-isolated "건너뜀(B1 실패)"
elif out="$("${R[@]}" bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev bash -c 'ip -o link | wc -l; exec 3<>/dev/tcp/127.0.0.1/22' 2>&1)"; then
  fail "B2-bwrap-netns-isolated (sandbox 안에서 호스트 lo 에 닿음): ${out}"
else
  pass B2-bwrap-netns-isolated
fi
rd() { cat "/proc/sys/$1" 2>/dev/null || echo n/a; }
info B3-sysctl "apparmor_restrict_unprivileged_userns=$(rd kernel/apparmor_restrict_unprivileged_userns) unprivileged_userns_clone=$(rd kernel/unprivileged_userns_clone) max_user_namespaces=$(rd user/max_user_namespaces)"
[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] && pass G1-cgroup-v2 || fail G1-cgroup-v2
info G2-controllers "$(cat /sys/fs/cgroup/cgroup.subtree_control)"
lim="$(sudo systemd-run --quiet --pipe --wait --collect -p MemoryMax=64M -p TasksMax=16 --uid=ossresearch /usr/bin/bash -c 'c=$(cut -d: -f3 /proc/self/cgroup); echo "$(id -u) $(cat /sys/fs/cgroup$c/memory.max) $(cat /sys/fs/cgroup$c/pids.max)"' 2>&1)"
[ "$lim" = "2000 67108864 16" ] && pass "G3-cgroup-limits $lim" || fail "G3-cgroup-limits [$lim]"
expect_fail G4-runtime-max sudo systemd-run --quiet --pipe --wait --collect -p RuntimeMaxSec=2 --uid=ossresearch /usr/bin/sleep 30

echo "SUMMARY fails=${fails}"
[ "$fails" = 0 ]
