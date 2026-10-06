"""#286: every reason the strict check refuses an answer for is a rule the prompt of that answer states.

prompt_contract.py is the one table of strict's refusals by answer kind and the words each prompt states them with.
Here, without a model:

  S3  every row of the table is in every prompt it covers, built the way the stages build them (the analyzer's extract
      and verify prompts with stage_inputs' own limit options, the bundle's templates rendered from run_limits.json,
      the assess instructions with assess_contract's field types)
  --  every problem strict writes for a mutated answer (a key dropped or added, every JSON type in every field, a
      list grown past its limit, a text grown past its size, a key from outside the input) matches a row of its kind,
      and every row is reached: a refusal outside the table, or a row strict does not enforce, fails here
  S4  an answer written by the prompts' sentences at each limit (counts at the maximum, sizes at the byte, an empty
      result, a list-only counterexample, clues of 200 characters) passes the strict check, run as a process
  S2  the verify prompt states the 5,120-byte limit, how it is measured, and the 4,300-byte target

The answers are hand-written over the 2026-09-28 fixture corpus (two PRs). The strict check runs as a process (it
starts check_extraction), so this file does not install the offline guard; nothing here opens the network.

  /opt/homebrew/bin/python3 -B -m unittest skills/researching-open-source-contributions/analysis/test_prompt_contract.py
"""

from __future__ import annotations

import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ANALYZER = REPO / "skills/analyzing-open-source-pr-patterns/scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ANALYZER))
sys.dont_write_bytecode = True

import build_digest  # noqa: E402
import build_prompts  # noqa: E402
import check_extraction  # noqa: E402
import validate_corpus  # noqa: E402
import prompt_contract as contract  # noqa: E402
import stage_inputs  # noqa: E402
import verify_e2e_result as strict  # noqa: E402
import assess_contract  # noqa: E402
import machine_settings as machine  # noqa: E402

CORPUS = next(path for path in (machine.RECORDS_ROOT / "oss-e2e-2026-09-28/fixtures/corpus-min.json",
                                machine.RECORDS_ROOT / "oss-functional-validation-2026-09-28/raw/corpus.json")
              if path.exists())
LIMITS = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
RECORDS = build_digest.records_by_key(json.loads(CORPUS.read_text(encoding="utf-8")))
KEYS = sorted(RECORDS)
PR_ID = {key: record["pr_id"] for key, record in RECORDS.items()}
URL = {record["pr_id"]: record["pull_request"]["url"] for record in RECORDS.values()}


def quote_of(key: str, width: int = 60) -> str:
    """A verbatim quote of the PR: the start of its title (a title is quotable text)."""
    return validate_corpus.clean_text(RECORDS[key]["pull_request"]["title"])[:width].rstrip()


def run_main(main, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def compact(answer) -> bytes:
    """An answer the way a model writes it when told to write one line without indentation."""
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def grow(answer, setter, size, limit):
    """Pad one ASCII text of the answer (``setter(answer, pad)``) until ``size(answer)`` is exactly ``limit``."""
    for _ in range(3):
        trial = deepcopy(answer)
        setter(trial, "")
        missing = limit - size(trial)
        if missing < 0:
            raise AssertionError("the answer is already over %d bytes (%d)" % (limit, size(trial)))
        setter(trial, "x" * missing)
        if size(trial) == limit:
            return trial
        answer = trial
    raise AssertionError("could not pad the answer to exactly %d bytes" % limit)


# -- the prompts, built as the stages build them --------------------------------------------------------------------


class Prompts:
    """Every prompt of PROMPTS, from the fixture corpus (one shard for the digest paths, two for the reduced verify)."""

    def __init__(self, root: Path):
        self.root = root
        one, two = root / "digest-one", root / "digest-two"
        for out, argv in ((one, []), (two, ["--max-bytes", "8000"])):
            code, _, stderr = run_main(build_digest.main, [str(CORPUS), "--out", str(out), *argv])
            assert code == 0, stderr
        assert len(json.loads((two / "keymap.json").read_text())["shards"]) == 2
        candidate = root / "candidates.json"
        candidate.write_text(json.dumps([{"id": "C1", "statement": "s", "cited_prs": [KEYS[0]]}]), encoding="utf-8")
        self.text = {
            "extract": self.build(["extract", "--digest-dir", str(one), "--lens", "blind",
                                   *stage_inputs.extract_limit_args()], "extract-blind.md"),
            "verify": self.build(["verify", "--digest-dir", str(two), "--candidates", str(candidate),
                                  *stage_inputs.verify_limit_args()], "verify-C1.md"),
            "verify-digest": self.build(["verify", "--digest-dir", str(one), "--candidates", str(candidate),
                                         *stage_inputs.verify_limit_args()], "verify-C1.md"),
            "synthesis": stage_inputs.template("synthesize-reduced.md"),
            "synthesis-digest": stage_inputs.template("synthesize.md"),
            "merge": stage_inputs.template("synthesize-merge.md"),
            "final": stage_inputs.template("finalize-reduced.md"),
            "final-digest": stage_inputs.template("finalize.md"),
            "assess": stage_inputs.assess_instructions(strict.load_verifier()),
        }
        assert set(self.text) == set(contract.PROMPTS)

    def build(self, argv, name):
        out = self.root / ("build-%d" % len(list(self.root.glob("build-*"))))
        code, _, stderr = run_main(build_prompts.main, argv[:1] + ["--out", str(out)] + argv[1:])
        assert code == 0, stderr
        return (out / "prompts" / name).read_text(encoding="utf-8")


def filled() -> dict:
    found = contract.values(strict, validate_corpus)
    found["assessment_field_types"] = assess_contract.prompt_lines(strict.load_verifier())
    return found


class ContractCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="prompt-contract-"))
        cls.prompts = Prompts(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)


class S3PromptStatesEveryRefusal(ContractCase):
    def test_every_row_is_in_every_prompt_it_covers(self):
        values = filled()
        counts = {}
        for name, text in self.prompts.text.items():
            rules = contract.rules_for(name)
            counts[name] = len(rules)
            self.assertTrue(rules, name)
            for rule in rules:
                for phrase in contract.words(rule, values):
                    with self.subTest(prompt=name, rule=rule.name):
                        self.assertIn(phrase, text)
        print("\nS3 rows per prompt: %s" % json.dumps(counts, ensure_ascii=False))

    def test_the_numbers_come_from_the_one_source(self):
        values = contract.values(strict, validate_corpus)
        for key in ("extract_answer_max_bytes", "extract_answer_target_bytes", "verify_answer_max_bytes",
                    "verify_answer_target_bytes", "candidate_max_bytes", "candidate_statement_max_chars",
                    "synthesis_max_candidates", "max_patterns", "max_observations", "max_rejected"):
            self.assertEqual(LIMITS[key], values[key], key)
        self.assertLess(LIMITS["verify_answer_target_bytes"], LIMITS["verify_answer_max_bytes"])
        self.assertEqual((stage_inputs.VERIFY_ANSWER_MAX_BYTES, stage_inputs.VERIFY_ANSWER_TARGET_BYTES),
                         (LIMITS["verify_answer_max_bytes"], LIMITS["verify_answer_target_bytes"]))
        # the prompt asks for shorter quotes than the check allows, never longer
        self.assertLessEqual(contract.QUOTE_PROMPT_MAX, validate_corpus.MAX_QUOTE_LENGTH)
        # no literal limit left in a template: each is a {{key}} rendered from run_limits.json
        for name in ("synthesize-reduced.md", "synthesize.md", "synthesize-merge.md", "finalize-reduced.md",
                     "finalize.md"):
            raw = (HERE / "prompts" / name).read_text(encoding="utf-8")
            self.assertNotIn("최대 %d개다" % LIMITS["max_patterns"], raw, name)
            self.assertNotRegex(raw, r"\b(1024|5120|600|7680)\b", name)

    def test_every_row_names_a_known_prompt_kind_and_item(self):
        names = [rule.name for rule in contract.RULES]
        self.assertEqual(len(names), len(set(names)))
        for rule in contract.RULES:
            self.assertTrue(set(rule.kinds) <= set(contract.kinds()), rule.name)
            self.assertRegex(rule.item, r"^(R3|R5|R6|Q1|A1|A2|A4)(\|(R3|R5|R6|Q1|A1|A2|A4))*$", rule.name)
            re.compile(rule.refusal)


# -- what strict refuses, as its own functions judge it -------------------------------------------------------------


def problems_of(kind: str, answer, scope=None, expect_id="C1", lens="blind", report=None) -> list:
    """Every problem strict writes for an answer's content, as (item, text): R3, then R5/R6 when R3 passes (the order
    verify() judges them in), and each quote check_extraction refuses as Q1 (reason)."""
    if kind == "assess":
        return assess_problems_of(answer, scope)
    report = compact(answer) if report is None else report
    found, quote_doc, _, _ = strict.content_problems(kind, answer, report, LIMITS, RECORDS, lens, expect_id, scope)
    items = [(item, text) for item, texts in found.items() for text in texts]
    if not found["R3"] and kind != "synthesis" and not found.get("R5"):
        result, _ = check_extraction.check_result("answer", quote_doc, RECORDS)
        items += [("Q1", failure["reason"]) for failure in result["failures"]]
    return items


ASSESS_SCOPE = {"kind": "assess", "discovery_sha256": "0" * 64,
                "combinations": [{"pattern_id": "PAT-001", "repository": "acme/widgets"}], "max_assessments": 10,
                "links": ["https://github.com/acme/widgets"], "policy": [
                    {"key": "contributing", "source": "acme/widgets:CONTRIBUTING.md", "sha256": "1" * 64}],
                "policy_evidence_sha256": None}


def assess_problems_of(answer, scope=None) -> list:
    """R3 of an assess answer, then (when it passes) A1, R5, A2 and A4: the items that judge the answer alone (A3 runs
    the verifier's record gate; test_assess covers it)."""
    scope = scope or ASSESS_SCOPE
    r3 = strict.assess_problems(answer, scope, strict.load_verifier())
    items = [("R3", text) for text in r3]
    if r3:
        return items
    if answer["discovery_sha256"] != scope["discovery_sha256"]:
        items.append(("A1", "answer discovery_sha256 %r is not the input's %s"
                      % (answer["discovery_sha256"], scope["discovery_sha256"])))
    links, policy = strict.assess_scope_problems(answer, scope)
    return items + [("R5", text) for text in links] + [("A2", text) for text in policy] \
        + [("A4", text) for text in strict.assess_rule_problems(answer)]


def matching(kind: str, item: str, text: str) -> list:
    return [rule.name for rule in contract.RULES
            if kind in rule.kinds and item in rule.item.split("|") and re.fullmatch(rule.refusal, text, re.S)]


# -- answers at the limits, written by the prompts' sentences --------------------------------------------------------


def extract_answer() -> dict:
    """The lens object at every count limit and exactly at the size limit (indent-1 JSON)."""
    k0, k1 = KEYS[0], KEYS[1]
    pattern = {"title": "t", "description": "d", "kind": "maintenance-policy",
               "evidence": [{"pr": k0, "role": "supports", "quote": quote_of(k0), "claim": "c"},
                            {"pr": k1, "role": "counterexample", "quote": quote_of(k1), "claim": "c"}],
               "applicability": [], "counterconditions": [], "search_clues": ["editorconfig-checker"],
               "expected_tests": [], "maintainer_judgment_required": [], "confidence": "low", "confidence_reason": "r"}
    answer = {"lens": "blind", "patterns": [deepcopy(pattern) for _ in range(LIMITS["max_patterns"])],
              "observations": [{"pr": KEYS[i % 2], "note": "", "quote": quote_of(KEYS[i % 2], 20)}
                               for i in range(LIMITS["max_observations"])],
              "rejected": [{"idea": "", "reason": ""} for _ in range(LIMITS["max_rejected"])]}
    return grow(answer, lambda a, pad: a["patterns"][0].update(description=pad), strict.answer_bytes,
                LIMITS["extract_answer_max_bytes"])


def synthesis_answer() -> dict:
    """Three candidates, each exactly at the candidate size, the statement at its character limit, dropped at its
    limit; every cited key one the input held."""
    def candidate(number):
        item = {"id": "C%d" % number, "statement": "s" * LIMITS["candidate_statement_max_chars"],
                "cited_prs": list(KEYS), "found_by": ""}
        return grow(item, lambda c, pad: c.update(found_by=pad), strict.answer_bytes, LIMITS["candidate_max_bytes"])

    return {"candidates": [candidate(n) for n in range(1, LIMITS["synthesis_max_candidates"] + 1)],
            "dropped": [{"idea": "i", "reason": "r"} for _ in range(LIMITS["max_rejected"])]}


SCOPE = {"keys": [KEYS[0]], "listed": [KEYS[1]]}       # a verify prompt with KEYS[0]'s section and KEYS[1] in the list


def verify_answer() -> dict:
    """One quoted check, a quoted counterexample of a section PR and a list-only counterexample without a quote, at
    exactly the size limit as written (one line)."""
    answer = {"id": "C1", "verdict": "weak",
              "evidence_checks": [{"pr": KEYS[0], "holds": True, "quote": quote_of(KEYS[0]), "reason": "r"}],
              "counterexamples": [{"pr": KEYS[0], "quote": quote_of(KEYS[0], 30), "reason": "r"},
                                  {"pr": KEYS[1], "reason": "목록에만 있다"}],
              "distinct_prs": 1, "distinct_repos": 1, "actionable": False, "actionable_reason": "r",
              "confidence": "low", "revised_statement": ""}
    return grow(answer, lambda a, pad: a.update(revised_statement=pad), lambda a: len(compact(a)),
                LIMITS["verify_answer_max_bytes"])


def final_answer() -> tuple:
    """Every PR's record, the maximum of patterns, each with a quote entry and search clues at their limit. Returns
    (answer, the quotes its input listed)."""
    def claim(pr_id, basis="fact"):
        return {"value": "v", "basis": basis, "evidence_links": [URL[pr_id]]} if basis != "unknown" \
            else {"value": None, "basis": "unknown", "evidence_links": []}

    records = {pr_id: {"change_summary": claim(pr_id), "motivation": claim(pr_id, "unknown"),
                       "review_judgment": claim(pr_id, "inference"), "closure_reason": claim(pr_id, "unknown"),
                       "test_evidence": [], "pattern_ids": [], "evidence_links": [URL[pr_id]],
                       "provenance_mode": "independent-reimplementation",
                       "confidence": {"level": "low", "evidence": ["e"], "limitations": ["l"]}} for pr_id in URL}
    quotes = [{"pr": key, "quote": quote_of(key)} for key in KEYS]
    patterns = []
    for number in range(1, LIMITS["max_patterns"] + 1):
        patterns.append({"pattern_id": "PAT-%03d" % number, "description": "d", "evidence_pr_ids": sorted(URL),
                         "applicability": ["a"], "counterconditions": [], "expected_tests": [],
                         "search_clues": ["a" * strict.SEARCH_CLUE_MAX_CHARS, "## Options", "kangax.github.io"],
                         "maintainer_judgment_required": [], "provenance_mode": "independent-reimplementation",
                         "confidence": {"level": "low", "limitations": ["l"], "evidence": [
                             '%s "%s"' % (PR_ID[item["pr"]], item["quote"]) for item in quotes]}})
    answer = {"records": records, "patterns": patterns, "extraction_ledger": "digest 1 shard; candidates C1",
              "limitations": ["l"]}
    return answer, quotes


def assess_answer() -> dict:
    """The conservative assessment the prompt asks for when the input proves nothing."""
    vc = strict.load_verifier()
    return {"schema_version": "1.0.0", "discovery_sha256": ASSESS_SCOPE["discovery_sha256"], "assessments": [{
        "pattern_id": "PAT-001", "repository": "acme/widgets", "locus": "", "status": "unverified",
        "status_reason": "insufficient-evidence", "sensitivity": "normal", "summary": "s", "impact": "i",
        "readiness_checks": {key: {"result": "not-confirmed", "evidence_links": [], "note": "n"}
                             for key in vc.READINESS_KEYS},
        "duplicate_verdict": {"matched_items": [], "judgment": ""}, "ai_policy_status": "unknown",
        "disclosure_required": None, "private_evidence_reference": None,
        "evidence_links": ["https://github.com/acme/widgets"], "execution_evidence": None,
        "reproduction": {"method": "none", "evidence_links": [], "public_steps": None},
        "policy_checks": {key: {"found": None, "source": None, "sha256": None, "assessment": "a", "evidence_links": []}
                          for key in vc.POLICY_CHECK_KEYS},
        "blocking_gaps": ["g"], "superseded_by": None}]}


def boundary(kind: str) -> tuple:
    """(answer, scope) at the limits of a kind."""
    if kind == "extract":
        return extract_answer(), None
    if kind == "synthesis":
        return synthesis_answer(), {"keys": list(KEYS)}
    if kind == "verify":
        return verify_answer(), dict(SCOPE)
    if kind == "final":
        answer, quotes = final_answer()
        return answer, {"keys": list(KEYS), "quotes": quotes}
    return assess_answer(), ASSESS_SCOPE


# -- mutations ------------------------------------------------------------------------------------------------------

ODD_VALUES = (None, True, False, 0, -1, 1.5, "", " ", "x", "C0", [], [""], [0], {}, {"x": 1})


def paths(value, at=()):
    yield at
    if isinstance(value, dict):
        for key in value:
            yield from paths(value[key], at + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value[:2]):
            yield from paths(item, at + (index,))


def get(value, at):
    for part in at:
        value = value[part]
    return value


def mutations(answer):
    """Generic mutations of an answer: every value replaced by every odd value, every key dropped, an extra key in
    every object, every list grown by one copy of its last item and by many, every text grown long."""
    for at in paths(answer):
        if at:
            for odd in ODD_VALUES:
                changed = deepcopy(answer)
                get(changed, at[:-1])[at[-1]] = deepcopy(odd)
                yield "%s=%r" % (list(at), odd), changed
        target = get(answer, at)
        if isinstance(target, dict):
            for key in list(target):
                changed = deepcopy(answer)
                del get(changed, at)[key]
                yield "drop %s" % list(at + (key,)), changed
            changed = deepcopy(answer)
            get(changed, at)["extra"] = "x"
            yield "extra key in %s" % list(at), changed
        if isinstance(target, list) and target:
            for times in (1, 30):
                changed = deepcopy(answer)
                get(changed, at).extend(deepcopy(target[-1]) for _ in range(times))
                yield "grow %s by %d" % (list(at), times), changed
        if isinstance(target, str) and at:
            changed = deepcopy(answer)
            get(changed, at[:-1])[at[-1]] = target + "y" * 6000
            yield "long %s" % list(at), changed


KEEP = object()


def targeted(kind: str):
    """Mutations a generic one does not reach: values from outside the input, quotes that are not the source, ids
    and verdicts out of their sets, Hangul and control characters in clues."""
    answer, scope = boundary(kind)
    cases = []

    def case(label, edit, new_scope=KEEP):
        changed = deepcopy(answer)
        edit(changed)
        cases.append((label, changed, scope if new_scope is KEEP else new_scope))

    if kind == "extract":
        case("other lens", lambda a: a.update(lens="correctness"))
        # reworded in the same length: the answer stays at its size limit, so the quote check is what refuses it
        case("reworded quote", lambda a: a["observations"][0].update(quote=quote_of(KEYS[0], 20).replace("chore", "chord")))
        case("unknown key", lambda a: a["observations"][0].update(pr="nope#1"))
        case("short quote", lambda a: a["observations"][0].update(quote=quote_of(KEYS[0], 10)))
        case("empty quote", lambda a: a["observations"][0].update(quote=""))
    if kind == "synthesis":
        case("key outside the input", lambda a: a, {"keys": [KEYS[0]]})
        case("one cited key", lambda a: a["candidates"][0].update(cited_prs=[KEYS[0], KEYS[0]]))
        case("long statement", lambda a: a["candidates"][0].update(
            statement="s" * (LIMITS["candidate_statement_max_chars"] + 1), found_by=""))
        case("id C9 twice", lambda a: [c.update(id="C9") for c in a["candidates"]])
    if kind == "verify":
        case("other id", lambda a: a.update(id="C2"))
        case("over the size by one", lambda a: a.update(revised_statement=a["revised_statement"] + "x"))
        case("the 2026-10-06 empty pr", lambda a: a["counterexamples"].__setitem__(1, {"pr": "", "quote": "",
                                                                                       "reason": "r"}))
        case("a listed key with an empty quote", lambda a: a["counterexamples"][1].update(quote=""))
        case("a key neither in a section nor listed", lambda a: a["counterexamples"][1].update(pr="nope#1"))
        case("a section key without a quote", lambda a: a["counterexamples"][1].update(pr=KEYS[0]))
        case("a listed key quoted", lambda a: a["counterexamples"][1].update(quote=quote_of(KEYS[1])))
        case("a check of a listed key", lambda a: a["evidence_checks"][0].update(pr=KEYS[1], quote=quote_of(KEYS[1])))
        case("no list: an unquoted counterexample", lambda a: a, {"keys": [KEYS[0]]})
        case("reworded quote", lambda a: a["evidence_checks"][0].update(quote=quote_of(KEYS[0]).replace("bump", "jump")))
        case("empty quote", lambda a: a["evidence_checks"][0].update(quote=""))
        case("unknown key", lambda a: a["evidence_checks"][0].update(pr="nope#1"))
        case("short quote", lambda a: a["evidence_checks"][0].update(quote=quote_of(KEYS[0], 10)))
    if kind == "final":
        entry = answer["patterns"][0]["confidence"]["evidence"][0]
        case("quote of an unknown PR", lambda a: a["patterns"][0]["confidence"]["evidence"].__setitem__(
            0, entry.replace(PR_ID[KEYS[0]], "PR-999")))
        case("quote not listed", lambda a: a["patterns"][0]["confidence"]["evidence"].__setitem__(
            0, entry[:-2] + '"'))
        case("escaped quote", lambda a: a, {"keys": list(KEYS), "quotes": [
            {"pr": KEYS[0], "quote": 'a "b'}]})
        case("escaped quote listed", lambda a: a["patterns"][0]["confidence"]["evidence"].__setitem__(
            0, '%s "a \\"b"' % PR_ID[KEYS[0]]), {"keys": list(KEYS), "quotes": [{"pr": KEYS[0], "quote": 'a "b'}]})
        case("no quote entry", lambda a: [p["confidence"].update(evidence=["e"]) for p in a["patterns"]])
        case("reworded quote", lambda a: a["patterns"][0]["confidence"]["evidence"].__setitem__(
            0, entry.replace("bump", "bumps")), None)
        for label, clue in (("Hangul clue", "규칙 문서"), ("tab clue", "a\tb"), ("quote clue", 'a"b'),
                            ("backslash clue", "a\\b"), ("edge clue", " a"), ("long clue", "a" * 201)):
            case(label, lambda a, clue=clue: a["patterns"][0]["search_clues"].__setitem__(0, clue))
    if kind == "assess":
        case("other discovery", lambda a: a.update(discovery_sha256="f" * 64))
        case("other combination", lambda a: a["assessments"][0].update(repository="acme/other"))
        case("no assessment of a combination", lambda a: a, dict(ASSESS_SCOPE, combinations=ASSESS_SCOPE["combinations"]
                                                                  + [{"pattern_id": "PAT-002", "repository": "acme/w"}]))
        case("link outside", lambda a: a["assessments"][0].update(evidence_links=["https://example.com/x"]))
        case("link in a sentence", lambda a: a["assessments"][0].update(summary="see https://example.com/x"))
        case("policy bytes outside", lambda a: a["assessments"][0]["policy_checks"]["contributing"].update(
            found=True, source="acme/widgets:CONTRIBUTING.md", sha256="2" * 64))
        case("source not found", lambda a: a["assessments"][0]["policy_checks"]["contributing"].update(
            found=False, source="acme/widgets:CONTRIBUTING.md"))
        case("program rules digest", lambda a: a["assessments"][0]["policy_checks"]["program_rules"].update(
            sha256="1" * 64))
        case("executed", lambda a: a["assessments"][0].update(reproduction={
            "method": "executed", "evidence_links": [], "public_steps": None}))
        case("execution evidence", lambda a: a["assessments"][0].update(execution_evidence={}))
        case("private reference", lambda a: a["assessments"][0].update(private_evidence_reference="x"))
        case("bad sensitivity", lambda a: a["assessments"][0].update(sensitivity="secret"))
        case("bad superseded", lambda a: a["assessments"][0].update(superseded_by="X"))
        case("too many", lambda a: a.update(assessments=a["assessments"] * 11))
    return cases


class EveryRefusalIsARow(unittest.TestCase):
    """No problem strict writes falls outside the table, and every row is one strict enforces."""

    def test_every_refusal_matches_a_row_of_its_kind_and_every_row_is_reached(self):
        reached, counts = set(), {}
        for kind in contract.kinds():
            answer, scope = boundary(kind)
            self.assertEqual([], problems_of(kind, answer, scope), kind)
            cases = [(label, changed, scope) for label, changed in mutations(answer)] + targeted(kind)
            counts[kind] = len(cases)
            for label, changed, case_scope in cases:
                for item, text in problems_of(kind, changed, case_scope):
                    rows = matching(kind, item, text)
                    with self.subTest(kind=kind, mutation=label, item=item):
                        self.assertTrue(rows, "%s %s: %s" % (kind, item, text[:300]))
                    reached.update(rows)
        unreached = [rule.name for rule in contract.RULES if rule.name not in reached]
        self.assertEqual([], unreached)
        print("\nmutations per kind: %s; rows reached %d/%d" % (json.dumps(counts), len(reached), len(contract.RULES)))

    def test_rows_per_kind(self):
        counts = {kind: len([rule for rule in contract.RULES if kind in rule.kinds]) for kind in contract.kinds()}
        print("\nrows per kind: %s" % json.dumps(counts))
        self.assertTrue(all(counts.values()))


# -- S4: the boundary answers pass the strict check, run as a process -----------------------------------------------


def strict_run(root: Path, corpus: Path, kind, answer_bytes: bytes, prompt: str, scope=None, extra=()) -> tuple:
    """The strict check as a process on a collected result tree made for this answer: (exit code, verdict)."""
    tmp = Path(tempfile.mkdtemp(dir=root))
    run = tmp / "run"
    (run / "result").mkdir(parents=True)
    (run / "result/report.md").write_bytes(answer_bytes)
    (tmp / "task.txt").write_text(prompt, encoding="utf-8")
    manifest = {"status": "succeeded", "profile": "claude-profile2", "exit_code": 0, "failures": [], "problems": [],
                "permission_mode": "dontAsk", "requested_model": "claude-opus-5-5", "requested_effort": "medium",
                "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                "observed_models": ["claude-opus-5-5"],
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "report_sha256": hashlib.sha256(answer_bytes).hexdigest()}
    (run / "result/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    argv = [sys.executable, "-B", str(HERE / "verify_e2e_result.py"), "--run-dir", str(run), "--corpus",
            str(corpus), "--prompt", str(tmp / "task.txt"), "--answer-kind", kind, *extra]
    if scope is not None:
        (tmp / "scope.json").write_text(json.dumps(scope), encoding="utf-8")
        argv += ["--input-scope", str(tmp / "scope.json")]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert done.stdout, done.stderr
    return done.returncode, json.loads(done.stdout)


class S4BoundaryAnswersPass(ContractCase):
    def strict_run(self, kind, answer_bytes: bytes, prompt: str, scope=None, extra=()):
        return strict_run(self.root, CORPUS, kind, answer_bytes, prompt, scope, extra)

    def assert_pass(self, kind, answer, prompt, scope=None, extra=(), result_class="evidence", raw=None):
        code, verdict = self.strict_run(kind, raw if raw is not None else compact(answer), prompt, scope, extra)
        failed = {item["id"]: item["detail"] for item in verdict["items"] if item["status"] in ("FAIL", "BLOCKED")}
        self.assertEqual((0, {}, result_class), (code, failed, verdict["result_class"]), kind)

    def test_answers_at_every_limit_pass(self):
        text = self.prompts.text
        answer = extract_answer()
        self.assertEqual(LIMITS["extract_answer_max_bytes"], strict.answer_bytes(answer))
        self.assert_pass("extract", answer, text["extract"], extra=["--lens", "blind"])
        answer = synthesis_answer()
        self.assertEqual({LIMITS["candidate_max_bytes"]}, {strict.answer_bytes(c) for c in answer["candidates"]})
        for name in ("synthesis", "merge"):
            self.assert_pass("synthesis", answer, text[name], {"keys": list(KEYS)})
        self.assert_pass("synthesis", answer, text["synthesis-digest"])
        answer = verify_answer()
        self.assertEqual(LIMITS["verify_answer_max_bytes"], len(compact(answer)))
        self.assert_pass("verify", answer, text["verify"], dict(SCOPE), ["--expect-id", "C1"])
        answer, quotes = final_answer()
        self.assert_pass("final", answer, text["final"], {"keys": list(KEYS), "quotes": quotes})
        self.assert_pass("final", answer, text["final-digest"])

    def test_empty_results_the_prompts_call_normal_pass_as_insufficient(self):
        text = self.prompts.text
        self.assert_pass("extract", {"lens": "blind", "patterns": [], "observations": [], "rejected": []},
                         text["extract"], extra=["--lens", "blind"], result_class="insufficient")
        self.assert_pass("synthesis", {"candidates": [], "dropped": []}, text["synthesis"], {"keys": list(KEYS)},
                         result_class="insufficient")
        answer, _ = final_answer()
        answer.update(patterns=[])
        self.assert_pass("final", answer, text["final"], {"keys": list(KEYS), "quotes": []},
                         result_class="insufficient")
        # a verify answer with no counterexample, written with the indentation a model may still use
        answer = verify_answer()
        answer.update(counterexamples=[], revised_statement="s")
        self.assert_pass("verify", answer, text["verify-digest"], extra=["--expect-id", "C1"],
                         raw=json.dumps(answer, ensure_ascii=False, indent=2).encode("utf-8"))

    def test_one_byte_over_a_limit_fails_where_the_prompt_says_it_does(self):
        answer = verify_answer()
        answer["revised_statement"] += "x"
        code, verdict = self.strict_run("verify", compact(answer), self.prompts.text["verify"], dict(SCOPE),
                                        ["--expect-id", "C1"])
        r3 = next(item for item in verdict["items"] if item["id"] == "R3-report-schema")
        self.assertEqual((1, "FAIL"), (code, r3["status"]))
        self.assertIn("verify answer is %d bytes (> verify_answer_max_bytes %d)"
                      % (LIMITS["verify_answer_max_bytes"] + 1, LIMITS["verify_answer_max_bytes"]), r3["detail"])


class S2VerifyPromptStatesTheSize(ContractCase):
    def test_the_verify_prompt_states_the_limit_its_measure_and_the_target(self):
        for name in ("verify", "verify-digest"):
            text = self.prompts.text[name]
            self.assertEqual(1, text.count("%d 바이트 이하여야 한다" % LIMITS["verify_answer_max_bytes"]), name)
            self.assertEqual(1, text.count("%d 바이트 이하로 써라" % LIMITS["verify_answer_target_bytes"]), name)
            self.assertIn("공백·줄바꿈 포함", text)
            self.assertIn("들여쓰기·줄바꿈 없이 한 줄로", text)
        self.assertIn("목록에만 있는 PR 은 본문이 없어 인용할 수 없다", self.prompts.text["verify"])
        self.assertNotIn("목록에만 있는 PR", self.prompts.text["verify-digest"])

    def test_without_the_limit_options_the_analyzer_writes_the_verify_prompt_as_before(self):
        candidate = self.root / "c2.json"
        candidate.write_text(json.dumps([{"id": "C1", "statement": "s", "cited_prs": [KEYS[0]]}]), encoding="utf-8")
        text = self.prompts.build(["verify", "--digest-dir", str(self.root / "digest-one"), "--candidates",
                                   str(candidate)], "verify-C1.md")
        self.assertNotIn("답 크기", text)
        self.assertNotIn("evidence_checks` 는 인용된 PR 마다", text)



FRESH = machine.VM_LOGS / "a78-split-gate-fresh-20261006T081016Z"


@unittest.skipUnless((FRESH / "ledger/verify-c1-r2/result/report.md").is_file() and (FRESH / "wf-run/state.json").is_file(),
                     "the 2026-10-06 split gate fresh run is only on the machine that made it")
class S1SplitGateFreshVerifyAnswers(ContractCase):
    """S1: the two verify answers the fresh run refused, read from its evidence (never changed), against the verify
    inputs verify-inputs writes now for that run (rebuilt from the same collections in a temporary directory).

    verify-c1 (5,079 bytes) wrote its list-only suspicion as {"pr": "", "quote": "", "reason": "...#12709, #12910..."}
    as its prompt then told it (keys in the reason only): R5 still refuses the empty pr. Written as the prompt now says,
    one quote-less counterexample per listed key, the same answer passes. A key neither listed nor in a section, a
    section key without a quote and a listed key with an (empty) quote still fail. verify-c1-r2 (5,636 bytes, over
    5,120) still fails R3: the check is the same."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        wf = stage_inputs.load_workflow()
        state = json.loads((FRESH / "wf-run/state.json").read_text(encoding="utf-8"))
        inputs = state["stages"]["collect"]["generations"][0]["inputs"]
        adopt = []
        for name in sorted(key for key in inputs if key.startswith("corpus-")):
            unpacked = Path(inputs[name]["source_path"]).parent
            adopt += ["--corpus", str(unpacked / "corpus.json"), "--manifest",
                      str(unpacked / "collection-manifest.json"), "--inventory", str(unpacked / "inventory.md")]
        run = str(cls.root / "run")
        for argv in (("init", "--request", str(FRESH / "request.json")), ("adopt-collection", *adopt),
                     ("digest", "--max-bytes", "54000")):
            code, _, stderr = wf.run_main(wf.main, [argv[0], "--run-dir", run, *argv[1:]])
            assert code == 0, stderr
        extract = [item for path in sorted((FRESH / "answers").glob("extract-*.result.json"))
                   for item in ("--extract-result", str(path))]
        code, _, stderr = wf.run_main(stage_inputs.main, ["verify-inputs", "--run-dir", run, "--synthesis",
                                                          str(FRESH / "answers/synthesis.result.json"), *extract,
                                                          "--out", str(cls.root / "v")])
        assert code == 0, stderr
        cls.corpus = stage_inputs.Context(run).corpus_path
        cls.prompt = (cls.root / "v/verify-C1/task.txt").read_text(encoding="utf-8")
        cls.scope = json.loads((cls.root / "v/verify-C1.scope.json").read_text(encoding="utf-8"))

    def verdict(self, answer_bytes):
        code, verdict = strict_run(self.root, self.corpus, "verify", answer_bytes, self.prompt, self.scope,
                                   ["--expect-id", "C1"])
        return code, {item["id"].split("-")[0]: (item["status"], item["detail"]) for item in verdict["items"]}

    def test_the_new_scope_lists_the_prs_the_prompt_lists_by_title(self):
        self.assertEqual(8, len(self.scope["keys"]))
        self.assertEqual(13, len(self.scope["listed"]))
        for key in ("typescript-eslint#12709", "typescript-eslint#12910"):
            self.assertIn(key, self.scope["listed"])
            self.assertIn("- %s — " % key, self.prompt)
        old = (FRESH / "in-verify/verify-C1/task.txt").read_text(encoding="utf-8")
        old_scope = json.loads((FRESH / "in-verify/verify-C1.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(old_scope["keys"], self.scope["keys"])
        # the prompt the run called, with only the two #286 changes: the list-only sentence and the verify rules
        rules = (build_prompts.VERIFY_RULES.format(answer_bytes=LIMITS["verify_answer_max_bytes"])
                 + build_prompts.VERIFY_TARGET_RULE.format(target_bytes=LIMITS["verify_answer_target_bytes"]))
        before = "목록에만 있는 PR 은 인용할 수 없으니 반례로 의심되면 counterexamples 의 reason 에 키만 적어라."
        after = build_prompts.LISTED_SCOPE.split("싣는다. ", 1)[1]
        self.assertEqual(old, self.prompt.replace(rules, "", 1).replace(after, before, 1))

    def test_the_answers_the_run_refused_still_fail(self):
        code, items = self.verdict((FRESH / "ledger/verify-c1/result/report.md").read_bytes())
        self.assertEqual((1, "PASS", "FAIL"), (code, items["R3"][0], items["R5"][0]))
        self.assertEqual('names PRs whose section was not in its prompt: ""', items["R5"][1])
        code, items = self.verdict((FRESH / "ledger/verify-c1-r2/result/report.md").read_bytes())
        self.assertEqual((1, "FAIL"), (code, items["R3"][0]))
        self.assertIn("verify answer is 5636 bytes (> verify_answer_max_bytes 5120)", items["R3"][1])

    def rewritten(self, counterexamples):
        answer = json.loads((FRESH / "ledger/verify-c1/result/report.md").read_text(encoding="utf-8"))
        answer["counterexamples"] = counterexamples
        return answer

    def test_the_same_answer_written_as_the_prompt_now_says_passes(self):
        listed = [{"pr": "typescript-eslint#12709", "reason": "목록에만 있다. 제목상 작은 문서 수정이지만 closed-unmerged다."},
                  {"pr": "typescript-eslint#12910", "reason": "목록에만 있다. 제목상 작은 문서 수정이지만 closed-unmerged다."}]
        answer = self.rewritten(listed)
        self.assertLessEqual(len(compact(answer)), LIMITS["verify_answer_max_bytes"])     # 4,548 with its own reasons
        code, items = self.verdict(compact(answer))
        self.assertEqual((0, "PASS", "PASS", "PASS"), (code, items["R3"][0], items["R5"][0], items["Q1"][0]),
                         {key: value[1] for key, value in items.items() if value[0] != "PASS"})
        self.assertIn("8/8 verified", items["Q1"][1])
        # the same with the indentation the run's answer used (5,079 bytes there) is within the limit as well
        self.assertEqual(0, self.verdict(json.dumps(answer, ensure_ascii=False, indent=2).encode("utf-8"))[0])
        refused = {
            "a key neither listed nor in a section": [dict(listed[0], pr="typescript-eslint#99999")],
            "an empty key": [dict(listed[0], pr="")],
            "a section key without a quote": [dict(listed[0], pr="typescript-eslint#12657")],
            "a listed key with an empty quote (verify-c1-r2's form)": [dict(listed[0], quote="")],
        }
        for label, counterexamples in refused.items():
            with self.subTest(label):
                code, items = self.verdict(compact(self.rewritten(counterexamples)))
                self.assertEqual((1, "FAIL"), (code, items["R5"][0]), items["R5"][1])

if __name__ == "__main__":
    unittest.main()
