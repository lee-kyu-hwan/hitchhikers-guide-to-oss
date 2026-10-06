#!/usr/bin/env python3
"""Judge one wrapper record of the --egress pin non-AI integration (E3).

    egress_verdict.py RECORD.json            # every check, one line each
    egress_verdict.py RECORD.json NAME       # one check

The record is the JSON line oss-research-linux-run printed for
`--entry verify --egress pin -- egress OUT CA`. It is data only.

Preconditions come first: both sandboxed runs ended normally with every
observation, the control run is the counterfactual policy (no
deniedResolvedAddresses, marked test-only when the record says so), and the
product policy reached the fixture over 443 (the positive). A check that
depends on a failed precondition is BLOCKED, never PASS. Exit 0 only when the
asked checks all PASS; 1 on a FAIL; 2 when only BLOCKED remain.
"""

import json
import re
import sys

KEYS = {"model_443", "model_8443", "other_name", "denied_resolve", "direct"}
SHA = re.compile(r"sha256:[0-9a-f]{64}")
CHECKS = ("W1-wrapper-ok", "W2-pin-apply-verify-clear", "W3-evidence-matches-pin",
          "E0-runs-complete", "E1-model-443-through-srt-tls-fixture-ca",
          "E2-model-8443-refused-by-port", "E3-unlisted-name-refused",
          "E4-private-resolution-refused", "E4c-counterfactual-dials-without-the-list",
          "E5-direct-tcp-impossible")


def tunnel(result: dict, code: str) -> bool:
    return f"Tunnel connection failed: {code}" in str(result.get("detail", ""))


def verdict(record: dict) -> dict:
    """{check: (status, detail)} with status PASS, FAIL or BLOCKED."""
    try:
        inner = json.loads(record["stdout"].strip().splitlines()[-1])["results"]
    except (KeyError, IndexError, TypeError, ValueError):
        inner = {}
    if not isinstance(inner, dict):
        inner = {}
    product = inner.get("product") or {}
    control = inner.get("control-without-denied-resolved") or {}
    p = product.get("observed") or {}
    c = control.get("observed") or {}
    pin = record.get("egress_pin") or {}
    apply, verify, clear = (pin.get(k) or {} for k in ("apply", "verify", "clear"))
    evidence = record.get("egress_evidence") or {}
    reported = inner.get("egress_reported_not_verified") or {}

    counterfactual = (control.get("deniedResolvedAddresses") is None
                      and bool(product.get("deniedResolvedAddresses"))
                      and control.get("counterfactual_test_only", True) is True
                      and product.get("counterfactual_test_only", False) is False)
    runs_ok = (product.get("exit") == 0 and control.get("exit") == 0 and KEYS <= set(p)
               and "denied_resolve" in c and counterfactual)
    positive = (runs_ok and (p.get("model_443") or {}).get("status") == 200
                and str((p.get("model_443") or {}).get("body", "")).startswith(
                    "mock-ok 140.82.112.7:443"))
    sha = apply.get("manifest_sha256")
    expires = apply.get("expires_at")
    table = {
        "W1-wrapper-ok": (None, record.get("exit_code") == 0 and record.get("failures") == []),
        "W2-pin-apply-verify-clear": (None, apply.get("exit") == 0
                                      and apply.get("status") == "pinned"
                                      and verify.get("exit") == 0 and verify.get("ok") is True
                                      and clear.get("exit") == 0
                                      and clear.get("status") == "cleared"),
        "W3-evidence-matches-pin": (None, isinstance(sha, str) and SHA.fullmatch(sha) is not None
                                    and isinstance(expires, str)
                                    and verify.get("manifest_sha256") == sha
                                    == evidence.get("manifest_sha256")
                                    == reported.get("OSS_RESEARCH_EGRESS_MANIFEST_SHA256")
                                    and isinstance(record.get("unit"), str)
                                    and record.get("unit") == apply.get("run_id")
                                    == verify.get("run_id") == evidence.get("run_id")
                                    == reported.get("OSS_RESEARCH_EGRESS_RUN_ID")
                                    and verify.get("expires_at") == expires
                                    == evidence.get("expires_at")
                                    == reported.get("OSS_RESEARCH_EGRESS_EXPIRES_AT")),
        "E0-runs-complete": (None, runs_ok),
        "E1-model-443-through-srt-tls-fixture-ca": (runs_ok, positive),
        "E2-model-8443-refused-by-port": (positive, tunnel(p.get("model_8443") or {}, "403")),
        "E3-unlisted-name-refused": (positive, tunnel(p.get("other_name") or {}, "403")),
        "E4-private-resolution-refused": (positive, tunnel(p.get("denied_resolve") or {}, "403")),
        # The counterfactual must show srt dialing (502 once nft refused the dial),
        # not merely "not 403": an empty or failed control proves nothing.
        "E4c-counterfactual-dials-without-the-list": (positive,
                                                      tunnel(c.get("denied_resolve") or {}, "502")),
        "E5-direct-tcp-impossible": (positive, re.fullmatch(
            r"(ConnectionRefusedError|OSError):\d+", str(p.get("direct", ""))) is not None),
    }
    detail = json.dumps({"product": p, "control": c.get("denied_resolve")})[:600]
    result = {}
    for name, (precondition, ok) in table.items():
        status = "BLOCKED" if precondition is False else ("PASS" if ok else "FAIL")
        result[name] = (status, detail)
    return result


def main() -> int:
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as handle:
        record = json.load(handle)
    names = CHECKS if len(sys.argv) == 2 else (sys.argv[2],)
    judged = verdict(record)
    statuses = []
    for name in names:
        status, detail = judged[name]
        statuses.append(status)
        print(f"{status} {name} {detail}")
    if "FAIL" in statuses:
        return 1
    return 2 if "BLOCKED" in statuses else 0


if __name__ == "__main__":
    raise SystemExit(main())
