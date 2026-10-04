#!/usr/bin/env python3
"""Runs inside srt: what an srt child can see of the GitHub token (a fixture sentinel).

    github_token_probe.py TOKEN_FILE RELAY_PORT REPOSITORY

The collection entries never tell the child the token file's path. This probe is told it
anyway and still tries: to read the file, to list its directory and the directories above
it, and to find the fixture marker (the substring SENTINEL, which the sentinel token holds)
in its own environment and command line. Then it asks the relay for one repository through
srt's own loopback proxy: the relay, which holds the token, answers. It prints one
result-shaped JSON line (the supervisor's collect()). It never prints a value it finds.
"""

import errno
import json
import os
import sys
import urllib.error
import urllib.request

path, relay, repository = sys.argv[1], int(sys.argv[2]), sys.argv[3]
for name in ("NO_PROXY", "no_proxy"):  # srt's NO_PROXY lists loopback, which would bypass its proxy
    os.environ.pop(name, None)


def failure(error: OSError) -> str:
    return errno.errorcode.get(error.errno, "OSError")


observed = {}
try:
    with open(path, "rb") as handle:
        handle.read(1)
    observed["open"] = "READ"
except OSError as error:
    observed["open"] = failure(error)
observed["exists"] = os.path.exists(path)
for directory in (os.path.dirname(path), os.path.dirname(os.path.dirname(path)),
                  "/run/oss-research-credentials", "/run"):
    try:
        observed["list:" + directory] = sorted(os.listdir(directory))[:6]
    except OSError as error:
        observed["list:" + directory] = failure(error)
with open("/proc/self/environ", "rb") as handle:
    observed["marker_in_environ"] = b"SENTINEL" in handle.read()
with open("/proc/self/cmdline", "rb") as handle:
    observed["marker_in_cmdline"] = b"SENTINEL" in handle.read()
observed["credential_like_variables"] = sorted(
    name for name in os.environ
    if any(word in name.upper() for word in ("TOKEN", "AUTH", "SECRET", "PASSWORD")) or name.startswith(("GH_", "GITHUB")))
try:
    with urllib.request.build_opener().open(
            urllib.request.Request(f"http://127.0.0.1:{relay}/v1/rest/repos/{repository}", method="GET"),
            timeout=30) as response:
        observed["relay_get"] = response.status
except urllib.error.HTTPError as error:
    observed["relay_get"] = error.code
except OSError as error:
    observed["relay_get"] = failure(error)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                  "result": json.dumps(observed)}))
