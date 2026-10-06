#!/usr/bin/env python3
"""Mock self-test of verify_e2e_result.py --answer-kind (no model, no network, no credential).

    python3 selftest_verify_kinds.py

Every kind (extract, synthesis, verify, final) must stop on the same worker
problems: failed run, refusal text, malformed JSON, wrong model, mode or
effort, a credential check that is not clean, report sha drift, and a
recorded prompt_sha256 that does not match. Under the strict default limits
they must also stop on a missing or different prompt_sha256, observed_models
that are missing (unknown), empty, mixed or another model, and failures or
problems that are missing or not empty. The legacy fixture limits are checked
to apply only when marked, and never to adopt. Kind-specific shapes and quotes
are checked too, and --adopt-to must write only a verified answer.

The corpus is fixtures/corpus-min.json when present (A bd1bb37), otherwise the
2026-09-06 corpus copy of the B functional check. Answers and manifests are
synthetic. PASS proves only that the verifier accepts and rejects these shapes.
"""
import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "verify_e2e_result.py"
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import machine_settings as machine  # noqa: E402

# The 2026-09-28 run record beside this bundle's history: its fixtures and legacy limits stay there (#261).
RECORD = machine.RECORDS_ROOT / "oss-e2e-2026-09-28"
CORPUS = next(p for p in (RECORD / "fixtures/corpus-min.json",
                          RECORD.parent / "oss-functional-validation-2026-09-28/raw/corpus.json") if p.exists())
KEY, QUOTE = "npm-eslint-markdown#677", "Updates `editorconfig-checker` from 6.1.1 to 6.2.0"
sys.path.insert(0, str(HERE.parents[3] / "dot_codex/skills/analyzing-open-source-pr-patterns/scripts"))
from build_digest import records_by_key  # noqa: E402

KEYS = records_by_key(json.loads(CORPUS.read_text(encoding="utf-8")))
PR_OF = {key: record["pr_id"] for key, record in KEYS.items()}
URL_OF = {record["pr_id"]: record["pull_request"]["url"] for record in KEYS.values()}
PROMPT = "mock stage prompt\n"


def claim(pr_id):
    return {"value": "mock", "basis": "fact", "evidence_links": [URL_OF[pr_id]]}


GOOD = {
    "extract": {"lens": "correctness", "patterns": [], "rejected": [],
                "observations": [{"pr": KEY, "note": "mock", "quote": QUOTE}]},
    "synthesis": {"candidates": [], "dropped": [{"idea": "i", "reason": "one PR only"}]},
    "verify": {"id": "C1", "verdict": "weak", "evidence_checks": [{"pr": KEY, "holds": True, "quote": QUOTE, "reason": "r"}],
               "counterexamples": [], "distinct_prs": 1, "distinct_repos": 1, "actionable": False,
               "actionable_reason": "r", "confidence": "low", "revised_statement": "s"},
    "final": {"records": {pr_id: {"change_summary": claim(pr_id)} for pr_id in URL_OF},
              "patterns": [{"pattern_id": "PAT-001", "confidence": {"level": "low", "evidence": ['%s "%s"' % (PR_OF[KEY], QUOTE)],
                                                                   "limitations": []}}],
              "extraction_ledger": "mock ledger", "limitations": ["mock"]},
}
FLAGS = {"extract": ["--lens", "correctness"], "synthesis": [], "verify": ["--expect-id", "C1"], "final": []}


def make(tmp, kind, answer=None, manifest_edit=None, report_text=None):
    run = tmp / "run"; (run / "result").mkdir(parents=True)
    text = report_text if report_text is not None else json.dumps(GOOD[kind] if answer is None else answer, ensure_ascii=False)
    (run / "result/report.md").write_text(text, encoding="utf-8")
    manifest = {"status": "succeeded", "profile": "claude-profile2", "exit_code": 0, "failures": [], "problems": [],
                "permission_mode": "dontAsk", "requested_model": "claude-opus-5-5", "requested_effort": "medium",
                "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                "observed_models": ["claude-opus-5-5"], "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                "report_sha256": hashlib.sha256(text.encode()).hexdigest()}
    if manifest_edit:
        manifest_edit(manifest)
    (run / "result/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp / "task.txt").write_text(PROMPT, encoding="utf-8")
    return run


def verdict(tmp, run, kind, extra=()):
    p = subprocess.run([sys.executable, "-B", str(VERIFY), "--run-dir", str(run), "--corpus", str(CORPUS),
                        "--prompt", str(tmp / "task.txt"), "--answer-kind", kind, *FLAGS[kind], *extra],
                       capture_output=True, text=True)
    if not p.stdout:
        return p.returncode, {"usage": p.stderr.strip()[-200:]}, {}
    doc = json.loads(p.stdout)
    items = {i["id"].split("-")[0]: i["status"] for i in doc["items"]}
    items["class"] = doc["result_class"]
    return p.returncode, items, doc


PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()
COMMON = {
    "good (strict)": (dict(), 0, {"M1": "PASS", "M5": "PASS", "P4": "PASS", "R3": "PASS"}),
    "prompt sha missing": (dict(manifest_edit=lambda m: m.pop("prompt_sha256")), 1, {"P4": "FAIL", "class": "invalid"}),
    "observed models missing (unknown)": (dict(manifest_edit=lambda m: m.pop("observed_models")), 1, {"M5": "FAIL", "class": "invalid"}),
    "observed other model": (dict(manifest_edit=lambda m: m.update(observed_models=["claude-opus-4-8"])), 1, {"M5": "FAIL"}),
    "observed mixed fallback": (dict(manifest_edit=lambda m: m.update(observed_models=["claude-opus-4-8", "claude-opus-5-5"])), 1, {"M5": "FAIL"}),
    "observed empty": (dict(manifest_edit=lambda m: m.update(observed_models=[])), 1, {"M5": "FAIL"}),
    "observed not a list": (dict(manifest_edit=lambda m: m.update(observed_models="claude-opus-5-5")), 1, {"M5": "FAIL"}),
    "problems nonempty": (dict(manifest_edit=lambda m: m.update(problems=["result is not a success"])), 1, {"M1": "FAIL"}),
    "failures nonempty": (dict(manifest_edit=lambda m: m.update(failures=["timeout"])), 1, {"M1": "FAIL"}),
    "failures missing": (dict(manifest_edit=lambda m: m.pop("failures")), 1, {"M1": "FAIL"}),
    "problems missing": (dict(manifest_edit=lambda m: m.pop("problems")), 1, {"M1": "FAIL"}),
    "exit code as bool": (dict(manifest_edit=lambda m: m.update(exit_code=False)), 1, {"M1": "FAIL"}),
    "exit code as string": (dict(manifest_edit=lambda m: m.update(exit_code="0")), 1, {"M1": "FAIL"}),
    "prompt sha recorded and different": (dict(manifest_edit=lambda m: m.update(prompt_sha256="0" * 64)), 1, {"P4": "FAIL", "class": "invalid"}),
    "failed run": (dict(manifest_edit=lambda m: m.update(status="failed", exit_code=1)), 1, {"M1": "FAIL", "class": "invalid"}),
    "refusal text": (dict(report_text="I can't help with that request."), 1, {"R2": "FAIL", "R3": "BLOCKED"}),
    "malformed json": (dict(report_text='{"candidates": ['), 1, {"R2": "FAIL"}),
    "fenced json": (dict(report_text="```json\n{}\n```"), 1, {"R2": "FAIL"}),
    "wrong model": (dict(manifest_edit=lambda m: m.update(requested_model="claude-opus-4-8")), 1, {"M2": "FAIL"}),
    # The live run is dontAsk (user approval, A's measured launch); auto is refused.
    "wrong mode": (dict(manifest_edit=lambda m: m.update(permission_mode="auto")), 1, {"M2": "FAIL"}),
    "wrong effort": (dict(manifest_edit=lambda m: m.update(requested_effort="high")), 1, {"M2": "FAIL"}),
    "staged profile": (dict(manifest_edit=lambda m: m.update(execution_profile="corpus-extract", staged={})), 1, {"M3": "FAIL"}),
    "credential not clean": (dict(manifest_edit=lambda m: m.update(credential={"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "failed"})), 1, {"M4": "FAIL"}),
    "report sha drift": (dict(manifest_edit=lambda m: m.update(report_sha256="0" * 64)), 1, {"R1": "FAIL"}),
}
reworded = QUOTE.replace("Updates", "Bumps")
SPECIFIC = {
    ("extract", "other lens"): (dict(answer={**GOOD["extract"], "lens": "blind"}), 1, {"R3": "FAIL"}),
    ("extract", "reworded quote"): (dict(answer={**GOOD["extract"], "observations": [{"pr": KEY, "note": "x", "quote": reworded}]}), 1, {"Q1": "FAIL"}),
    ("synthesis", "empty is insufficient"): (dict(), 0, {"Q1": "INFO", "class": "insufficient"}),
    ("synthesis", "unknown key"): (dict(answer={"candidates": [{"id": "C1", "statement": "s", "cited_prs": [KEY, "nope#1"]}], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "one PR"): (dict(answer={"candidates": [{"id": "C1", "statement": "s", "cited_prs": [KEY, KEY]}], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "six candidates"): (dict(answer={"candidates": [{"id": "C%d" % n, "statement": "s", "cited_prs": list(KEYS)[:2]} for n in range(6)], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "extra key"): (dict(answer={**GOOD["synthesis"], "note": 1}), 1, {"R3": "FAIL"}),
    ("synthesis", "statement too long"): (dict(answer={"candidates": [{"id": "C1", "statement": "s" * 601, "cited_prs": list(KEYS)[:2]}], "dropped": []}), 1, {"R3": "FAIL"}),
    # The stage output limits of run_limits.json (#248): the prompt states them, the strict check uses the same values.
    ("synthesis", "four candidates over synthesis_max_candidates 3"): (dict(answer={"candidates": [{"id": "C%d" % n, "statement": "s", "cited_prs": list(KEYS)[:2]} for n in range(1, 5)], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "candidate over candidate_max_bytes"): (dict(answer={"candidates": [{"id": "C1", "statement": "가" * 340, "cited_prs": list(KEYS)[:2]}], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "candidate id not C1 ... C99"): (dict(answer={"candidates": [{"id": "X1", "statement": "s", "cited_prs": list(KEYS)[:2]}], "dropped": []}), 1, {"R3": "FAIL"}),
    ("synthesis", "21 dropped ideas"): (dict(answer={"candidates": [], "dropped": [{"idea": "i", "reason": "r"}] * 21}), 1, {"R3": "FAIL"}),
    ("extract", "answer over extract_answer_max_bytes"): (dict(answer={**GOOD["extract"], "observations": [{"pr": KEY, "note": "n" * 7700, "quote": QUOTE}]}), 1, {"R3": "FAIL"}),
    ("verify", "answer over verify_answer_max_bytes"): (dict(answer={**GOOD["verify"], "actionable_reason": "r" * 6144}), 1, {"R3": "FAIL"}),
    ("verify", "answer over verify_answer_max_bytes 5120"): (dict(answer={**GOOD["verify"], "actionable_reason": "r" * 5000}), 1, {"R3": "FAIL"}),
    ("verify", "other candidate id"): (dict(answer={**GOOD["verify"], "id": "C2"}), 1, {"R3": "FAIL"}),
    ("verify", "no evidence checks"): (dict(answer={**GOOD["verify"], "evidence_checks": []}), 1, {"R3": "FAIL"}),
    ("verify", "reworded quote"): (dict(answer={**GOOD["verify"], "evidence_checks": [{"pr": KEY, "holds": True, "quote": reworded, "reason": "r"}]}), 1, {"Q1": "FAIL"}),
    ("final", "reworded pattern quote"): (dict(answer={**GOOD["final"], "patterns": [{"pattern_id": "PAT-001", "confidence": {"level": "low", "evidence": ['%s "%s"' % (PR_OF[KEY], reworded)], "limitations": []}}]}), 1, {"Q1": "FAIL"}),
    ("final", "missing record"): (dict(answer={**GOOD["final"], "records": {}}), 1, {"R3": "FAIL"}),
    ("final", "unknown PR quote"): (dict(answer={**GOOD["final"], "patterns": [{"pattern_id": "PAT-001", "confidence": {"level": "low", "evidence": ['PR-999 "%s"' % QUOTE], "limitations": []}}]}), 1, {"R3": "FAIL"}),
    ("final", "no pattern is insufficient"): (dict(answer={**GOOD["final"], "patterns": []}), 0, {"Q1": "PASS", "class": "insufficient"}),
    ("final", "six patterns"): (dict(answer={**GOOD["final"], "patterns": GOOD["final"]["patterns"] * 6}), 1, {"R3": "FAIL"}),
}
if len(KEYS) >= 2:
    SPECIFIC[("synthesis", "two keys is evidence")] = (dict(answer={"candidates": [{"id": "C1", "statement": "s", "cited_prs": list(KEYS)[:2]}], "dropped": []}), 0, {"class": "evidence"})
    SPECIFIC[("synthesis", "three candidates of 1024 bytes is evidence")] = (dict(answer={"candidates": [
        {"id": "C%d" % n, "statement": "가" * 300 + "a" * (1024 - 3 * 300 - len(json.dumps({"id": "C1", "statement": "", "cited_prs": list(KEYS)[:2]}, ensure_ascii=False, indent=1).encode("utf-8"))),
         "cited_prs": list(KEYS)[:2]} for n in range(1, 4)], "dropped": []}), 0, {"class": "evidence"})


def main():
    fails, count = 0, 0
    cases = [((kind, label), spec) for kind in GOOD for label, spec in COMMON.items()] + list(SPECIFIC.items())
    for (kind, label), (kw, code, expect) in cases:
        with tempfile.TemporaryDirectory(prefix="e2e-kinds-") as t:
            tmp = Path(t)
            rc, items, _ = verdict(tmp, make(tmp, kind, **kw), kind)
            ok = rc == code and all(items.get(k) == v for k, v in expect.items())
            fails += not ok; count += 1
            print("%s %s/%s rc=%d %s" % ("PASS" if ok else "FAIL", kind, label, rc, {k: items.get(k) for k in expect}))
    for kind in GOOD:
        with tempfile.TemporaryDirectory(prefix="e2e-kinds-") as t:
            tmp = Path(t)
            run = make(tmp, kind)
            target = tmp / (kind + ".result.json")
            rc, _, doc = verdict(tmp, run, kind, ["--adopt-to", str(target)])
            good = rc == 0 and target.read_bytes() == (run / "result/report.md").read_bytes() \
                and oct(os.stat(target).st_mode & 0o777) == "0o444"
            rc2, _, doc2 = verdict(tmp, run, kind, ["--adopt-to", str(target)])
            again = rc2 == 1 and "adopt_error" in doc2
            bad_run = make(Path(tempfile.mkdtemp(dir=t)), kind, manifest_edit=lambda m: m.update(status="failed"))
            refused = tmp / "refused.result.json"
            rc3, _, _ = verdict(bad_run.parent, bad_run, kind, ["--adopt-to", str(refused)])
            ok = good and again and rc3 == 1 and not refused.exists()
            fails += not ok; count += 1
            print("%s %s/adopt only verified bytes, never overwrite, never a failed run" % ("PASS" if ok else "FAIL", kind))
    legacy = RECORD / "run_limits.legacy-fixture.json"
    for kind in GOOD:
        with tempfile.TemporaryDirectory(prefix="e2e-kinds-") as t:
            tmp = Path(t)
            # The legacy mock shape: auto, and neither observed_models nor prompt_sha256.
            old = make(tmp, kind, manifest_edit=lambda m: (m.pop("observed_models"), m.pop("prompt_sha256"),
                                                           m.update(permission_mode="auto")))
            rc, items, doc = verdict(tmp, old, kind, ["--limits", str(legacy)])
            ok_legacy = rc == 0 and items.get("M5") == "INFO" and items.get("P4") == "INFO" and doc.get("limits") == "legacy-fixture"
            rc2, items2, _ = verdict(tmp, old, kind, ["--limits", str(legacy), "--adopt-to", str(tmp / "x.result.json")])
            no_adopt = rc2 == 2 and not (tmp / "x.result.json").exists()
            relaxed = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
            relaxed["require_observed_model"] = False
            (tmp / "relaxed.json").write_text(json.dumps(relaxed), encoding="utf-8")
            rc3, _, _ = verdict(tmp, old, kind, ["--limits", str(tmp / "relaxed.json")])
            bare = {k: v for k, v in json.loads((HERE / "run_limits.json").read_text(encoding="utf-8")).items()
                    if not k.startswith("require_")}
            (tmp / "bare.json").write_text(json.dumps(bare), encoding="utf-8")
            rc4, items4, doc4 = verdict(tmp, old, kind, ["--limits", str(tmp / "bare.json")])
            fail_closed = rc4 == 1 and items4.get("M5") == "FAIL" and items4.get("P4") == "FAIL" and doc4.get("limits") == "strict"
            # A switch of another type is refused (rc 2, nothing adopted), never a relaxed check.
            typed = True
            for key in ("require_observed_model", "require_prompt_sha256", "legacy_fixture"):
                for bad in (0, None, "", "false", 1):
                    odd = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
                    odd[key] = bad
                    (tmp / "odd.json").write_text(json.dumps(odd), encoding="utf-8")
                    target = tmp / "odd.result.json"
                    rc5, _, _ = verdict(tmp, old, kind, ["--limits", str(tmp / "odd.json"), "--adopt-to", str(target)])
                    typed = typed and rc5 == 2 and not target.exists()
            ok = ok_legacy and no_adopt and rc3 == 2 and fail_closed and typed
            fails += not ok; count += 1
            print("%s %s/limits: legacy only when marked, never adopts; relaxed unmarked refused; missing keys strict; non-bool switches refused "
                  "(legacy rc=%d, adopt rc=%d, relaxed rc=%d, bare rc=%d)" % ("PASS" if ok else "FAIL", kind, rc, rc2, rc3, rc4))
    print("cases=%d failures=%d (mock, corpus %s)" % (count, fails, CORPUS.name))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
