"""Non-AI child run inside srt by test_oss_research_offline_integration.py.

It reads a JSON plan (a file path), tries each step through the srt HTTP
proxy (or directly, to observe a denial), writes the outcome list to the plan's
``output`` path, prints the same list, and exits with the plan's ``exit_code``.
It prints relay error codes and payload digests, never file contents.
"""

import base64
import hashlib
import json
import os
import socket
import sys
import urllib.parse


def failure(error):
    name = getattr(error, "errno", None)
    return {"ok": False, "error": type(error).__name__,
            "errno": os.strerror(name) if isinstance(name, int) else None}


def proxy_address():
    # srt sets HTTP_PROXY to its local HTTP proxy for this process tree.
    parts = urllib.parse.urlsplit(os.environ["HTTP_PROXY"])
    return parts.hostname, parts.port


def proxy_authorization():
    # The srt proxy answers 407 without the credential it put in HTTP_PROXY.
    parts = urllib.parse.urlsplit(os.environ["HTTP_PROXY"])
    if parts.username is None:
        return []
    token = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
    return ["Proxy-Authorization: Basic " + base64.b64encode(token.encode()).decode()]


def read_response(connection):
    data = b""
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


def summarize(status, headers, body):
    result = {"status": status, "proxy_error": headers.get("x-proxy-error")}
    try:
        document = json.loads(body) if body else None
    except ValueError:
        document = None
    if isinstance(document, dict):
        result["relay_ok"] = document.get("ok")
        result["relay_error"] = document.get("error")
        if "payload" in document:
            result["payload_sha256"] = hashlib.sha256(
                json.dumps(document["payload"], sort_keys=True).encode()).hexdigest()
    return result


def request_text(method, target, host, body, extra=()):
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}", "Connection: close", *extra]
    if body is not None:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body.encode())}"]
    return ("\r\n".join(lines) + "\r\n\r\n" + (body or "")).encode()


def probe(step):
    op = step["op"]
    try:
        if op == "read":
            with open(step["path"], "rb") as handle:
                return {"ok": True, "sha256": hashlib.sha256(handle.read()).hexdigest()}
        if op == "write":
            descriptor = os.open(step["path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, step["text"].encode())
            os.close(descriptor)
            return {"ok": True}
        if op == "proxied":
            # Absolute-form request to the srt proxy; the proxy picks the destination.
            relay = step["relay"]
            with socket.create_connection(proxy_address(), timeout=step.get("timeout", 30)) as proxy:
                proxy.sendall(request_text(step["method"], f"http://{relay}{step['target']}",
                                           step.get("host", relay), step.get("body"),
                                           proxy_authorization()))
                return dict(summarize(*read_response(proxy)), ok=True)
        if op == "connect":
            with socket.create_connection(proxy_address(), timeout=step.get("timeout", 30)) as proxy:
                proxy.sendall("\r\n".join([f"CONNECT {step['authority']} HTTP/1.1",
                                             f"Host: {step['authority']}",
                                             *proxy_authorization(), "", ""]).encode())
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = proxy.recv(1)
                    if not chunk:
                        break
                    head += chunk
                lines = head.decode("latin-1").split("\r\n")
                status = int(lines[0].split()[1]) if lines[0] else None
                proxy_error = next((line.split(":", 1)[1].strip() for line in lines[1:]
                                    if line.lower().startswith("x-proxy-error:")), None)
                result = {"ok": True, "status": status, "proxy_error": proxy_error}
                if status == 200 and "inner" in step:
                    inner = step["inner"]
                    proxy.sendall(request_text(inner["method"], inner["target"],
                                               inner["host"], inner.get("body")))
                    result["inner"] = summarize(*read_response(proxy))
                return result
        if op == "tcp":
            with socket.create_connection((step["host"], step["port"]), timeout=5):
                return {"ok": True}
        raise ValueError(f"unknown op {op}")
    except (OSError, ValueError, IndexError) as error:
        return failure(error)


def main():
    with open(sys.argv[1], encoding="utf-8") as source:
        plan = json.load(source)
    results = [dict(probe(step), op=step["op"], name=step["name"]) for step in plan["steps"]]
    text = json.dumps(results)
    descriptor = os.open(plan["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, text.encode())
    os.close(descriptor)
    print(text)
    sys.stdout.flush()
    return plan["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
