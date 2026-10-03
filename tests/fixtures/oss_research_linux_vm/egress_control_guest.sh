#!/bin/bash
# #78 egress allow set 대조군. 게스트 안에서 관리 계정(vmadmin)으로 실행한다.
#
#   limactl shell --workdir / <인스턴스> -- bash -s -- <호스트 fixture 포트> < egress_control_guest.sh
#
# 대상은 호스트 127.0.0.1 에만 bind 한 임시 fixture(게스트에서 192.168.5.2)뿐이다. 실제 공개 endpoint 는 없다.
# 제품 pin 도구는 192.168.5.0/24 를 거부하므로, 여기서는 root 가 시험 전용으로 집합 원소를 직접 넣어
# "집합에 있으면 통과, 없으면 거부"라는 nft 층의 동작만 대조한다. 끝나면 집합을 비운다.
set -u
PORT="${1:?host fixture port}"
R=(sudo -u ossresearch -H env -i HOME=/home/ossresearch PATH=/usr/bin:/bin)
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
cleanup() { sudo nft flush set inet oss_research egress4; }
trap cleanup EXIT
reach() { "${R[@]}" curl -sS -m 5 -o /dev/null "http://192.168.5.2:${PORT}/$1"; }

cleanup
if reach X1-before; then fail X1-denied-without-element; else pass X1-denied-without-element; fi
sudo nft add element inet oss_research egress4 "{ 192.168.5.2 . ${PORT} timeout 30s }"
if reach X2-with-element; then pass X2-allowed-with-element; else fail X2-allowed-with-element; fi
if "${R[@]}" timeout 5 bash -c 'exec 3<>/dev/tcp/192.168.5.2/53' 2>/dev/null; then fail X3-dns-still-denied; else pass X3-dns-still-denied; fi
# 같은 주소의 다른 포트 거부는 대조군(열린 두 번째 포트)이 있어야 의미가 있어 여기서는 주장하지 않는다.
cleanup
if reach X5-after-flush; then fail X5-denied-after-flush; else pass X5-denied-after-flush; fi
echo "SUMMARY fails=${fails}"
[ "$fails" = 0 ]
