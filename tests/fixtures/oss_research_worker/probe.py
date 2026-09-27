"""Non-AI probe run inside srt by test_oss_research_worker_runtime.py.

It reads a JSON plan (a file path, or "-" for stdin), tries each operation and
prints one JSON list of outcomes. It never prints file contents or variable
values: an env probe reports names and whether a sentinel was seen.
"""

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def failure(error):
    error = getattr(error, "reason", error)
    name = getattr(error, "errno", None)
    return {"ok": False, "error": type(error).__name__,
            "errno": os.strerror(name) if isinstance(name, int) else None}


def probe(step):
    op = step["op"]
    try:
        if op == "read":
            with open(step["path"], "rb") as handle:
                handle.read(1)
            return {"ok": True}
        if op == "write":
            flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if step.get("replace") else os.O_EXCL)
            descriptor = os.open(step["path"], flags, 0o600)
            os.write(descriptor, b"written by the sandboxed probe\n")
            os.close(descriptor)
            return {"ok": True}
        if op == "listdir":
            os.listdir(step["path"])
            return {"ok": True}
        if op == "rename":
            os.rename(step["path"], step["target"])
            return {"ok": True}
        if op == "env":
            sentinel = step["sentinel"]
            return {"ok": True, "names": sorted(os.environ),
                    "sentinel_seen": any(sentinel in value for value in os.environ.values())}
        if op == "unix":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(step["path"])
            return {"ok": True}
        if op == "tcp":
            with socket.create_connection((step["host"], step["port"]), timeout=5):
                pass
            return {"ok": True}
        if op == "http":
            proxy = os.environ.get("HTTP_PROXY") if step.get("proxy") else None
            if proxy:
                # srt lists 127.0.0.1 in NO_PROXY; urllib would then dial directly.
                os.environ.pop("NO_PROXY", None)
                os.environ.pop("no_proxy", None)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy} if proxy else {}))
            body = step.get("body")
            request = urllib.request.Request(
                step["url"], method=step.get("method", "GET"),
                data=body.encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=10) as response:
                    return {"ok": True, "status": response.status}
            except urllib.error.HTTPError as error:
                return {"ok": False, "status": error.code,
                        "proxy_error": error.headers.get("X-Proxy-Error")}
        if op == "run":
            environment = {name: value for name, value in os.environ.items()
                           if name not in step.get("unset", ())}
            completed = subprocess.run(step["argv"], cwd=step.get("cwd"), env=environment,
                                       capture_output=True, text=True, timeout=60)
            return {"ok": completed.returncode == 0, "returncode": completed.returncode,
                    "stdout": completed.stdout[-400:] if step.get("stdout") else None,
                    "stderr": completed.stderr[-400:]}
        if op == "child":
            completed = subprocess.run([sys.executable, __file__, "-"],
                                       input=json.dumps(step["plan"]),
                                       capture_output=True, text=True, timeout=60)
            return {"ok": completed.returncode == 0,
                    "results": json.loads(completed.stdout) if completed.stdout else None,
                    "stderr": completed.stderr[-400:]}
        raise ValueError(f"unknown op {op}")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return failure(error)


def main():
    source = sys.stdin if sys.argv[1] == "-" else open(sys.argv[1], encoding="utf-8")
    plan = json.load(source)
    print(json.dumps([dict(probe(step), op=step["op"], name=step.get("name"))
                      for step in plan]))


if __name__ == "__main__":
    main()
