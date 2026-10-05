#!/usr/bin/env python3
"""Mock self-test of verify_e2e_result.py --input-scope (no model, no network, no credential).

    python3 selftest_verify_scope.py

When a stage's input is built from extraction answers instead of the digest, an
answer may only use what that input held. The stage writes input-scope.json
(stage_inputs.py) and the verifier is given it: a synthesis answer may cite only
the digest keys of its input, a verify answer may only quote PRs whose section was
in its prompt, and a final answer's pattern quotes must be (PR, quote) pairs the
input listed as verified. Without --input-scope nothing changes (selftest_verify_kinds.py).
"""
import json, sys, tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest_verify_kinds as kinds  # noqa: E402

KEYS = list(kinds.KEYS)
KEY, QUOTE = kinds.KEY, kinds.QUOTE
OTHER = next(key for key in KEYS if key != KEY)
reworded_ok = QUOTE  # the same string in both the corpus and the allowed list


def scope(tmp, **document):
    path = tmp / "input-scope.json"
    path.write_text(document.pop("raw") if "raw" in document else json.dumps(document), encoding="utf-8")
    return ["--input-scope", str(path)]


def synth(*keys):
    return {"candidates": [{"id": "C1", "statement": "s", "cited_prs": list(keys)}], "dropped": []}


def run_case(kind, answer, scope_args, code, expect):
    with tempfile.TemporaryDirectory(prefix="e2e-scope-") as t:
        tmp = Path(t)
        rc, items, _ = kinds.verdict(tmp, kinds.make(tmp, kind, answer=answer), kind, scope_args(tmp))
        ok = rc == code and all(items.get(k) == v for k, v in expect.items())
        return ok, rc, {k: items.get(k) for k in expect}


CASES = [
    ("synthesis/cites only keys of its input", "synthesis", synth(*KEYS[:2]),
     lambda t: scope(t, keys=KEYS[:2]), 0, {"R3": "PASS", "R5": "PASS", "class": "evidence"}),
    ("synthesis/cites a key its input did not hold", "synthesis", synth(*KEYS[:2]),
     lambda t: scope(t, keys=[KEYS[0]]), 1, {"R3": "PASS", "R5": "FAIL", "class": "invalid"}),
    ("synthesis/an empty answer cites nothing", "synthesis", None,
     lambda t: scope(t, keys=[]), 0, {"R5": "PASS", "class": "insufficient"}),
    ("verify/quotes a PR whose section was in the prompt", "verify", None,
     lambda t: scope(t, keys=[KEY]), 0, {"R5": "PASS", "Q1": "PASS"}),
    ("verify/quotes a PR whose section was not in the prompt", "verify", None,
     lambda t: scope(t, keys=[OTHER]), 1, {"R5": "FAIL", "class": "invalid"}),
    ("final/quotes a pair the input listed as verified", "final", None,
     lambda t: scope(t, keys=KEYS, quotes=[{"pr": KEY, "quote": QUOTE}]), 0, {"R5": "PASS", "Q1": "PASS"}),
    ("final/quote is in the corpus but was not in the input", "final", None,
     lambda t: scope(t, keys=KEYS, quotes=[{"pr": KEY, "quote": "another verified sentence"}]), 1, {"R5": "FAIL", "class": "invalid"}),
    ("final/quote belongs to another PR in the input", "final", None,
     lambda t: scope(t, keys=KEYS, quotes=[{"pr": OTHER, "quote": QUOTE}]), 1, {"R5": "FAIL"}),
    ("final/no quote needs no list", "final", {**kinds.GOOD["final"], "patterns": []},
     lambda t: scope(t, keys=KEYS, quotes=[]), 0, {"R5": "PASS", "class": "insufficient"}),
    ("final/a scope without quotes allows none", "final", None,
     lambda t: scope(t, keys=KEYS), 1, {"R5": "FAIL"}),
    ("scope is not JSON", "synthesis", None, lambda t: scope(t, raw="{"), 2, {}),
    ("scope has no keys list", "synthesis", None, lambda t: scope(t, quotes=[]), 2, {}),
    ("scope keys are not strings", "synthesis", None, lambda t: scope(t, keys=[1]), 2, {}),
    ("scope quotes are malformed", "final", None, lambda t: scope(t, keys=KEYS, quotes=[{"pr": KEY}]), 2, {}),
    ("an extract answer takes no scope", "extract", None, lambda t: scope(t, keys=KEYS), 2, {}),
]


# A corpus sentence (npm-eslint-markdown#677 in corpus-min.json) that holds double quotes, and the same sentence
# with each " written as the JSON notation \" (what the 2026-10-05 r5 final answer did): still refused, never
# normalized, and the R5 detail says the quote carries a JSON escape (#82).
SOURCE = 'Sourced from <a href="https://github.com/editorconfig-checker/editorconfig-checker.javascript/releases">'
BACKSLASHED = SOURCE.replace('"', '\\"')


def final_quoting(quote):
    answer = json.loads(json.dumps(kinds.GOOD["final"]))
    answer["patterns"][0]["confidence"]["evidence"] = ['%s "%s"' % (kinds.PR_OF[KEY], quote)]
    return answer


ESCAPE_CASES = [
    # label, answer quote, listed quote (None: no --input-scope), code, statuses, R5 detail holds "JSON escape"
    ("escape/the source text with its double quotes passes", SOURCE, SOURCE, 0, {"R5": "PASS", "Q1": "PASS"}, False),
    ("escape/a backslash before each double quote fails and the reason says so", BACKSLASHED, SOURCE, 1,
     {"R5": "FAIL", "class": "invalid"}, True),
    ("escape/an unlisted quote fails without the escape reason", SOURCE, "another verified sentence", 1,
     {"R5": "FAIL"}, False),
    ("escape/without a scope the escaped quote fails the corpus check", BACKSLASHED, None, 1, {"Q1": "FAIL"}, None),
]


def run_escape_case(quote, listed, code, expect, escape_reason):
    with tempfile.TemporaryDirectory(prefix="e2e-escape-") as t:
        tmp = Path(t)
        extra = [] if listed is None else scope(tmp, keys=KEYS, quotes=[{"pr": KEY, "quote": listed}])
        rc, items, doc = kinds.verdict(tmp, kinds.make(tmp, "final", answer=final_quoting(quote)), "final", extra)
        detail = next((i["detail"] for i in doc.get("items", []) if i["id"] == "R5-input-scope"), "")
        ok = rc == code and all(items.get(k) == v for k, v in expect.items()) \
            and (escape_reason is None or ("JSON escape" in detail) == escape_reason)
        return ok, rc, {**{k: items.get(k) for k in expect}, "R5 detail": detail[:90]}


def main():
    fails = 0
    for label, kind, answer, scope_args, code, expect in CASES:
        ok, rc, got = run_case(kind, answer, scope_args, code, expect)
        fails += not ok
        print("%s %s rc=%d %s" % ("PASS" if ok else "FAIL", label, rc, got))
    for label, quote, listed, code, expect, escape_reason in ESCAPE_CASES:
        ok, rc, got = run_escape_case(quote, listed, code, expect, escape_reason)
        fails += not ok
        print("%s %s rc=%d %s" % ("PASS" if ok else "FAIL", label, rc, got))
    print("cases=%d failures=%d (mock, corpus %s)" % (len(CASES) + len(ESCAPE_CASES), fails, kinds.CORPUS.name))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
