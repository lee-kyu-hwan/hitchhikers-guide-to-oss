#!/usr/bin/env python3
"""#78 actual E2E: verify one isolated model run's collected result on the host.

  verify_e2e_result.py --run-dir RUN --corpus CORPUS --prompt TASK
      [--answer-kind extract|synthesis|verify|final] [--lens LENS] [--expect-id CANDIDATE]
      [--limits LIMITS] [--out VERDICT] [--adopt-to ANSWER.result.json] [--input-scope SCOPE.json]
  verify_e2e_result.py --run-dir RUN --discovery DISCOVERY --prompt TASK --answer-kind assess
      --input-scope assess.scope.json [--policy-evidence P] [--locus-search L] [--limits ...] [--out ...] [--adopt-to ...]

Every model call of the E2E (5 extract, 1 synthesis, k verify, 1 final) goes
through this same check. The worker contract (C, P, S, M, R0-R2) is identical
for every kind. What differs is R3 (the kind's answer shape) and Q1 (quotes):

  extract    lens object; every pr+quote checked by check_extraction.py
  synthesis  {candidates, dropped}; no quote in this kind, cited_prs must be
             corpus digest keys (R3). Deeper checks: stage_inputs verify-inputs.
  verify     the build_prompts verify object for --expect-id; every pr+quote
             checked by check_extraction.py
  final      {records, patterns, extraction_ledger, limitations}; every
             pattern quote entry PR-* "..." checked by check_extraction.py.
             Judgment-field schema, deterministic-field refusal and the
             envelope: stage_inputs assemble + validate_corpus (must run next).

--input-scope FILE binds a synthesis, verify or final answer to what its input
held, for a stage built from extraction answers instead of the whole digest
(stage_inputs.py writes input-scope.json next to such a prompt): {"keys": [digest
keys the input held], "quotes": [{"pr": key, "quote": text}] (final only)}. A
synthesis answer may cite only those keys, a verify answer may only name those PRs,
and every pattern quote of a final answer must be one of the listed pairs, exactly.
This only adds a check (item R5); the corpus checks stay. Without it nothing changes.

An assess answer (#259) is the candidate verifier's assessment document for the combinations of one discovery
(stage_inputs.py assess-inputs). It has no corpus and no quote: C1 reads --discovery (its sha256 must be the scope's),
R3 is the verifier's own assessment shape plus the scope's combinations, A1 binds discovery_sha256, R5 keeps every
link inside the scope's links, A2 keeps every policy digest inside the policy bytes the input held (key, source,
sha256), C2 binds --policy-evidence to the file the input was built with, A3 runs the verifier's own record gate
in process (import, network refused) on the same discovery, the answer bytes, that policy evidence and, when given,
--locus-search, and A4 refuses what the call cannot have done (execution evidence, an executed reproduction, a
private reference outside private-report-ready). A URL in free text counts as a link for R5. A ready status passes A3 only with the locus search of its locus (see README 3.6).

A final answer's search_clues (item R6, #82) must be strings the candidate verifier can put into GitHub search
as they are: its discover quotes each clue into repo:<target> "<clue>" (issue, PR and code searches) and drops a
clue with a double quote, a backslash or a control character (verify_candidates._safe_search_clue). R6 fails a
clue that is not a string, empty, longer than the candidate-reads entry's clue limit (MAX_CLUE_CHARS, 200), has
edge spaces, a double quote, a backslash or a control character, or holds Hangul (a sentence, not a search
string: the 2026-10-05 discover of Korean clues read nothing). A pattern without search_clues is the assemble
schema's to refuse.

Limits are strict by default (run_limits.json): M1 needs status succeeded,
exit 0 and present, empty failures and problems; M5 needs observed_models to
be exactly [the requested model] (missing = unknown = FAIL, mixed or fallback
= FAIL); P4 needs prompt_sha256. A missing key in a limits file means strict.
Only a file marked "legacy_fixture": true (run_limits.legacy-fixture.json) may
turn M5 and P4 into INFO, and such a run never adopts an answer.

--adopt-to writes the verified report bytes to a new 0444 file only when the
verdict is ok; that file is what stage_inputs.py takes. A refused answer is
never adopted. When the worker manifest carries prompt_sha256 (A's proposed
field), P4 requires it to equal the sha256 of --prompt's bytes; without it P4
fails under strict limits and is INFO under legacy limits.

Precondition: RUN is a result tree the A pickup verifier (verify_pickup.py) has
already accepted and extracted on the host. This script re-checks what it reads
anyway: the result directory and files must be real (no symlink), regular,
bounded in size, and read through O_NOFOLLOW before any byte is parsed.

Every item is PASS, FAIL, BLOCKED (a prerequisite failed), or INFO (recorded,
not judged). The verdict also names a result class:

  evidence      quotes present and all verbatim against a non-empty corpus
  insufficient  a valid answer with no quote at all (no pattern, no observation):
                an honest "nothing found" partial result, never "complete"
  invalid       anything failed or was blocked

Exit 0 for evidence or insufficient, 1 for invalid. Nothing here reads a
credential, opens the network, or starts a model; quote checking reuses the
analyzer's own check_extraction.py. Observed models are copied from the worker
manifest as INFO; they are not the gate for which model ran.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import re
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ANALYZER = REPO / "dot_codex/skills/analyzing-open-source-pr-patterns/scripts"
CHECK = ANALYZER / "check_extraction.py"
CREDENTIAL_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
# Strings that must never reach the model prompt or come back in the report.
SECRET_MARKERS = ("oss-research-credentials", "oauth-token", CREDENTIAL_VARIABLE)
MANIFEST_MAX_BYTES = 1 << 20
CORPUS_MAX_BYTES = 64 << 20
ROLES = {"supports", "counterexample"}
PATTERN_KEYS = {"title", "description", "kind", "evidence", "applicability", "counterconditions",
                "search_clues", "expected_tests", "maintainer_judgment_required", "confidence",
                "confidence_reason"}
KINDS = ("extract", "synthesis", "verify", "final", "assess")
VERIFIER = REPO / "dot_codex/skills/verifying-open-source-contribution-candidates/scripts/verify_candidates.py"
DISCOVERY_MAX_BYTES = 4 << 20
VERIFY_KEYS = {"id", "verdict", "evidence_checks", "counterexamples", "distinct_prs", "distinct_repos",
               "actionable", "actionable_reason", "confidence", "revised_statement"}
FINAL_KEYS = {"records", "patterns", "extraction_ledger", "limitations"}
QUOTE_ENTRY = re.compile(r'(PR-[0-9]+) "(.*)"', re.S)
# The candidate-reads entry's clue limit (dot_local/bin/executable_oss-research-candidate-reads MAX_CLUE_CHARS).
SEARCH_CLUE_MAX_CHARS = 200
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]")
STAGE_OWNER = {
    "extract": "next: stage_inputs.py synthesis-input (all five lenses, check_extraction again)",
    "synthesis": "next: stage_inputs.py verify-inputs (candidate caps, statement length, digest keys, prompt budget)",
    "verify": "next: stage_inputs.py final-input (id binding, answer size, prompt budget)",
    "final": "next: stage_inputs.py assemble (judgment-field schema, deterministic-field refusal, "
             "record identity) and validate_corpus --analysis-output",
    "assess": "next: verify_candidates.py locus-search on the adopted answer, workflow adopt-locus-search, then "
              "record --assessment ... --origin ai (the workflow runs the record gate again)",
}


class Unsafe(Exception):
    pass


def read_bounded(path: Path, limit: int) -> bytes:
    """Read a regular, non-symlink file of at most `limit` bytes (checked before reading)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Unsafe("%s is not a regular file" % path.name)
        if info.st_size > limit:
            raise Unsafe("%s is %d bytes (> %d)" % (path.name, info.st_size, limit))
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise Unsafe("%s grew past %d bytes" % (path.name, limit))
        return data
    finally:
        os.close(fd)


def real_dir(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Unsafe("%s is not a real directory" % path)


class Verdict:
    def __init__(self):
        self.items = []

    def add(self, item, status, detail=""):
        self.items.append({"id": item, "status": status, "detail": detail})
        return status == "PASS"

    def block(self, items, why):
        for item in items:
            self.add(item, "BLOCKED", why)

    def ok(self):
        return not any(i["status"] in ("FAIL", "BLOCKED") for i in self.items)


def corpus_keys(corpus_doc: dict) -> dict:
    """Digest key -> record, as build_digest names them (the keys prompts cite)."""
    if str(ANALYZER) not in sys.path:
        sys.path.insert(0, str(ANALYZER))
    from build_digest import records_by_key  # noqa: E402
    return records_by_key(corpus_doc)


def answer_bytes(value) -> int:
    """An answer (or one candidate) the way the next stage's prompt block holds it: indent-1 JSON, UTF-8. The stage
    output limits of run_limits.json (#248) are in this measure (stage_inputs.answer_bytes)."""
    return len(json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


def schema_problems(answer: object, limits: dict, lens: str) -> list:
    problems = []
    if not isinstance(answer, dict) or set(answer) != {"lens", "patterns", "observations", "rejected"}:
        return ["keys must be exactly lens, patterns, observations, rejected"]
    if answer_bytes(answer) > limits["extract_answer_max_bytes"]:
        problems.append("the answer is %d bytes as a prompt holds it (> extract_answer_max_bytes %d)"
                        % (answer_bytes(answer), limits["extract_answer_max_bytes"]))
    if answer["lens"] != lens:
        problems.append("lens %r" % (answer["lens"],))
    for key, cap in (("patterns", "max_patterns"), ("observations", "max_observations"),
                     ("rejected", "max_rejected")):
        if not isinstance(answer[key], list) or len(answer[key]) > limits[cap]:
            problems.append("%s must be a list of at most %d" % (key, limits[cap]))
    if problems:
        return problems
    for index, pattern in enumerate(answer["patterns"]):
        where = "patterns[%d]" % index
        if not isinstance(pattern, dict):
            problems.append(where + " is not an object")
            continue
        missing = PATTERN_KEYS - set(pattern)
        if missing:
            problems.append(where + " lacks " + ", ".join(sorted(missing)))
        evidence = pattern.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            problems.append(where + ".evidence must be a non-empty list")
            continue
        for e_index, item in enumerate(evidence):
            if not (isinstance(item, dict) and isinstance(item.get("pr"), str) and isinstance(item.get("quote"), str)
                    and item.get("role") in ROLES and isinstance(item.get("claim"), str)):
                problems.append("%s.evidence[%d] needs pr, quote, claim (strings) and role in %s"
                                % (where, e_index, sorted(ROLES)))
    for index, item in enumerate(answer["observations"]):
        if not (isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("pr", "note", "quote"))):
            problems.append("observations[%d] needs pr, note, quote (strings)" % index)
    for index, item in enumerate(answer["rejected"]):
        if not (isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("idea", "reason"))):
            problems.append("rejected[%d] needs idea, reason (strings)" % index)
    return problems


def count_quotes(answer: dict) -> int:
    return sum(len(p["evidence"]) for p in answer["patterns"]) + len(answer["observations"])


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def synthesis_problems(answer: object, limits: dict, keys: dict) -> list:
    if not isinstance(answer, dict) or set(answer) != {"candidates", "dropped"}:
        return ["keys must be exactly candidates, dropped"]
    candidates, dropped = answer["candidates"], answer["dropped"]
    if not isinstance(candidates, list) or len(candidates) > limits["synthesis_max_candidates"]:
        return ["candidates must be a list of at most %d (synthesis_max_candidates)" % limits["synthesis_max_candidates"]]
    if not isinstance(dropped, list) or len(dropped) > limits["max_rejected"]:
        return ["dropped must be a list of at most %d" % limits["max_rejected"]]
    problems, seen = [], set()
    for index, item in enumerate(candidates):
        where = "candidates[%d]" % index
        if not isinstance(item, dict) or not {"id", "statement", "cited_prs"} <= set(item) \
                or set(item) - {"id", "statement", "cited_prs", "found_by"}:
            problems.append(where + " needs id, statement, cited_prs (and optional found_by) only")
            continue
        if not isinstance(item["id"], str) or re.fullmatch(r"C[1-9][0-9]?", item["id"]) is None or item["id"] in seen:
            problems.append(where + ".id must be a unique C1 … C99")
        seen.add(item.get("id"))
        if answer_bytes(item) > limits["candidate_max_bytes"]:
            problems.append(where + " is %d bytes as a prompt holds it (> candidate_max_bytes %d)"
                            % (answer_bytes(item), limits["candidate_max_bytes"]))
        if not isinstance(item["statement"], str) or not item["statement"].strip() \
                or len(item["statement"]) > limits["candidate_statement_max_chars"]:
            problems.append(where + ".statement must be a nonempty string of at most %d characters"
                            % limits["candidate_statement_max_chars"])
        cited = item["cited_prs"]
        if not isinstance(cited, list) or len(set(k for k in cited if isinstance(k, str))) < 2 \
                or not all(isinstance(k, str) and k in keys for k in cited):
            problems.append(where + ".cited_prs must name two or more distinct corpus digest keys")
    for index, item in enumerate(dropped):
        if not (isinstance(item, dict) and set(item) == {"idea", "reason"} and all(isinstance(v, str) for v in item.values())):
            problems.append("dropped[%d] needs idea, reason (strings) only" % index)
    return problems


def verify_problems(answer: object, expect_id) -> list:
    if not isinstance(answer, dict) or set(answer) != VERIFY_KEYS:
        return ["keys must be exactly " + ", ".join(sorted(VERIFY_KEYS))]
    problems = []
    if expect_id is None or answer["id"] != expect_id:
        problems.append("id %r is not the expected candidate %r" % (answer["id"], expect_id))
    if answer["verdict"] not in ("supported", "weak", "unsupported"):
        problems.append("verdict must be supported, weak, or unsupported")
    if answer["confidence"] not in ("high", "medium", "low"):
        problems.append("confidence must be high, medium, or low")
    if not isinstance(answer["actionable"], bool):
        problems.append("actionable must be a boolean")
    for key in ("distinct_prs", "distinct_repos"):
        if not isinstance(answer[key], int) or isinstance(answer[key], bool) or answer[key] < 0:
            problems.append(key + " must be a non-negative integer")
    for key in ("actionable_reason", "revised_statement"):
        if not isinstance(answer[key], str):
            problems.append(key + " must be a string")
    checks = answer["evidence_checks"]
    if not isinstance(checks, list) or not checks:
        problems.append("evidence_checks must be a non-empty list")
    else:
        for index, item in enumerate(checks):
            if not (isinstance(item, dict) and set(item) == {"pr", "holds", "quote", "reason"}
                    and isinstance(item["holds"], bool)
                    and all(isinstance(item[k], str) for k in ("pr", "quote", "reason"))):
                problems.append("evidence_checks[%d] needs pr, holds (bool), quote, reason only" % index)
    counter = answer["counterexamples"]
    if not isinstance(counter, list):
        problems.append("counterexamples must be a list")
    else:
        for index, item in enumerate(counter):
            if not (isinstance(item, dict) and set(item) == {"pr", "quote", "reason"}
                    and all(isinstance(item[k], str) for k in ("pr", "quote", "reason"))):
                problems.append("counterexamples[%d] needs pr, quote, reason (strings) only" % index)
    return problems


def final_problems(answer: object, limits: dict, keys: dict) -> tuple:
    """Top-level shape, record identity, pattern count and quote entries. Returns (problems, quote objects)."""
    if not isinstance(answer, dict) or set(answer) != FINAL_KEYS:
        return ["keys must be exactly " + ", ".join(sorted(FINAL_KEYS))], []
    key_of = {record.get("pr_id"): key for key, record in keys.items()}
    problems, quotes = [], []
    if not isinstance(answer["records"], dict) or set(answer["records"]) != set(key_of):
        problems.append("records must name every corpus PR-* id exactly once: %s" % sorted(key_of))
    patterns = answer["patterns"]
    if not isinstance(patterns, list) or len(patterns) > limits["max_patterns"]:
        return problems + ["patterns must be a list of at most %d" % limits["max_patterns"]], []
    for index, pattern in enumerate(patterns):
        evidence = pattern.get("confidence", {}).get("evidence") if isinstance(pattern, dict) \
            and isinstance(pattern.get("confidence"), dict) else None
        if not _strings(evidence):
            problems.append("patterns[%d].confidence.evidence must be a list of strings" % index)
            continue
        for item in evidence:
            match = QUOTE_ENTRY.fullmatch(item)
            if match is None:
                continue
            if match.group(1) not in key_of:
                problems.append("patterns[%d] quotes unknown %s" % (index, match.group(1)))
                continue
            quotes.append({"pr": key_of[match.group(1)], "quote": match.group(2)})
    if not isinstance(answer["extraction_ledger"], str) or not answer["extraction_ledger"].strip():
        problems.append("extraction_ledger must be a nonempty string")
    if not _strings(answer["limitations"]):
        problems.append("limitations must be a list of nonempty strings")
    if patterns and not quotes:
        problems.append("patterns carry no PR-* \"quote\" entry")
    return problems, quotes


def search_clue_problems(answer: dict) -> tuple:
    """R6: every search clue of the final answer's patterns is a GitHub search string (see the module docstring).
    Returns (problems, number of clues seen)."""
    problems, seen = [], 0
    for index, pattern in enumerate(answer.get("patterns") or []):
        if not isinstance(pattern, dict) or "search_clues" not in pattern:
            continue
        clues = pattern["search_clues"]
        if not isinstance(clues, list):
            problems.append("patterns[%d].search_clues: not a list" % index)
            continue
        for number, clue in enumerate(clues):
            seen += 1
            at = "patterns[%d].search_clues[%d]" % (index, number)
            if not isinstance(clue, str):
                problems.append(at + ": not a string")
                continue
            if not clue:
                problems.append(at + ": empty")
                continue
            if len(clue) > SEARCH_CLUE_MAX_CHARS:
                problems.append(at + ": longer than %d characters (%d)" % (SEARCH_CLUE_MAX_CHARS, len(clue)))
            if clue != clue.strip():
                problems.append(at + ": edge spaces")
            if '"' in clue:
                problems.append(at + ": double quote")
            if "\\" in clue:
                problems.append(at + ": backslash")
            if any(ord(character) < 32 or ord(character) == 127 for character in clue):
                problems.append(at + ": control character")
            if HANGUL.search(clue):
                problems.append(at + ": Hangul (a sentence, not a search string) %r" % clue[:40])
    return problems, seen


def scope_problems(kind: str, answer: dict, quote_doc: dict, scope: dict) -> list:
    """What the answer used that its input did not hold (the input-scope.json of the stage)."""
    keys = set(scope["keys"])
    if kind == "synthesis":
        outside = sorted({key for item in answer["candidates"] for key in item["cited_prs"]} - keys)
        return ["cites digest keys its input did not hold: " + ", ".join(outside)] if outside else []
    if kind == "verify":
        named = {item["pr"] for item in answer["evidence_checks"] + answer["counterexamples"]}
        outside = sorted(named - keys)
        return ["names PRs whose section was not in its prompt: " + ", ".join(outside)] if outside else []
    allowed = {(item["pr"], item["quote"]) for item in scope.get("quotes", [])}
    outside = [item for item in quote_doc["quotes"] if (item["pr"], item["quote"]) not in allowed]
    if not outside:
        return []
    problems = ["%d pattern quote(s) are not among the verified quotes its input listed (first: %s %r)"
                % (len(outside), outside[0]["pr"], outside[0]["quote"][:60])]
    # Only a reason, never a match: a quote that equals a listed one once its JSON escapes are taken out still
    # fails (the r5 final answer wrote \" for a source "; #82).
    escaped = [item for item in outside
               if (item["pr"], item["quote"].replace('\\"', '"').replace("\\\\", "\\")) in allowed]
    if escaped:
        problems.append("%d of them carry a JSON escape (\\\" or \\\\) the source text does not have (first: %s %r)"
                        % (len(escaped), escaped[0]["pr"], escaped[0]["quote"][:60]))
    return problems


def load_verifier():
    """The candidate verifier, by its fixed repository path (never from input data); one copy per process."""
    existing = sys.modules.get("verify_candidates")
    if existing is not None and os.path.realpath(getattr(existing, "__file__", "")) == os.path.realpath(VERIFIER):
        return existing
    spec = importlib.util.spec_from_file_location("verify_candidates", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_candidates"] = module
    spec.loader.exec_module(module)
    return module


def assess_problems(answer: object, scope: dict, vc) -> list:
    """R3 of an assess answer: the verifier's assessment document shape, and the combinations of its input."""
    if not isinstance(answer, dict) or set(answer) != {"schema_version", "discovery_sha256", "assessments"}:
        return ["keys must be exactly schema_version, discovery_sha256, assessments"]
    problems = []
    if answer["schema_version"] != "1.0.0":
        problems.append("schema_version must be 1.0.0")
    if not isinstance(answer["discovery_sha256"], str):
        problems.append("discovery_sha256 must be a string")
    items = answer["assessments"]
    if not isinstance(items, list) or not items or len(items) > scope["max_assessments"]:
        return problems + ["assessments must be a list of 1 to %d" % scope["max_assessments"]]
    combinations = {(item["pattern_id"], item["repository"]) for item in scope["combinations"]}
    seen = set()
    for index, item in enumerate(items):
        problems.extend(vc._assessment_shape_violations(item, "assessments[%d]" % index))
        if isinstance(item, dict):
            combination = (item.get("pattern_id"), item.get("repository"))
            if not all(isinstance(value, str) for value in combination):
                continue        # the verifier's shape check above already names the field
            if combination not in combinations:
                problems.append("assessments[%d] assesses %s x %s, not a combination of its input"
                                % ((index,) + combination))
            seen.add(combination)
    missing = sorted(combinations - seen)
    if missing:
        problems.append("no assessment for " + ", ".join("%s x %s" % item for item in missing))
    return problems


def assess_links(item: dict) -> list:
    """Every link an assessment cites: all its evidence_links and the duplicate verdict's matched items."""
    links = list(item.get("evidence_links") or [])
    for group in ("readiness_checks", "policy_checks"):
        values = item.get(group)
        for value in (values.values() if isinstance(values, dict) else []):
            if isinstance(value, dict) and isinstance(value.get("evidence_links"), list):
                links += value["evidence_links"]
    for name, key in (("reproduction", "evidence_links"), ("duplicate_verdict", "matched_items")):
        value = item.get(name)
        if isinstance(value, dict) and isinstance(value.get(key), list):
            links += value[key]
    return links


URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>()\[\]{}`]+")


def text_values(value) -> list:
    """Every string anywhere in a value (free text included)."""
    if isinstance(value, dict):
        return [text for item in value.values() for text in text_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in text_values(item)]
    return [value] if isinstance(value, str) else []


def assess_scope_problems(answer: dict, scope: dict) -> tuple:
    """(R5: links outside the input, A2: policy bytes outside the input). Exact match only, as R5 of final.

    A link field must hold strings its input listed. A URL anywhere else in the assessment (summary, notes, public
    steps, the private reference) is a link too, and must be one its input listed. A policy value names a source
    only for ``found: true``, and then (key, source, sha256) must be bytes its input held."""
    allowed_links = set(scope["links"])
    allowed_policy = {(item["key"], item["source"], item["sha256"]) for item in scope["policy"]}
    links, policy = [], []
    for index, item in enumerate(answer["assessments"]):
        cited = assess_links(item)
        typed = [link for link in cited if not isinstance(link, str)]
        if typed:
            links.append("assessments[%d] has %d link value(s) that are not strings" % (index, len(typed)))
        outside = [link for link in cited if isinstance(link, str) and link not in allowed_links]
        for text in text_values(item):
            if text in allowed_links:
                continue
            outside += [url.rstrip(".,;:!?") for url in URL_IN_TEXT.findall(text)
                        if url.rstrip(".,;:!?") not in allowed_links]
        outside = list(dict.fromkeys(outside))
        if outside:
            links.append("assessments[%d] cites %d link(s) its input did not hold: %s"
                         % (index, len(outside), ", ".join(repr(link) for link in outside[:3])))
        checks = item.get("policy_checks")
        for key, value in sorted(checks.items() if isinstance(checks, dict) else []):
            if not isinstance(value, dict):
                continue
            source, sha = value.get("source"), value.get("sha256")
            if value.get("found") is not True and source is not None:
                policy.append("assessments[%d].policy_checks.%s names a source (%r) but was not found; only found: "
                              "true names one" % (index, key, source))
            if sha is None:
                continue
            if key == "program_rules":
                policy.append("assessments[%d].policy_checks.program_rules names a digest; the input held no "
                              "program rules" % index)
            elif not isinstance(source, str) or not isinstance(sha, str) or (key, source, sha) not in allowed_policy:
                policy.append("assessments[%d].policy_checks.%s names policy bytes its input did not hold "
                              "(source %r, sha256 %r)" % (index, key, source, sha))
    return links, policy


def assess_rule_problems(answer: dict) -> list:
    """A4: what one assessment call cannot have done. It executed nothing (no execution evidence, no executed
    reproduction) and held no private evidence (a private reference only for private-report-ready)."""
    problems = []
    for index, item in enumerate(answer["assessments"]):
        if item.get("execution_evidence") is not None:
            problems.append("assessments[%d].execution_evidence must be null: the assess call executed nothing" % index)
        reproduction = item.get("reproduction")
        if isinstance(reproduction, dict) and reproduction.get("method") == "executed":
            problems.append("assessments[%d].reproduction.method executed: the assess call executed nothing" % index)
        if item.get("private_evidence_reference") is not None and item.get("status") != "private-report-ready":
            problems.append("assessments[%d].private_evidence_reference must be null outside private-report-ready"
                            % index)
    return problems


def record_gate(vc, discovery: Path, answer: bytes, policy_evidence, locus_search) -> tuple:
    """The verifier's own record gate on the same inputs, in process, writing only to a scratch directory.
    Returns (ok, detail); the detail is the verifier's own message."""
    def refuse(*args, **kwargs):
        raise OSError("the record gate opens no network connection")

    with tempfile.TemporaryDirectory(prefix="e2e-assess-gate-") as tmp:
        document = Path(tmp) / "assessment.json"
        document.write_bytes(answer)
        argv = ["record", "--discovery", str(discovery), "--assessment", str(document),
                "--output", str(Path(tmp) / "candidates.json"), "--manifest", str(Path(tmp) / "manifest.json")]
        if policy_evidence is not None:
            argv += ["--policy-evidence", str(policy_evidence)]
        if locus_search is not None:
            argv += ["--locus-search", str(locus_search)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = vc.main(argv, opener=refuse)
            except SystemExit as error:
                code = error.code if isinstance(error.code, int) else 2
            except Exception as error:  # a malformed answer must fail the gate, never crash the check
                code = "raised"
                print("%s: %s" % (type(error).__name__, error), file=sys.stderr)
    detail = (stderr.getvalue().strip() or stdout.getvalue().strip())[:4000]
    return code == 0, "verifier record exit %s: %s" % (code, detail)


def assess_scope_valid(scope: object) -> bool:
    """The shape of an assess.scope.json (stage_inputs assess-inputs)."""
    sha = re.compile(r"[0-9a-f]{64}")
    if not isinstance(scope, dict) or scope.get("kind") != "assess":
        return False
    combinations, policy, evidence = scope.get("combinations"), scope.get("policy"), scope.get("policy_evidence_sha256")
    return (isinstance(scope.get("discovery_sha256"), str) and sha.fullmatch(scope["discovery_sha256"]) is not None
            and isinstance(combinations, list) and combinations
            and all(isinstance(item, dict) and set(item) == {"pattern_id", "repository"}
                    and all(isinstance(value, str) for value in item.values()) for item in combinations)
            and type(scope.get("max_assessments")) is int and scope["max_assessments"] >= len(combinations)
            and isinstance(scope.get("links"), list) and all(isinstance(link, str) for link in scope["links"])
            and isinstance(policy, list)
            and all(isinstance(item, dict) and set(item) == {"key", "source", "sha256"}
                    and all(isinstance(value, str) for value in item.values()) for item in policy)
            and (evidence is None or isinstance(evidence, str) and sha.fullmatch(evidence) is not None))


def verify_assess(v: "Verdict", answer: dict, report_bytes: bytes, scope: dict, assess: dict) -> tuple:
    """A1, R5, A2, C2, A3 and Q1 of an assess answer whose R3 passed; each is judged on its own so a refusal names
    every reason. Returns (result class, verified report bytes or None)."""
    vc = load_verifier()
    bound = answer["discovery_sha256"] == scope["discovery_sha256"]
    v.add("A1-discovery-binding", "PASS" if bound else "FAIL",
          "answer discovery_sha256 %r %s the input's %s" % (answer["discovery_sha256"], "is" if bound else "is not",
                                                           scope["discovery_sha256"]))
    links, policy = assess_scope_problems(answer, scope)
    v.add("R5-input-scope", "FAIL" if links else "PASS", "; ".join(links) or "every link is one its input held")
    v.add("A2-policy-bytes", "FAIL" if policy else "PASS",
          "; ".join(policy) or "every policy digest names bytes its input held")
    evidence, expected = assess.get("policy_evidence"), scope["policy_evidence_sha256"]
    if evidence is None:
        same = expected is None
        detail = "none given" + ("" if same else "; the input was built with policy evidence %s" % expected)
    else:
        try:
            digest = hashlib.sha256(read_bounded(evidence, MANIFEST_MAX_BYTES)).hexdigest()
        except (OSError, Unsafe) as error:
            digest = "unreadable (%s)" % error
        same = digest == expected
        detail = "--policy-evidence sha256 %s %s the input's %s" % (digest, "is" if same else "is not", expected)
    v.add("C2-policy-evidence", "PASS" if same else "FAIL", detail)
    ok, detail = record_gate(vc, assess["discovery"], report_bytes, evidence, assess.get("locus_search"))
    v.add("A3-record-gate", "PASS" if ok else "FAIL", detail)
    rules = assess_rule_problems(answer)
    v.add("A4-assess-rules", "FAIL" if rules else "PASS",
          "; ".join(rules) or "no execution evidence, no executed reproduction, no stray private reference")
    v.add("Q1-quotes-verbatim", "INFO", "an assess answer holds no quote; its links and policy digests were "
          "checked against its input in R5 and A2")
    if not v.ok():
        return "invalid", None
    statuses = {item["status"] for item in answer["assessments"]}
    return ("insufficient" if statuses == {"unverified"} else "evidence"), report_bytes


def verify(run_dir: Path, corpus: Path, prompt: Path, limits: dict, kind: str = "extract",
           lens: str | None = None, expect_id: str | None = None, scope: dict | None = None,
           assess: dict | None = None) -> tuple:
    """Return (verdict, result class, verified report bytes or None). ``assess`` (kind assess only) holds the
    discovery path, and the policy evidence and locus search paths when given."""
    v = Verdict()
    worker = limits["worker"]
    lens = lens or limits["prompt_lens"]
    later = ["M1", "M2", "M3", "M4", "R1", "R0", "R2", "R3", "Q1"]

    # C: the corpus the prompt was built from must be real and non-empty (assess: the discovery it judged).
    if kind == "assess":
        corpus_doc = None
        try:
            data = read_bounded(assess["discovery"], DISCOVERY_MAX_BYTES)
            document = json.loads(data)
            records = document.get("records") if isinstance(document, dict) else None
            if not isinstance(records, list) or not records:
                raise Unsafe("discovery has no records")
            digest = hashlib.sha256(data).hexdigest()
            if digest != scope["discovery_sha256"]:
                raise Unsafe("discovery sha256 %s is not the input's %s" % (digest, scope["discovery_sha256"]))
            v.add("C1-discovery", "PASS", "%d record(s), sha256 %s" % (len(records), digest))
        except (OSError, ValueError, Unsafe) as error:
            v.add("C1-discovery", "FAIL", str(error))
            v.block(later, "no usable discovery")
            return v, "invalid", None
    else:
        try:
            corpus_doc = json.loads(read_bounded(corpus, CORPUS_MAX_BYTES))
            records = corpus_doc.get("records") if isinstance(corpus_doc, dict) else None
            if not isinstance(records, list) or not records:
                raise Unsafe("corpus has no records")
            v.add("C1-corpus-nonempty", "PASS", "%d record(s)" % len(records))
        except (OSError, ValueError, Unsafe) as error:
            v.add("C1-corpus-nonempty", "FAIL", str(error))
            v.block(later, "no usable corpus")
            return v, "invalid", None

    # P: the prompt the worker was given.
    try:
        prompt_bytes = read_bounded(prompt, limits["prompt_max_bytes"])
        v.add("P1-prompt-size", "PASS", "%d bytes" % len(prompt_bytes))
    except (OSError, Unsafe) as error:
        v.add("P1-prompt-size", "FAIL", str(error))
        prompt_bytes = b""
    leaked = [m for m in SECRET_MARKERS if m.encode() in prompt_bytes]
    v.add("P2-prompt-has-no-credential-marker", "FAIL" if leaked else "PASS", ", ".join(leaked))
    v.add("P3-prompt-sha256", "INFO", hashlib.sha256(prompt_bytes).hexdigest())

    # S: the result tree itself, before reading anything in it.
    result = run_dir / "result"
    try:
        real_dir(run_dir)
        real_dir(result)
        manifest_bytes = read_bounded(result / "manifest.json", MANIFEST_MAX_BYTES)
        v.add("S1-result-tree-safe", "PASS")
    except (OSError, Unsafe) as error:
        v.add("S1-result-tree-safe", "FAIL", str(error))
        v.block(later, "unsafe or missing result tree")
        return v, "invalid", None

    # M: the worker's own manifest.
    try:
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("not an object")
    except ValueError as error:
        v.add("M0-manifest-readable", "FAIL", str(error))
        v.block(later, "no manifest")
        return v, "invalid", None
    v.add("M0-manifest-readable", "PASS")
    exit_code = manifest.get("exit_code")
    succeeded = (manifest.get("status") == "succeeded" and isinstance(exit_code, int)
                 and not isinstance(exit_code, bool) and exit_code == 0
                 and manifest.get("failures") == [] and manifest.get("problems") == [])
    v.add("M1-run-succeeded", "PASS" if succeeded else "FAIL",
          "status=%s exit_code=%s failures=%s problems=%s (failures and problems must be present and empty)"
          % (manifest.get("status"), exit_code, manifest.get("failures", "missing"), manifest.get("problems", "missing")))
    expected = {"profile": worker["profile"], "permission_mode": worker["permission_mode"],
                "requested_model": worker["model"], "requested_effort": worker["effort"]}
    got = {k: manifest.get(k) for k in expected}
    v.add("M2-requested-selection-recorded", "PASS" if got == expected else "FAIL", json.dumps(got))
    # A worker manifest contract: read-only runs carry neither execution_profile nor staged;
    # staged runs carry both. No default is assumed for a missing key.
    if worker["execution_profile"] == "read-only":
        extra = [k for k in ("execution_profile", "staged") if k in manifest]
        v.add("M3-execution-profile", "FAIL" if extra else "PASS",
              ("read-only manifest must not carry " + ", ".join(extra)) if extra else "read-only (no staged keys)")
    else:
        ok = manifest.get("execution_profile") == worker["execution_profile"] and isinstance(manifest.get("staged"), dict)
        v.add("M3-execution-profile", "PASS" if ok else "FAIL", str(manifest.get("execution_profile")))
    credential = manifest.get("credential")
    v.add("M4-credential-exposure-clean",
          "PASS" if credential == {"variable": CREDENTIAL_VARIABLE, "exact_value_check": "clean"} else "FAIL",
          json.dumps(credential))
    # M5: which model the result says answered. Strict (live) limits require exactly the requested
    # model; a missing modelUsage is unknown, not success. Only explicit legacy limits keep it INFO.
    observed = manifest.get("observed_models")
    if limits.get("require_observed_model", True) is not False:
        if observed == [worker["model"]]:
            v.add("M5-observed-models", "PASS", json.dumps(observed))
        elif observed is None:
            v.add("M5-observed-models", "FAIL", "unknown: the result named no model (modelUsage absent)")
        else:
            v.add("M5-observed-models", "FAIL", "observed %s, required exactly [%r] (mixed or fallback models fail)"
                  % (json.dumps(observed), worker["model"]))
    else:
        v.add("M5-observed-models", "INFO", "legacy limits: " + json.dumps(observed if observed is not None else "not recorded"))
    v.add("M6-relay", "INFO", json.dumps(manifest.get("relay", "no relay in this run")))
    # P4: prompt binding. The worker records prompt_sha256 only once A adds it; then it must match.
    recorded = manifest.get("prompt_sha256")
    if recorded is None and limits.get("require_prompt_sha256", True) is not False:
        v.add("P4-prompt-binding", "FAIL", "prompt_sha256 missing; these limits require the worker to record it")
    elif recorded is None:
        v.add("P4-prompt-binding", "INFO", "legacy limits: prompt_sha256 not recorded; bind by the host call ledger")
    else:
        v.add("P4-prompt-binding", "PASS" if recorded == hashlib.sha256(prompt_bytes).hexdigest() else "FAIL",
              "manifest prompt_sha256 %s the --prompt bytes" % ("matches" if recorded == hashlib.sha256(prompt_bytes).hexdigest() else "differs from"))

    # R: the report (the model's answer text) and its binding to the manifest.
    try:
        report_bytes = read_bounded(result / "report.md", limits["report_max_bytes"])
    except (OSError, Unsafe) as error:
        v.add("R1-report-present", "FAIL", str(error))
        v.block(["R0", "R2", "R3", "Q1"], "no readable report")
        return v, "invalid", None
    digest = hashlib.sha256(report_bytes).hexdigest()
    v.add("R1-report-present", "PASS" if manifest.get("report_sha256") == digest else "FAIL",
          "%d bytes, sha256 %s manifest" % (len(report_bytes),
                                           "matches" if manifest.get("report_sha256") == digest else "differs from"))
    text = report_bytes.decode("utf-8", "replace")
    leaked = [m for m in SECRET_MARKERS if m in text]
    v.add("R0-report-has-no-credential-marker", "FAIL" if leaked else "PASS", ", ".join(leaked))
    try:
        answer = json.loads(text)
    except ValueError as error:
        v.add("R2-report-json", "FAIL", "not one JSON object: %s" % error)
        v.block(["R3", "Q1"], "not JSON")
        return v, "invalid", None
    v.add("R2-report-json", "PASS" if isinstance(answer, dict) else "FAIL")
    v.add("R4-stage-contract", "INFO", "%s answer; %s" % (kind, STAGE_OWNER[kind]))
    if kind == "assess":
        problems = assess_problems(answer, scope, load_verifier())
        if not v.add("R3-report-schema", "FAIL" if problems else "PASS", "; ".join(problems)):
            v.block(["A1-discovery-binding", "R5-input-scope", "A2-policy-bytes", "C2-policy-evidence",
                     "A3-record-gate", "A4-assess-rules", "Q1-quotes-verbatim"], "schema failed")
            return v, "invalid", None
        result_class, verified = verify_assess(v, answer, report_bytes, scope, assess)
        return v, result_class, verified
    keys = corpus_keys(corpus_doc)
    quote_doc, quotes, empty = answer, 0, False
    if kind == "extract":
        problems = schema_problems(answer, limits, lens)
        if not problems:
            quotes = count_quotes(answer)
            empty = quotes == 0
    elif kind == "synthesis":
        problems = synthesis_problems(answer, limits, keys)
        empty = not problems and not answer["candidates"]
    elif kind == "verify":
        problems = verify_problems(answer, expect_id)
        if len(report_bytes) > limits["verify_answer_max_bytes"]:
            problems.append("verify answer is %d bytes (> verify_answer_max_bytes %d)"
                            % (len(report_bytes), limits["verify_answer_max_bytes"]))
        if not problems:
            quotes = len(answer["evidence_checks"]) + len(answer["counterexamples"])
    else:
        problems, found = final_problems(answer, limits, keys)
        quote_doc, quotes = {"quotes": found}, len(found)
        empty = not problems and not answer["patterns"]
    if not v.add("R3-report-schema", "FAIL" if problems else "PASS", "; ".join(problems)):
        v.block(["Q1"] + (["R5"] if scope is not None else []), "schema failed")
        return v, "invalid", None
    if kind == "final":
        clue_problems, clues = search_clue_problems(answer)
        v.add("R6-search-clues", "FAIL" if clue_problems else "PASS", "; ".join(clue_problems)
              or ("%d search clue(s), each a GitHub search string" % clues if clues
                  else "no search_clues in the patterns (the assemble schema requires them)"))
    if scope is not None:
        found = scope_problems(kind, answer, quote_doc, scope)
        if not v.add("R5-input-scope", "FAIL" if found else "PASS", "; ".join(found) or "within what the input held"):
            v.block(["Q1"], "the answer went beyond its input")
            return v, "invalid", None

    # Q: every pr+quote pair against the corpus, with the analyzer's own checker.
    if kind == "synthesis":
        v.add("Q1-quotes-verbatim", "INFO", "a synthesis answer holds no quote; cited_prs were checked "
              "against the corpus digest keys in R3")
        return v, ("insufficient" if empty else "evidence") if v.ok() else "invalid", (report_bytes if v.ok() else None)
    if quotes == 0:
        v.add("Q1-quotes-verbatim", "PASS", "0 quotes: a valid %s answer with no pattern or observation" % kind)
        v.add("Q2-grounded-patterns", "INFO", "0")
        return v, ("insufficient" if v.ok() else "invalid"), (report_bytes if v.ok() else None)
    with tempfile.TemporaryDirectory(prefix="e2e-verify-") as tmp:
        result_file = Path(tmp) / (kind + ".result.json")
        result_file.write_text(json.dumps(quote_doc, ensure_ascii=False), encoding="utf-8")
        run = subprocess.run([sys.executable, "-B", str(CHECK), str(corpus), str(result_file)],
                             capture_output=True, text=True, timeout=120)
    try:
        report_json = json.loads(run.stdout)
        totals = report_json["totals"]
    except (ValueError, KeyError):
        v.add("Q1-quotes-verbatim", "FAIL", "check_extraction output unreadable (exit %d): %s"
              % (run.returncode, run.stderr.strip()[:300]))
        return v, "invalid", None
    verified, total = totals.get("verified"), totals.get("total")
    ok = run.returncode == 0 and total == quotes and verified == total
    v.add("Q1-quotes-verbatim", "PASS" if ok else "FAIL",
          "%s/%s verified (answer holds %d), check_extraction exit %d" % (verified, total, quotes, run.returncode))
    grounded = [p for f in report_json.get("files", []) for p in f.get("patterns", []) if p.get("grounded")]
    v.add("Q2-grounded-patterns", "INFO", "%d pattern(s) with two or more verified PRs" % len(grounded))
    if not v.ok():
        return v, "invalid", None
    return v, ("insufficient" if empty else "evidence"), report_bytes


def adopt(report_bytes: bytes, target: Path) -> None:
    """Write the verified bytes to a new read-only file; never overwrite."""
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o444)
    with os.fdopen(fd, "wb") as handle:
        handle.write(report_bytes)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--corpus", type=Path, help="every kind but assess (required there)")
    ap.add_argument("--discovery", type=Path, help="assess only (required): the discovery.json the input was built from")
    ap.add_argument("--policy-evidence", type=Path,
                    help="assess only: the hand-reviewed policy evidence the input was built with (record takes it too)")
    ap.add_argument("--locus-search", type=Path,
                    help="assess only: the locus search of this answer's loci (a ready status needs it)")
    ap.add_argument("--prompt", required=True, type=Path)
    ap.add_argument("--answer-kind", choices=KINDS, default="extract")
    ap.add_argument("--lens", help="extract only: the lens this call answered (default: limits prompt_lens)")
    ap.add_argument("--expect-id", help="verify only: the candidate id this call verified")
    ap.add_argument("--limits", type=Path, default=HERE / "run_limits.json")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--adopt-to", type=Path, help="new file that receives the verified report bytes when ok")
    ap.add_argument("--input-scope", type=Path,
                    help="synthesis, verify or final: the stage's input-scope.json (keys, and quotes for final); "
                         "assess (required): assess.scope.json")
    a = ap.parse_args(argv)
    assess = a.answer_kind == "assess"
    if assess and (a.discovery is None or a.input_scope is None or a.corpus is not None):
        ap.error("--answer-kind assess takes --discovery and --input-scope, and no --corpus")
    if not assess and (a.corpus is None or a.discovery or a.policy_evidence or a.locus_search):
        ap.error("--corpus is required, and --discovery, --policy-evidence and --locus-search are for assess only")
    if (a.lens is not None) != (a.answer_kind == "extract" and a.lens is not None):
        ap.error("--lens applies to --answer-kind extract only")
    if (a.expect_id is not None) != (a.answer_kind == "verify"):
        ap.error("--expect-id is required for, and only for, --answer-kind verify")
    limits = json.loads(a.limits.read_text(encoding="utf-8"))
    # These switches are booleans or absent (absent = strict). 0, null, "" and the
    # rest are refused, never read as a relaxed check.
    for key in ("legacy_fixture", "require_observed_model", "require_prompt_sha256"):
        if key in limits and type(limits[key]) is not bool:
            ap.error("limits %s must be true or false when present" % key)
    # Strict unless the limits file says, explicitly, that it is the legacy fixture profile.
    legacy = limits.get("legacy_fixture") is True
    if not legacy and (limits.get("require_observed_model") is False or limits.get("require_prompt_sha256") is False):
        ap.error("only limits marked legacy_fixture: true may relax observed-model or prompt_sha256 checks")
    if legacy and a.adopt_to is not None:
        ap.error("legacy fixture limits never adopt an answer (--adopt-to is for strict live limits only)")
    scope = None
    if a.input_scope is not None:
        if a.answer_kind == "extract":
            ap.error("--input-scope applies to synthesis, verify and final answers only")
        try:
            scope = json.loads(a.input_scope.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            ap.error("--input-scope is not readable JSON: %s" % error)
        quotes = scope.get("quotes", []) if isinstance(scope, dict) else None
        if assess:
            if not assess_scope_valid(scope):
                ap.error("--input-scope of assess must be the assess.scope.json of stage_inputs assess-inputs")
        elif (not isinstance(scope, dict) or not isinstance(scope.get("keys"), list)
                or not all(isinstance(key, str) for key in scope["keys"]) or not isinstance(quotes, list)
                or not all(isinstance(item, dict) and isinstance(item.get("pr"), str) and isinstance(item.get("quote"), str)
                           for item in quotes)):
            ap.error("--input-scope must be {\"keys\": [strings], \"quotes\": [{pr, quote}]}")
    verdict, result_class, report_bytes = verify(
        a.run_dir, a.corpus, a.prompt, limits, a.answer_kind, a.lens, a.expect_id, scope,
        {"discovery": a.discovery, "policy_evidence": a.policy_evidence, "locus_search": a.locus_search} if assess else None)
    document = {"ok": result_class in ("evidence", "insufficient"), "result_class": result_class,
                "answer_kind": a.answer_kind, "limits": "legacy-fixture" if legacy else "strict",
                "complete": False, "items": verdict.items,
                "note": "collected-bytes contract only; one answer is never a complete analysis, "
                        "and this is not proof of which model ran"}
    if a.adopt_to is not None:
        if document["ok"] and report_bytes is not None:
            try:
                adopt(report_bytes, a.adopt_to)
                document["adopted_to"] = str(a.adopt_to)
            except OSError as error:
                document["ok"] = False
                document["adopt_error"] = "not adopted: %s" % error.strerror
        else:
            document["adopted_to"] = None
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
