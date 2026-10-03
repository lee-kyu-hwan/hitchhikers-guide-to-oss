#!/usr/bin/env python3
"""Does a systemd transient unit's cgroup end a setsid descendant? (Linux guest)

    sudo python3 cgroup_escape.py check     # as root on the guest; prints JSON
    python3 cgroup_escape.py payload TAG    # started by check, as ossresearch

payload forks a child that calls setsid and re-execs a sleeper carrying TAG
in its argv; payload itself exits at once. check runs it twice:

1. control: `runuser -u ossresearch` without a unit. The sleeper must still be
   there afterwards, found by TAG in /proc/*/cmdline. This shows the scan sees
   an escaped process at all; the sleeper is then killed by the script.
2. unit: the same payload under `systemd-run --uid=ossresearch --wait` with
   KillMode=control-group. Once systemd-run returns, no process may carry TAG
   (retried for a few seconds) and the unit must be gone.

Only local processes are started. No network, model, token or OSS code.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

USER = "ossresearch"
SETTLE_SECONDS = 5.0


def holding(tag: str) -> list[int]:
    """Pids whose argv holds tag. Raises unless /proc shows this process."""
    found, seen_self = [], False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(entry) == os.getpid():
            seen_self = bool(cmdline)
        if tag.encode() in cmdline:
            found.append(int(entry))
    if not seen_self:
        raise SystemExit("/proc does not show this process; the scan is blind")
    return found


def settled(tag: str) -> list[int]:
    until = time.monotonic() + SETTLE_SECONDS
    survivors = holding(tag)
    while survivors and time.monotonic() < until:
        time.sleep(0.1)
        survivors = holding(tag)
    return survivors


def payload(tag: str) -> None:
    if os.fork() == 0:
        os.setsid()
        os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(120)", tag])
    time.sleep(0.3)


def check() -> int:
    if os.geteuid() != 0:
        raise SystemExit("check runs as root (it starts a unit for ossresearch)")
    script = str(Path(__file__).resolve())
    python = sys.executable
    result = {"user": USER}
    control = f"osr-cg-control-{uuid.uuid4().hex}"
    subprocess.run(["/usr/sbin/runuser", "-u", USER, "--", python, "-I", script, "payload", control],
                   check=True, timeout=30)
    time.sleep(0.5)
    survivors = holding(control)
    result["control_survivors"] = len(survivors)
    for pid in survivors:
        os.kill(pid, signal.SIGKILL)
    unit_tag = f"osr-cg-unit-{uuid.uuid4().hex}"
    unit = f"osr-cg-{uuid.uuid4().hex[:12]}"
    completed = subprocess.run(
        ["/usr/bin/systemd-run", "--quiet", "--wait", "--collect", "--unit", unit,
         f"--uid={USER}", f"--gid={USER}", "-p", "KillMode=control-group",
         "-p", "RuntimeMaxSec=60", "-p", "TasksMax=16",
         python, "-I", script, "payload", unit_tag],
        capture_output=True, text=True, timeout=90)
    result["unit_exit"] = completed.returncode
    result["unit_stderr"] = completed.stderr[-500:]
    survivors = settled(unit_tag)
    result["unit_survivors"] = len(survivors)
    for pid in survivors:
        os.kill(pid, signal.SIGKILL)
    state = subprocess.run(["/usr/bin/systemctl", "show", "-p", "LoadState", "--value", unit],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    result["unit_load_state"] = state
    result["pass"] = (result["control_survivors"] >= 1 and completed.returncode == 0
                      and result["unit_survivors"] == 0 and state == "not-found")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["payload"] and len(sys.argv) == 3:
        payload(sys.argv[2])
    elif sys.argv[1:] == ["check"]:
        raise SystemExit(check())
    else:
        raise SystemExit(__doc__)
