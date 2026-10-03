#!/usr/bin/env python3
"""#78 Linux VM 작업용 tar 검사·추출기 (호스트 쪽, 검토용 fixture).

두 모드가 있다. 어느 모드든 위반이 하나라도 있으면 아무것도 쓰지 않고 exit 2로 끝난다.

  install  DEST TARBALL
      도구 tarball(Lima)을 DEST에 푼다. regular file·directory와,
      DEST 안을 가리키는 상대 symlink만 허용한다. hardlink·장치·FIFO,
      절대 경로, "..", 허용 최상위(bin, libexec, share) 밖 항목은 거부한다.

  result   DEST TARFILE [--max-members N] [--max-bytes N] [--max-file-bytes N]
      게스트에서 회수한 결과 tar를 비어 있지 않으면 안 되는 새 DEST에 푼다.
      regular file·directory만 허용한다(symlink·hardlink·장치·FIFO 전부 거부).
      항목 수, 합산 크기, 파일 하나 크기, 경로 길이·깊이, 중복·대소문자 충돌,
      제어 문자를 제한한다. tarfile의 extract 계열은 쓰지 않고 직접 쓴다
      (O_CREAT|O_EXCL|O_NOFOLLOW, 권한 0600/0700).
"""

from __future__ import annotations

import argparse
import os
import posixpath
import stat
import sys
import tarfile

INSTALL_TOP = {"bin", "libexec", "share"}
MAX_PATH_BYTES = 512
MAX_DEPTH = 16


class Reject(Exception):
    pass


def _norm(name: str) -> str:
    if not name or "\x00" in name:
        raise Reject(f"빈 이름 또는 NUL: {name!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise Reject(f"제어 문자: {name!r}")
    if name.startswith("/"):
        raise Reject(f"절대 경로: {name!r}")
    stripped = name[2:] if name.startswith("./") else name
    stripped = stripped.rstrip("/")
    if stripped in ("", "."):
        return "."
    parts = stripped.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Reject(f"비정규 경로 성분: {name!r}")
    if len(stripped.encode()) > MAX_PATH_BYTES or len(parts) > MAX_DEPTH:
        raise Reject(f"경로가 너무 길거나 깊음: {name!r}")
    return stripped


def _open(path: str) -> tarfile.TarFile:
    # 압축 여부는 자동 판별. 스트림이 잘렸으면 여기나 순회에서 예외가 난다.
    return tarfile.open(path, mode="r:*")


def _members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    try:
        return tf.getmembers()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise Reject(f"tar 읽기 실패(잘림·손상): {exc}") from exc


def check_install(members: list[tarfile.TarInfo]) -> list[tuple[str, tarfile.TarInfo]]:
    out = []
    seen = set()
    for m in members:
        rel = _norm(m.name)
        if rel == ".":
            if not m.isdir():
                raise Reject("루트 항목이 디렉터리가 아님")
            continue
        if rel.split("/")[0] not in INSTALL_TOP:
            raise Reject(f"허용 최상위 밖: {m.name!r}")
        if rel in seen:
            raise Reject(f"중복 항목: {m.name!r}")
        seen.add(rel)
        if m.issym():
            target = m.linkname
            if not target or target.startswith("/"):
                raise Reject(f"절대·빈 symlink: {m.name!r} -> {target!r}")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
            if resolved == ".." or resolved.startswith("../"):
                raise Reject(f"DEST 밖 symlink: {m.name!r} -> {target!r}")
        elif not (m.isfile() or m.isdir()):
            raise Reject(f"허용하지 않는 형식({m.type!r}): {m.name!r}")
        out.append((rel, m))
    return out


def check_result(members, max_members, max_bytes, max_file_bytes):
    if len(members) > max_members:
        raise Reject(f"항목 수 초과: {len(members)} > {max_members}")
    out = []
    seen = set()
    folded = set()
    total = 0
    for m in members:
        rel = _norm(m.name)
        if not (m.isfile() or m.isdir()):
            raise Reject(f"regular file·directory 아님({m.type!r}): {m.name!r}")
        if rel == ".":
            if not m.isdir():
                raise Reject("루트 항목이 디렉터리가 아님")
            continue
        if rel in seen:
            raise Reject(f"중복 항목: {m.name!r}")
        if rel.casefold() in folded:
            raise Reject(f"대소문자만 다른 중복: {m.name!r}")
        seen.add(rel)
        folded.add(rel.casefold())
        if m.isfile():
            if m.size > max_file_bytes:
                raise Reject(f"파일 크기 초과: {m.name!r} {m.size}")
            total += m.size
            if total > max_bytes:
                raise Reject(f"합산 크기 초과: {total} > {max_bytes}")
        out.append((rel, m))
    # 파일 아래에 다른 항목이 오는 경우(파일을 디렉터리처럼 쓰기)를 거부한다.
    files = {rel for rel, m in out if m.isfile()}
    for rel, _ in out:
        parent = posixpath.dirname(rel)
        while parent:
            if parent in files:
                raise Reject(f"파일 아래 항목: {rel!r}")
            parent = posixpath.dirname(parent)
    return out


def _mkdirs(root_fd: int, parts: list[str]) -> int:
    fd = os.dup(root_fd)
    for p in parts:
        try:
            os.mkdir(p, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        nfd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = nfd
    return fd


def extract(tf, entries, dest: str, *, keep_modes: bool) -> None:
    root_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for rel, m in entries:
            parts = rel.split("/")
            if m.isdir():
                os.close(_mkdirs(root_fd, parts))
                continue
            dfd = _mkdirs(root_fd, parts[:-1])
            try:
                if m.issym():
                    os.symlink(m.linkname, parts[-1], dir_fd=dfd)
                    continue
                mode = (m.mode & 0o755) if keep_modes else 0o600
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
                with os.fdopen(fd, "wb") as w:
                    src = tf.extractfile(m)
                    remaining = m.size
                    while remaining:
                        chunk = src.read(min(1 << 20, remaining))
                        if not chunk:
                            raise Reject(f"본문이 잘림: {rel!r}")
                        w.write(chunk)
                        remaining -= len(chunk)
            finally:
                os.close(dfd)
    finally:
        os.close(root_fd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["install", "result"])
    ap.add_argument("dest")
    ap.add_argument("tar")
    ap.add_argument("--max-members", type=int, default=1000)
    ap.add_argument("--max-bytes", type=int, default=64 << 20)
    ap.add_argument("--max-file-bytes", type=int, default=16 << 20)
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args(argv)
    try:
        st = os.lstat(a.dest)
        if not stat.S_ISDIR(st.st_mode):
            raise Reject(f"DEST가 실제 디렉터리가 아님: {a.dest}")
        if os.listdir(a.dest):
            raise Reject(f"DEST가 비어 있지 않음: {a.dest}")
        with _open(a.tar) as tf:
            members = _members(tf)
            if a.mode == "install":
                entries = check_install(members)
            else:
                entries = check_result(members, a.max_members, a.max_bytes, a.max_file_bytes)
            if not a.check_only:
                extract(tf, entries, a.dest, keep_modes=(a.mode == "install"))
    except Reject as exc:
        print(f"REJECT: {exc}", file=sys.stderr)
        return 2
    except (tarfile.TarError, EOFError, OSError) as exc:
        # 추출 도중 실패하면 DEST에 일부가 남을 수 있다. 호출자가 DEST를 지운다.
        print(f"REJECT: tar·파일 오류: {exc}", file=sys.stderr)
        return 2
    print(f"OK {a.mode} {len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
