#!/usr/bin/env python3
"""Runs inside srt: the write-method regression of the candidate-reads step.

    candidate_write_probe.py RELAY_PORT

The child of the candidate-reads entry reaches the relay only through srt's loopback
proxy. This probe sends every write method to the relay that way (a REST path of the
target repository, and a GraphQL mutation) and prints one result-shaped JSON line
(the supervisor's collect()) with each status. The relay must answer 405 or 403 for all of
them; the parent counts what its upstream received (nothing). No credential.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# srt's NO_PROXY lists loopback, which would bypass its proxy; drop it.
for name in ("NO_PROXY", "no_proxy"):
    os.environ.pop(name, None)
OPENER = urllib.request.build_opener()
relay = int(sys.argv[1])
base = f"http://127.0.0.1:{relay}"


def send(method, url, body=None, headers=None):
    request = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with OPENER.open(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError as error:
        return type(error).__name__


observed = {"get": send("GET", base + "/v1/rest/repos/octo/demo")}
for method in ("POST", "PUT", "PATCH", "DELETE"):
    observed[method.lower() + "_rest"] = send(method, base + "/v1/rest/repos/octo/demo/issues", b"{}",
                                              {"Content-Type": "application/json"})
observed["post_graphql_mutation"] = send(
    "POST", base + "/v1/graphql",
    json.dumps({"query": "mutation { addComment(input: {}) { clientMutationId } }"}).encode(),
    {"Content-Type": "application/json"})
observed["post_search_code"] = send("POST", base + "/v1/rest/search/code?q=x", b"{}")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "permission_denials": [],
                  "result": json.dumps(observed)}))
