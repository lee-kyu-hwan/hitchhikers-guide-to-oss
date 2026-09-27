"""Non-AI child for the supervisor's real-srt tests. Not Claude, no model.

    relay_probe.py read RELAY_PORT MOCK_PORT   read through the relay, try the rest
    relay_probe.py hang RELAY_PORT             one relay read that never answers
    relay_probe.py escape RELAY_PORT OUT       fork, setsid, hold stdout; parent exits

`read` prints a result-shaped JSON object so the parent's collect treats it
like a result; its text is the probe's observations, not a model answer.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request

# srt's NO_PROXY lists loopback, which would bypass its proxy; drop it.
for name in ("NO_PROXY", "no_proxy"):
    os.environ.pop(name, None)
PROXY = urllib.request.build_opener()


def request(url, method="GET", timeout=30):
    try:
        with PROXY.open(urllib.request.Request(url, method=method,
                                               data=b"{}" if method != "GET" else None),
                        timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except OSError as error:
        return None, type(error).__name__


def direct(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        return "connected"
    except OSError as error:
        return type(error).__name__ + ":" + str(error.errno)


mode, relay = sys.argv[1], int(sys.argv[2])
base = f"http://127.0.0.1:{relay}/v1/rest/repos/octo/demo"
if mode == "hang":
    request(f"http://127.0.0.1:{relay}/v1/rest/repos/octo/hang", timeout=3600)
    raise SystemExit(0)
if mode == "escape":
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            outcome = "setsid"
        except OSError as error:
            outcome = type(error).__name__
        with open(sys.argv[3], "w") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "outcome": outcome}))
        time_module = __import__("time")
        time_module.sleep(60)
        os._exit(0)
    __import__("time").sleep(0.5)
    raise SystemExit(0)
mock = int(sys.argv[3])
status, body = request(base)
observed = {
    "get": [status, json.loads(body)["payload"].get("full_name") if status == 200 else body[:80]],
    "post": request(base, method="POST")[0],
    "direct_relay": direct(relay),
    "proxy_mock": request(f"http://127.0.0.1:{mock}/repos/octo/demo")[0],
    "direct_mock": direct(mock),
}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "permission_denials": [], "result": json.dumps(observed)}))
