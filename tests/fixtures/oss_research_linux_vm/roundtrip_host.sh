#!/bin/bash
# #78 Linux VM 입력 전달·결과 회수 왕복과 거부 fixture (호스트에서 실행).
#
#   roundtrip_host.sh <인스턴스>
#
# 전제: $W=$HOME/.oss-research-vm 에 Lima 가 설치돼 있고 인스턴스가 실행 중이다.
# 결과는 $W/xfer/out/<인스턴스>-<case>/ 에 safe_tar.py 로만 푼다. FAIL 이 있으면 exit 1.
set -u
INST="${1:?instance}"
W="$HOME/.oss-research-vm"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/opt/homebrew/bin/python3
L() { env -i HOME="$W/home" LIMA_HOME="$W/lima" TMPDIR="$W/tmp" PATH="$W/tool/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$W/tool/bin/limactl" "$@"; }
G() { L shell --workdir / "$INST" -- "$@"; }
RS='sudo -u ossresearch -H env -i HOME=/home/ossresearch PATH=/usr/bin:/bin'
CAP=67108864
fails=0
ok() { echo "PASS $*"; }
ng() { echo "FAIL $*"; fails=$((fails + 1)); }

run_dir="$W/xfer/in/$INST"
rm -rf "$run_dir"; mkdir -m 0700 -p "$run_dir"
printf '{"repo":"fixture/none","question":"non-AI roundtrip"}\n' >"$run_dir/input.json"
want="$(shasum -a 256 "$run_dir/input.json" | awk '{print $1}')"

# 입력: macOS 메타데이터(AppleDouble ._*, xattr) 없이 파일 목록만 보낸다. in/ 디렉터리 권한은 덮지 않는다.
G sudo find /srv/oss-research/in /srv/oss-research/out -mindepth 1 -delete
(cd "$run_dir" && COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --no-acls -cf - input.json) |
  G sudo tar -C /srv/oss-research/in -xf - --no-same-owner --no-same-permissions --no-overwrite-dir
got="$(G $RS sha256sum /srv/oss-research/in/input.json | awk '{print $1}')"
[ "$got" = "$want" ] && ok "I1-input-readable-by-ossresearch" || ng "I1-input-readable-by-ossresearch got=$got"
[ "$(G sudo ls -A /srv/oss-research/in)" = "input.json" ] && ok "I2-no-apple-metadata" || ng "I2-no-apple-metadata"
[ "$(G stat -c '%a %U:%G' /srv/oss-research/in)" = "750 root:ossresearch" ] && ok "I3-in-dir-mode-kept" || ng "I3-in-dir-mode-kept"

collect() { # $1=case  $2=cap  -> safe_tar rc
  local out="$W/xfer/out/$INST-$1"
  rm -rf "$out" "$out.tar"; mkdir -m 0700 "$out"
  G sudo tar -C /srv/oss-research/out -cf - . | head -c "$2" >"$out.tar"
  local size; size="$(stat -f %z "$out.tar")"
  if [ "$size" -ge "$2" ]; then echo "REJECT: 상한 도달(잘림으로 간주) size=$size"; rm -rf "$out"; return 2; fi
  "$PY" "$HERE/safe_tar.py" result "$out" "$out.tar"; local rc=$?
  [ $rc -ne 0 ] && rm -rf "$out"
  return $rc
}

G $RS bash -c 'set -e; cd /srv/oss-research/out; mkdir report; sha256sum /srv/oss-research/in/input.json > manifest.txt; echo "# non-AI result" > report/report.md'
if collect good "$CAP" && grep -q "$want" "$W/xfer/out/$INST-good/manifest.txt"; then ok R1-good-roundtrip; else ng R1-good-roundtrip; fi

G sudo find /srv/oss-research/out -mindepth 1 -delete
G $RS bash -c 'cd /srv/oss-research/out && echo ok > a.txt && ln -s /etc/shadow leak'
collect symlink "$CAP"; [ $? -eq 2 ] && ok R2-symlink-rejected || ng R2-symlink-rejected
G sudo find /srv/oss-research/out -mindepth 1 -delete
G $RS bash -c 'cd /srv/oss-research/out && echo ok > a.txt && ln a.txt hard'
collect hardlink "$CAP"; [ $? -eq 2 ] && ok R3-hardlink-rejected || ng R3-hardlink-rejected
G sudo find /srv/oss-research/out -mindepth 1 -delete
G $RS bash -c 'cd /srv/oss-research/out && mkfifo pipe'
collect fifo "$CAP"; [ $? -eq 2 ] && ok R4-fifo-rejected || ng R4-fifo-rejected
G sudo find /srv/oss-research/out -mindepth 1 -delete
G $RS bash -c 'head -c 200000 /dev/zero > /srv/oss-research/out/big.bin'
collect capped 65536; [ $? -eq 2 ] && ok R5-over-cap-rejected || ng R5-over-cap-rejected
G sudo find /srv/oss-research/in /srv/oss-research/out -mindepth 1 -delete

echo "SUMMARY fails=${fails}"
[ "$fails" = 0 ]
