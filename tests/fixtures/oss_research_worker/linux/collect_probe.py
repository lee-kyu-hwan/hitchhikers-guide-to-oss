#!/usr/bin/env python3
"""Runs inside srt: the direct-connect regression of the collection step.

    collect_probe.py ADAPTER ADAPTER_ARGUMENTS...

The arguments are the production collection entry's own adapter argv
(adapter_arguments, with --require-proxy). This probe drops --require-proxy
and every proxy variable, so the adapter connects to 127.0.0.1:<relay port>
directly; srt's network namespace must refuse that. It prints one
result-shaped JSON line (the supervisor's collect()) with the adapter's exit,
a bounded stderr tail and the transport record's outcomes. No credential.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

adapter, arguments = sys.argv[1], [a for a in sys.argv[2:] if a != "--require-proxy"]
transport = Path(arguments[arguments.index("--transport-record") + 1])
proxy_names = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
               "NO_PROXY", "no_proxy")
at = arguments.index("--timeout-seconds")
arguments[at + 1] = "5"
try:
    completed = subprocess.run([sys.executable, "-B", adapter, *arguments], capture_output=True,
                               text=True, timeout=120,
                               env={k: v for k, v in os.environ.items() if k not in proxy_names})
    code, stderr = completed.returncode, completed.stderr[-600:]
except subprocess.TimeoutExpired:
    code, stderr = None, "timeout"
record = json.loads(transport.read_text()) if transport.is_file() else {}
summary = {"adapter_exit": code, "stderr_tail": stderr, "transport": record.get("transport"),
           "requests": len(record.get("requests", [])),
           "outcomes": sorted({str(r.get("outcome")) for r in record.get("requests", [])}),
           "proxy_variables_seen": sorted(name for name in proxy_names if name in os.environ)}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                  "result": json.dumps(summary)}))
