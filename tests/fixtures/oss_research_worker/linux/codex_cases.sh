#!/bin/bash
# #6 2단계 S3: Codex 비AI 사례(게스트, 관리 계정 vmadmin이 실행, 안에서 sudo). 모델 호출·자격 없음.
#
#   bash codex_cases.sh install TARBALL     # root가 sha 고정 Codex를 /opt/oss-research/codex-0.162.1/codex 로 설치
#   bash codex_cases.sh cases COMMIT        # 배포된 src-COMMIT 의 unit_inner.py codex-probe 사례 전부
#
# install: TARBALL(호스트가 받은 릴리스 자산 codex-aarch64-unknown-linux-musl.tar.gz)의 sha256이 릴리스
# digest와 같고, 안의 단일 파일 codex-aarch64-unknown-linux-musl의 sha256이 고정값과 같을 때만 root 소유
# 0755로 설치한다. 이미 있으면 sha만 대조한다. 어느 단계든 다르면 아무것도 바꾸지 않고 exit 1.
#
# cases: 모두 wrapper --entry verify(조사 사용자 ossresearch, unit 안). 사례:
#   C1 version, C2 help(exec --help), C3 prompt-input(제품 설정으로 모델이 받을 입력: 스킬·multi-agent·
#   AGENTS.md canary 없음), C4 login-status(빈 CODEX_HOME: 로그인 없음), C5 escape(Codex 정책 안의 bash가
#   범위 밖 경로를 읽거나 나열하지 못함, CA 파일만 읽힘), C6 ablation(읽기 집합 하나씩 빼고 version·
#   prompt-input) — 이상 --egress none. C7 doctor(--egress pin --backend codex: Codex 자신의 HTTP 스택이
#   인증 없이 chatgpt.com/backend-api/codex/responses에 HEAD 1회. CA 있음/없음 둘) — 모델 호출 아님.
set -u
CODEX_DIR=/opt/oss-research/codex-0.162.1
TAR_SHA=bf3ca2719f1143b1440fbd85c149379e696c782c50ad7991f8bfcec60b2600c1
BIN_SHA=071cbe8d821cf1b02b6d8ba6c8b7991b228fcb8519dcb68a37d31222bb8cabe5
NODE=/opt/oss-research/node-v24.20.0-linux-arm64/bin
PREFIX=/opt/oss-research/srt-0.0.77
RESOLVER=192.168.5.2

install_codex() {
  local tarball=$1 tmp got
  got=$(sha256sum "$tarball" | cut -d' ' -f1)
  [ "$got" = "$TAR_SHA" ] || { echo "FAIL tarball sha256 $got"; return 1; }
  echo "PASS tarball sha256 $got"
  if sudo test -e "$CODEX_DIR/codex"; then
    got=$(sudo sha256sum "$CODEX_DIR/codex" | cut -d' ' -f1)
    [ "$got" = "$BIN_SHA" ] && { echo "PASS already installed $got"; return 0; }
    echo "FAIL installed codex differs: $got"; return 1
  fi
  tmp=$(sudo mktemp -d /root/osr-codex-XXXX)
  sudo tar -x -z -C "$tmp" --no-same-owner --no-same-permissions -f "$tarball" codex-aarch64-unknown-linux-musl \
    || { sudo rm -rf "$tmp"; echo "FAIL extract"; return 1; }
  got=$(sudo sha256sum "$tmp/codex-aarch64-unknown-linux-musl" | cut -d' ' -f1)
  if [ "$got" != "$BIN_SHA" ]; then sudo rm -rf "$tmp"; echo "FAIL binary sha256 $got"; return 1; fi
  sudo install -d -o root -g root -m 0755 "$CODEX_DIR"
  sudo install -o root -g root -m 0755 "$tmp/codex-aarch64-unknown-linux-musl" "$CODEX_DIR/codex"
  sudo rm -rf "$tmp"
  echo "PASS installed $(sudo sha256sum "$CODEX_DIR/codex" | cut -d' ' -f1) $(stat -c '%U:%G %a' "$CODEX_DIR" "$CODEX_DIR/codex" | tr '\n' ' ')"
  echo "INFO file $(file -b "$CODEX_DIR/codex" | cut -c1-80)"
}

run_cases() {
  local c=$1 s=/opt/oss-research/src-$1 o case
  local wrap=(sudo env -i PATH=$NODE:$CODEX_DIR:/usr/bin:/bin LANG=C.UTF-8 OSS_RESEARCH_SRT_PREFIX=$PREFIX
              OSS_RESEARCH_EGRESS_RESOLVER=$RESOLVER PYTHONDONTWRITEBYTECODE=1 python3 -I -B $s/bin/oss-research-linux-run)
  for case in version help prompt-input login-status escape ablation doctor; do
    o=$(sudo mktemp -d /tmp/osr-c-XXXX); sudo chown ossresearch:ossresearch "$o"
    if [ "$case" = doctor ]; then
      echo "== $case (--egress pin --backend codex)"
      "${wrap[@]}" --entry verify --egress pin --backend codex --timeout 240 -- codex-probe "$o" "$case"
    else
      echo "== $case (--egress none)"
      "${wrap[@]}" --entry verify --egress none --timeout 240 -- codex-probe "$o" "$case"
    fi
    echo "rc=$?"
    sudo rm -rf "$o"
  done
  echo "== after tmp_osr_c=$(ls -d /tmp/osr-c-* 2>/dev/null | wc -l)"
}

case "${1:-}" in
  install) install_codex "${2:?tarball}";;
  cases) run_cases "${2:?commit}";;
  *) echo "usage: codex_cases.sh install TARBALL | cases COMMIT"; exit 2;;
esac
