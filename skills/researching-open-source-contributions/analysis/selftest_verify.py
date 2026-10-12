#!/usr/bin/env python3
"""Mock self-test of verify_e2e_result.py (no model, no network, no credential).

    python3 selftest_verify.py [--corpus CORPUS]

The corpus defaults to fixtures/corpus-min.json: the two records
npm-eslint-markdown#677 and #676 (#676 so selftest_verify_kinds.py can run
its two-key case), copied unchanged from the already-collected
2026-09-06 corpus of the B functional check (sha256 345a7e14...fc64). That
past result document is not part of this tree, so the self-test no longer
depends on it; --corpus names another corpus explicitly. The "model answer"
and worker manifest are synthetic fixtures written here. PASS here proves
only that the verifier accepts and rejects the right shapes.
"""
import hashlib, json, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import machine_settings as machine  # noqa: E402

# The 2026-09-28 run record beside this bundle's history: its fixtures and legacy limits stay there (#261).
RECORD = machine.RECORDS_ROOT / "oss-e2e-2026-09-28"
CORPUS = RECORD / "fixtures/corpus-min.json"
if sys.argv[1:2] == ["--corpus"] and len(sys.argv) == 3:
    CORPUS = Path(sys.argv[2]).resolve()
elif len(sys.argv) != 1:
    sys.exit(__doc__)
VERIFY = HERE / "verify_e2e_result.py"
GOOD_ANSWER = {"lens": "blind", "patterns": [], "rejected": [],
               "observations": [{"pr": "npm-eslint-markdown#677", "note": "mock",
                                 "quote": "Updates `editorconfig-checker` from 6.1.1 to 6.2.0"}]}


def make(tmp, answer=GOOD_ANSWER, manifest_edit=None, prompt="mock blind prompt\n", report_text=None):
    run = tmp / "run"; (run / "result").mkdir(parents=True)
    text = report_text if report_text is not None else json.dumps(answer, ensure_ascii=False)
    (run / "result/report.md").write_text(text, encoding="utf-8")
    manifest = {"status": "succeeded", "profile": "claude-profile2", "exit_code": 0, "failures": [], "problems": [],
                "permission_mode": "auto", "requested_model": "claude-opus-5-5", "requested_effort": "medium",
                "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                "report_sha256": hashlib.sha256(text.encode()).hexdigest()}
    if manifest_edit:
        manifest_edit(manifest)
    (run / "result/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp / "task.txt").write_text(prompt, encoding="utf-8")
    return run


def verdict(tmp, run, corpus=CORPUS):
    p = subprocess.run([sys.executable, "-B", str(VERIFY), "--run-dir", str(run), "--corpus", str(corpus),
                        "--prompt", str(tmp / "task.txt"),
                        "--limits", str(RECORD / "run_limits.legacy-fixture.json")], capture_output=True, text=True)
    doc = json.loads(p.stdout)
    items = {i["id"].split("-")[0]: i["status"] for i in doc["items"]}
    items["class"] = doc["result_class"]
    return p.returncode, items


GOOD_PATTERN = {"title": "t", "description": "d", "kind": "convention", "applicability": [], "counterconditions": [],
                "search_clues": [], "expected_tests": [], "maintainer_judgment_required": [], "confidence": "low",
                "confidence_reason": "r",
                "evidence": [{"pr": "npm-eslint-markdown#677", "role": "supports", "claim": "c",
                              "quote": "Updates `editorconfig-checker` from 6.1.1 to 6.2.0"}]}


CASES = {
    "good": (dict(), 0, {"M1": "PASS", "M2": "PASS", "M3": "PASS", "Q1": "PASS", "class": "evidence"}),
    "good with pattern": (dict(answer={**GOOD_ANSWER, "patterns": [GOOD_PATTERN]}), 0, {"R3": "PASS", "Q1": "PASS", "class": "evidence"}),
    "staged keys on read-only": (dict(manifest_edit=lambda m: m.update(staged={"profile": "x"})), 1, {"M3": "FAIL", "class": "invalid"}),
    "explicit execution_profile on read-only": (dict(manifest_edit=lambda m: m.update(execution_profile="read-only")), 1, {"M3": "FAIL"}),
    "non-dict pattern": (dict(answer={**GOOD_ANSWER, "patterns": ["oops"]}), 1, {"R3": "FAIL", "Q1": "BLOCKED"}),
    "pattern without evidence": (dict(answer={**GOOD_ANSWER, "patterns": [{**GOOD_PATTERN, "evidence": []}]}), 1, {"R3": "FAIL"}),
    "evidence bad role": (dict(answer={**GOOD_ANSWER, "patterns": [{**GOOD_PATTERN, "evidence": [{**GOOD_PATTERN["evidence"][0], "role": "maybe"}]}]}), 1, {"R3": "FAIL"}),
    "non-dict observation": (dict(answer={**GOOD_ANSWER, "observations": ["x"]}), 1, {"R3": "FAIL"}),
    "empty honest answer": (dict(answer={"lens": "blind", "patterns": [], "observations": [], "rejected": [{"idea": "i", "reason": "r"}]}), 0, {"Q1": "PASS", "class": "insufficient"}),
    "empty answer but failed run": (dict(answer={"lens": "blind", "patterns": [], "observations": [], "rejected": []}, manifest_edit=lambda m: m.update(status="failed", exit_code=4)), 1, {"M1": "FAIL", "class": "invalid"}),
    "wrong model": (dict(manifest_edit=lambda m: m.update(requested_model="other")), 1, {"M2": "FAIL"}),
    "effort missing": (dict(manifest_edit=lambda m: m.pop("requested_effort")), 1, {"M2": "FAIL"}),
    "credential exposed": (dict(manifest_edit=lambda m: m.update(credential={"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "failed"})), 1, {"M4": "FAIL"}),
    "failed run": (dict(manifest_edit=lambda m: m.update(status="failed", exit_code=4)), 1, {"M1": "FAIL"}),
    # M4 by backend (#6 stage 2): Claude's form for no backend or claude, Codex's file form for codex only.
    "claude backend named": (dict(manifest_edit=lambda m: m.update(backend="claude")), 0, {"M4": "PASS"}),
    "codex credential": (dict(manifest_edit=lambda m: m.update(backend="codex", credential={"file": "home/.codex/auth.json", "exact_value_check": "clean"})), 0, {"M4": "PASS"}),
    "codex form on a claude manifest": (dict(manifest_edit=lambda m: m.update(credential={"file": "home/.codex/auth.json", "exact_value_check": "clean"})), 1, {"M4": "FAIL"}),
    "claude form on a codex manifest": (dict(manifest_edit=lambda m: m.update(backend="codex")), 1, {"M4": "FAIL"}),
    "codex credential exposed": (dict(manifest_edit=lambda m: m.update(backend="codex", credential={"file": "home/.codex/auth.json", "exact_value_check": "failed"})), 1, {"M4": "FAIL"}),
    "other backend": (dict(manifest_edit=lambda m: m.update(backend="agy", credential={"file": "home/.codex/auth.json", "exact_value_check": "clean"})), 1, {"M4": "FAIL"}),
    "prompt leaks path": (dict(prompt="read /x/.oss-research-credentials/claude-profile2/oauth-token\n"), 1, {"P2": "FAIL"}),
    "not json": (dict(report_text="Here is my answer: {}"), 1, {"R2": "FAIL", "R3": "BLOCKED", "Q1": "BLOCKED"}),
    "reworded quote": (dict(answer={**GOOD_ANSWER, "observations": [{"pr": "npm-eslint-markdown#677", "note": "x", "quote": "Updates editorconfig-checker from 6.1.1 to version 6.2.0"}]}), 1, {"Q1": "FAIL"}),
    "extra key": (dict(answer={**GOOD_ANSWER, "extra": 1}), 1, {"R3": "FAIL", "Q1": "BLOCKED"}),
    "report sha drift": (dict(manifest_edit=lambda m: m.update(report_sha256="0" * 64)), 1, {"R1": "FAIL"}),
}

def main():
    fails = 0
    for label, (kw, code, expect) in CASES.items():
        with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
            tmp = Path(t)
            run = make(tmp, **kw)
            rc, items = verdict(tmp, run)
            ok = rc == code and all(items.get(k) == v for k, v in expect.items())
            fails += not ok
            print("%s %s rc=%d %s" % ("PASS" if ok else "FAIL", label, rc, {k: items.get(k) for k in expect}))
    with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
        tmp = Path(t); run = make(tmp)
        secret = tmp / "elsewhere.txt"; secret.write_text("x", encoding="utf-8")
        (run / "result/report.md").unlink(); (run / "result/report.md").symlink_to(secret)
        rc, items = verdict(tmp, run)
        ok = rc == 1 and items.get("R1") == "FAIL"
        fails += not ok
        print("%s symlinked report rc=%d %s" % ("PASS" if ok else "FAIL", rc, items.get("R1")))
    with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
        tmp = Path(t); run = make(tmp)
        (run / "result/manifest.json").write_bytes(b"{" + b" " * (1 << 20) + b"}")
        rc, items = verdict(tmp, run)
        ok = rc == 1 and items.get("S1") == "FAIL" and items.get("M1") == "BLOCKED"
        fails += not ok
        print("%s oversized manifest rc=%d %s" % ("PASS" if ok else "FAIL", rc, items.get("S1")))
    with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
        tmp = Path(t); run = make(tmp); real = tmp / "real"; (run / "result").rename(real); (run / "result").symlink_to(real)
        rc, items = verdict(tmp, run)
        ok = rc == 1 and items.get("S1") == "FAIL"
        fails += not ok
        print("%s symlinked result dir rc=%d" % ("PASS" if ok else "FAIL", rc))
    with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
        tmp = Path(t); run = make(tmp); empty = tmp / "corpus.json"
        empty.write_text(json.dumps({"schema_version": "1.0.0", "generated_by": {}, "records": []}), encoding="utf-8")
        rc, items = verdict(tmp, run, corpus=empty)
        ok = rc == 1 and items.get("C1") == "FAIL" and items.get("Q1") == "BLOCKED"
        fails += not ok
        print("%s empty corpus rc=%d" % ("PASS" if ok else "FAIL", rc))
    with tempfile.TemporaryDirectory(prefix="e2e-selftest-") as t:
        tmp = Path(t); run = make(tmp); (run / "result/report.md").unlink()
        rc, items = verdict(tmp, run)
        ok = rc == 1 and items.get("R1") == "FAIL" and items.get("Q1") == "BLOCKED" and items.get("class") == "invalid"
        fails += not ok
        print("%s missing report rc=%d" % ("PASS" if ok else "FAIL", rc))
    print("failures=%d (mock)" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
