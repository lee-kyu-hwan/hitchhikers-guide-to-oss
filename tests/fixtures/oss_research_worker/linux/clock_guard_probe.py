#!/usr/bin/env python3
"""Guest, as root: the wrapper's main() with an unusable clock-set program (a test injection).

    clock_guard_probe.py SRC_TREE FAKE_PROGRAM

Loads the unit wrapper from the deployed source tree like a test (the module, not a
subprocess), points CLOCK_SET_PROGRAM at FAKE_PROGRAM, steps the system clock 120 s
into the past (the RTC is not touched), and runs main() for the light
`--entry verify` case with `launch` replaced by a counter. It prints one JSON line:
the exit code, the record main printed (if any), main's stderr and how often
launch was called (it must be 0 when main blocks). The product has no such knob:
this only changes the module constant of a copy loaded in this process.
Nothing is sent over the network; the unit, pin, relay and credential code are
never reached.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import time

tree, fake = Path(sys.argv[1]), Path(sys.argv[2])
script = tree / "dot_local/bin/executable_oss-research-linux-run"
loader = importlib.machinery.SourceFileLoader("probe_linux_run", str(script))
wrapper = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(wrapper)
wrapper.CLOCK_SET_PROGRAM = fake
launched = []
wrapper.launch = lambda *args, **kwargs: launched.append(args) or {
    "unit": None, "entry": "verify", "egress": "none", "exit_code": 0, "failures": []}
subprocess.run(["/usr/bin/date", "-u", "-s", f"@{int(time.time()) - 120}"], check=True,
               capture_output=True)
sys.argv = ["linux-run", "--entry", "verify", "--timeout", "60", "--egress", "none", "--",
            "unit-check", "/nonexistent"]
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = wrapper.main()
lines = out.getvalue().strip().splitlines()
print(json.dumps({"exit": code, "record": json.loads(lines[-1]) if lines else None,
                  "stderr": err.getvalue().strip()[-400:], "launch_calls": len(launched)}))
