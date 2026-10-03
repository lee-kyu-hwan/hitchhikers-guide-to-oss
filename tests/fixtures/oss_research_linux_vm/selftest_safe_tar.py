#!/usr/bin/env python3
"""safe_tar.py 거부 fixture 자체 시험. 임시 디렉터리만 쓴다.

실행: /opt/homebrew/bin/python3 tests/fixtures/oss_research_linux_vm/selftest_safe_tar.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "safe_tar.py")


def _add(tf, name, *, kind="file", data=b"x", link="", mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.mode = mode
    if kind == "file":
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        return
    ti.type = {
        "dir": tarfile.DIRTYPE,
        "sym": tarfile.SYMTYPE,
        "hard": tarfile.LNKTYPE,
        "fifo": tarfile.FIFOTYPE,
        "chr": tarfile.CHRTYPE,
    }[kind]
    ti.linkname = link
    tf.addfile(ti)


def build(path, entries):
    with tarfile.open(path, "w") as tf:
        for e in entries:
            _add(tf, **e)


def run(mode, tar, *extra):
    dest = tempfile.mkdtemp(prefix="dest-", dir=WORK)
    p = subprocess.run([sys.executable, TOOL, mode, dest, tar, *extra], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip(), dest


GOOD = [
    {"name": "./", "kind": "dir"},
    {"name": "./manifest.json", "data": b"{}"},
    {"name": "./report", "kind": "dir"},
    {"name": "./report/report.md", "data": b"# ok\n"},
]

RESULT_CASES = {
    "symlink": GOOD + [{"name": "./leak", "kind": "sym", "link": "/etc/passwd"}],
    "relative-symlink": GOOD + [{"name": "./report/l", "kind": "sym", "link": "report.md"}],
    "hardlink": GOOD + [{"name": "./h", "kind": "hard", "link": "manifest.json"}],
    "fifo": GOOD + [{"name": "./p", "kind": "fifo"}],
    "chardev": GOOD + [{"name": "./c", "kind": "chr"}],
    "absolute": [{"name": "/tmp/x"}],
    "dotdot": [{"name": "../x"}],
    "inner-dotdot": [{"name": "report/../../x"}],
    "duplicate": [{"name": "a"}, {"name": "./a"}],
    "case-duplicate": [{"name": "Report.md"}, {"name": "report.md"}],
    "file-under-file": [{"name": "a"}, {"name": "a/b"}],
    "control-char": [{"name": "a\nb"}],
    "too-many": [{"name": f"f{i}"} for i in range(6)],
    "too-big-total": [{"name": "a", "data": b"x" * 600}, {"name": "b", "data": b"x" * 600}],
    "too-big-file": [{"name": "a", "data": b"x" * 2000}],
}

INSTALL_CASES = {
    "outside-top": [{"name": "etc/x"}],
    "abs-symlink": [{"name": "bin", "kind": "dir"}, {"name": "bin/l", "kind": "sym", "link": "/usr/bin/true"}],
    "escape-symlink": [{"name": "bin", "kind": "dir"}, {"name": "bin/l", "kind": "sym", "link": "../../x"}],
    "hardlink": [{"name": "bin/a"}, {"name": "bin/b", "kind": "hard", "link": "bin/a"}],
    "dotdot": [{"name": "bin/../../x"}],
}


def main() -> int:
    failures = 0
    good = os.path.join(WORK, "good.tar")
    build(good, GOOD)
    rc, out, dest = run("result", good)
    ok = rc == 0 and sorted(os.listdir(dest)) == ["manifest.json", "report"]
    modes = {os.stat(os.path.join(dest, "manifest.json")).st_mode & 0o777}
    ok = ok and modes == {0o600}
    print(f"{'PASS' if ok else 'FAIL'} result good -> rc={rc} {out}")
    failures += not ok

    nonempty = tempfile.mkdtemp(prefix="dest-", dir=WORK)
    open(os.path.join(nonempty, "x"), "w").close()
    p = subprocess.run([sys.executable, TOOL, "result", nonempty, good], capture_output=True, text=True)
    ok = p.returncode == 2
    print(f"{'PASS' if ok else 'FAIL'} result nonempty-dest -> rc={p.returncode}")
    failures += not ok

    trunc = os.path.join(WORK, "trunc.tar")
    big = os.path.join(WORK, "big.tar")
    build(big, [{"name": "a", "data": b"y" * 5000}])
    with open(big, "rb") as r, open(trunc, "wb") as w:
        w.write(r.read(1024))
    rc, out, dest = run("result", trunc)
    ok = rc == 2 and not os.listdir(dest)
    print(f"{'PASS' if ok else 'FAIL'} result truncated -> rc={rc} {out}")
    failures += not ok

    limits = ["--max-members", "5", "--max-bytes", "1000", "--max-file-bytes", "1500"]
    for name, entries in RESULT_CASES.items():
        path = os.path.join(WORK, f"r-{name}.tar")
        build(path, entries)
        rc, out, dest = run("result", path, *limits)
        ok = rc == 2 and not os.listdir(dest)
        print(f"{'PASS' if ok else 'FAIL'} result {name} -> rc={rc} {out}")
        failures += not ok

    good_install = os.path.join(WORK, "i-good.tar")
    build(good_install, [
        {"name": "bin", "kind": "dir"},
        {"name": "bin/limactl", "data": b"#!", "mode": 0o755},
        {"name": "bin/alias", "kind": "sym", "link": "limactl"},
        {"name": "share/lima", "kind": "dir"},
    ])
    rc, out, dest = run("install", good_install)
    ok = rc == 0 and os.path.islink(os.path.join(dest, "bin/alias"))
    print(f"{'PASS' if ok else 'FAIL'} install good -> rc={rc} {out}")
    failures += not ok
    for name, entries in INSTALL_CASES.items():
        path = os.path.join(WORK, f"i-{name}.tar")
        build(path, entries)
        rc, out, dest = run("install", path)
        ok = rc == 2 and not os.listdir(dest)
        print(f"{'PASS' if ok else 'FAIL'} install {name} -> rc={rc} {out}")
        failures += not ok

    print(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="safe-tar-selftest-") as WORK:
        sys.exit(main())
