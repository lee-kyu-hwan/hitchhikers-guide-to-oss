#!/usr/bin/env python3
"""#78 actual E2E: host-side glue between isolated model calls, by the analyzer's own contract.

One workflow run (``oss_workflow.py init`` → ``adopt-collection`` → ``digest``) already
holds the collected corpus, the digest, and the five lens extraction prompts. This
script only moves bytes between that run and one input directory per isolated model
call, and assembles the final analysis envelope deterministically:

  dry-plan         RUN → the worst-case plan on stdout          (no model, nothing written; #248)
  extract-inputs   RUN → OUT/extract-<lens>/task.txt           (5 independent calls)
  continue-plan    RUN + adopted extract results + their extract stage → OUT/stage.json
                   (or + a continue stage and the adopted synthesis/verify answers of its ledger, or of the
                   ancestor ledgers of a straight chain: the rest)
                   (no prompt: the call plan of the stages not yet run, #82; with a part of the extract
                   results, the extract calls left too, #280)
  synthesis-input  RUN + extract results → OUT/task.txt        (1 call: candidates, or a call per group)
  synthesis-merge-input  RUN + a synthesis (or merge) stage + its group answers → OUT/<call>/task.txt
                   (1 last merge call, or the merge calls of another level, #248)
  verify-inputs    RUN + synthesis answer → OUT/verify-<id>/task.txt  (k calls, k may be 0)
  final-input      RUN + all answers → OUT/task.txt            (1 call: judgment fields)
  assemble         RUN + final answer → analysis.json          (no model; validated)
  assess-inputs    analysis.json + discovery.json [+ policy evidence] → OUT/assess/task.txt
                   (1 call: the candidate assessment of every combination of one discovery, #259; no RUN)

Then the existing workflow commands finish the run:
``adopt-analysis --analysis analysis.json --origin ai --extraction-result ...`` and ``report``.

Reused, never re-implemented: build_prompts.py (extract and verify prompts),
check_extraction.py (every quote against the corpus), validate_corpus.py
(``--analysis-output``), and oss_workflow.py (run state, integrity, report).
``assemble`` deep-copies every corpus record and fills the deterministic fields
(files_changed, evidence_manifest, license_spdx, revisions, times, pattern
generated_by/source_licenses/history) itself; a model answer that carries any of
them is refused. Nothing here opens the network, starts a model, or reads a credential.

Counts and sizes follow run_limits.json and are refused, never truncated: at most
``synthesis_max_candidates`` candidates of at most ``candidate_max_bytes`` each (so at most
that many verify calls), ``max_patterns`` final patterns, ``max_rejected`` dropped ideas,
the extract caps per lens result and ``extract_answer_max_bytes`` per extract answer. Each
prompt states the limits its answer is checked against (render_limits, build_prompts
--max-answer-bytes), and verify_e2e_result.py checks the same values.
The model calls of one run are therefore at most
``shards x lenses + synthesis calls + synthesis_max_candidates (verify) + 1 (final)``: 10
for a digest that fits one shard and whose final prompt can carry it (5 + 1 + 3 + 1).
Every call plan also holds the retry share of those calls (#280, retry_policy.py: run_limits.json
model_retry; 5 here), counted in max_model_calls, the ledger limit, and against ABSOLUTE_MAX_CALLS:
only a call whose answer broke a model output rule is retried, with the same prompt bytes.
The digest is sharded by ``oss_workflow.py digest --max-bytes`` so that each lens
prompt stays under the worker's input limit; every lens runs on every shard, and a
prompt over PROMPT_MAX_BYTES is refused before anything is written, never cut.

Worst-case plan (#248): from the corpus, the digest and those limits alone, every stage
input is sized with every answer at its limit, the reduce levels are counted (synthesis
groups, then merges of merges until one call) and so are the model calls; extract-inputs
records that plan as the call plan and refuses, before any model call, an extraction-only
run whose plan can pass PROMPT_MAX_BYTES or ABSOLUTE_MAX_CALLS. dry-plan shows it, with
repository bundles that would pass when it does not.

Two paths, chosen by the run (Context.reduced). When the digest is one shard and the
final prompt can carry it, every stage input is what it always was (the 2026-09-28 run's
inputs are reproduced byte for byte, test_stage_inputs.py). Otherwise the reduce stages
take the extraction answers, never the digest (map, then reduce):

  synthesis        the extract answers and a PR index; split in groups (and merged by
                   synthesis-merge-input: a synthesis of syntheses, itself split in another
                   level when the group answers do not fit one merge prompt) when they do not fit one prompt
  verify           the candidate and the digest sections of the PRs it cites, no others
  final            the synthesis, the verify answers, the verified quotes and the PR index

Each such stage writes <input>.scope.json beside its prompt (the digest keys the input held,
and for final the verified quotes): verify_e2e_result.py --input-scope refuses an answer that
used anything its input did not hold.

Prompt budget: the final prompt holds the digest, candidates, verify answers
and a deduplicated quote report, so it is the one most likely to pass 64 KiB.
Its fixed part plus a reserve is checked at extract-inputs, its size with the
real quotes at synthesis-input, and with the real candidates plus k verify
answers at verify-inputs (a bound from verify_answer_max_bytes, else a floor),
each time before the next model call. Only the verify answers' actual sizes
remain unknown until final-input, which then refuses an oversized prompt: a
dynamic failure that stays possible and is reported as such.

Path checks here (no symlink, regular file, size read before the bytes, a FIFO
refused without blocking) are a second line. Answers must come from a
host-owned pickup tree that A's pickup verifier has already accepted.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
WORKFLOW = REPO / "dot_agents/skills/researching-open-source-contributions/scripts/oss_workflow.py"
PROMPTS = HERE / "prompts"
LIMITS = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(HERE))
import retry_policy  # noqa: E402  (the retry share of a call plan, #280)
# Stage output limits (#248): each is stated in the prompt of the stage that answers and checked, with the same
# value, by verify_e2e_result.py and here; the worst-case plan (dry-plan) sizes the next stage's input from them.
MAX_CANDIDATES = LIMITS["synthesis_max_candidates"]     # candidates of one synthesis, group or merge answer
MAX_FINAL_PATTERNS = LIMITS["max_patterns"]
MAX_DROPPED = LIMITS["max_rejected"]
CANDIDATE_MAX_BYTES = LIMITS["candidate_max_bytes"]     # one candidate as a prompt block holds it (answer_bytes)
EXTRACT_ANSWER_MAX_BYTES = LIMITS["extract_answer_max_bytes"]   # one extract answer, the same measure
# What the extract prompt asks for, under that limit (#280): the check stays extract_answer_max_bytes, measured on the
# answer rewritten as indent-1 JSON, which a model cannot measure on the bytes it writes; the target leaves it a margin.
EXTRACT_ANSWER_TARGET_BYTES = LIMITS["extract_answer_target_bytes"]
EXTRACT_CAPS = {"patterns": LIMITS["max_patterns"], "observations": LIMITS["max_observations"],
                "rejected": LIMITS["max_rejected"]}
EXTRACT_CALLS = 5
# The harness ledger's absolute cap (analysis_call.sh): a plan over it is refused before any model call.
ABSOLUTE_MAX_CALLS = 40
CANDIDATE_ID = re.compile(r"C[1-9][0-9]?")


def model_call_plan(shards: int, lenses: int, max_candidates: int) -> dict:
    """The model calls of a digest-in-final run: every lens reads the one shard, then one synthesis, at most one
    verify per candidate, and one final, and the retry share of those calls (#280). (An extraction-only run takes its
    plan from worst_case_plan.)"""
    calls = {"extract": shards * lenses, "synthesis": 1, "verify": max_candidates, "final": 1}
    share = retry_policy.retry_share(calls)
    total = sum(calls.values()) + share["total"]
    return {"shards": shards, "lenses": lenses, **calls, "retry": share, "max_model_calls": total,
            "formula": "%d shards x %d lenses + 1 (synthesis) + %d (verify, at most max_candidates) + 1 (final) + %d "
                       "(retry share) = %d" % (shards, lenses, max_candidates, share["total"], total)}


MAX_MODEL_CALLS = model_call_plan(1, EXTRACT_CALLS, MAX_CANDIDATES)["max_model_calls"]
# Final-prompt budget (the final prompt is the largest: digest + candidates +
# verify answers + quote report). Checked at every stage before the next model
# call; a prompt over PROMPT_MAX_BYTES is refused, never cut.
STATEMENT_MAX_CHARS = LIMITS["candidate_statement_max_chars"]
VERIFY_ANSWER_MAX_BYTES = LIMITS["verify_answer_max_bytes"]
FINAL_RESERVE_BYTES = LIMITS["final_dynamic_reserve_bytes"]
# A verify answer adds its own block and, at most, its quotes again in the
# report; the smallest block a verify answer can leave is about this size.
VERIFY_FLOOR_BYTES = 512

# The corpus size is bounded by the request, not by this bundle: each collection run holds at most
# source.max_per_repository pull requests per repository, so a corpus holds at most that times the
# repositories of every collection adopted. The digest is sharded to keep each prompt in bounds.
PROMPT_MAX_BYTES = 64 * 1024
ANSWER_MAX_BYTES = 256 * 1024
CORPUS_MAX_BYTES = 4 * 1024 * 1024
CREDENTIAL_MARKERS = ("oss-research-credentials", "oauth-token", "CLAUDE_CODE_OAUTH_TOKEN")
EXTRACT_STEM = re.compile(r"extract-([a-z]+)(?:-\d{2})?")
PATTERN_ID = re.compile(r"PAT-\d{3}")
BASIS = ("fact", "inference", "unknown")
LEVELS = ("high", "medium", "low")
PROVENANCE = "independent-reimplementation"
CLAIM_KEYS = {"value", "basis", "evidence_links"}
CONFIDENCE_KEYS = {"level", "evidence", "limitations"}
MODEL_RECORD_KEYS = {"change_summary", "motivation", "review_judgment", "closure_reason", "test_evidence",
                     "pattern_ids", "evidence_links", "provenance_mode", "confidence"}
MODEL_PATTERN_KEYS = {"pattern_id", "description", "evidence_pr_ids", "applicability", "counterconditions",
                      "search_clues", "expected_tests", "maintainer_judgment_required", "provenance_mode", "confidence"}
# Filled from the corpus by assemble; a model answer must not carry them.
DETERMINISTIC_RECORD_KEYS = {"files_changed", "evidence_manifest", "license_spdx", "superseded_by"}
DETERMINISTIC_PATTERN_KEYS = {"generated_by", "source_licenses", "superseded_by", "pattern_history"}


class StageError(ValueError):
    pass


# -- loading ----------------------------------------------------------------


def load_workflow():
    existing = sys.modules.get("oss_workflow")
    if existing is not None and os.path.realpath(getattr(existing, "__file__", "")) == os.path.realpath(WORKFLOW):
        return existing
    spec = importlib.util.spec_from_file_location("oss_workflow", WORKFLOW)
    module = importlib.util.module_from_spec(spec)
    sys.modules["oss_workflow"] = module
    spec.loader.exec_module(module)
    return module


IN_FORCE = object()


def answer_target(target=IN_FORCE) -> int | None:
    """The extract answer size target a prompt states (#280): the one in force, or one an extract stage recorded. A
    target says nothing without a size limit or at or over it (an earlier or smaller limit), so it is then left out."""
    target = EXTRACT_ANSWER_TARGET_BYTES if target is IN_FORCE else target
    if target is None or EXTRACT_ANSWER_MAX_BYTES is None or target >= EXTRACT_ANSWER_MAX_BYTES:
        return None
    return target


def extract_limit_args(target=IN_FORCE) -> list[str]:
    """The analyzer's extract options for the limits in force: the count limits, the answer size limit (#248) and
    its target (#280, answer_target)."""
    limits = [item for kind, cap in EXTRACT_CAPS.items() for item in ("--max-" + kind, str(cap))]
    if EXTRACT_ANSWER_MAX_BYTES is not None:
        limits += ["--max-answer-bytes", str(EXTRACT_ANSWER_MAX_BYTES)]
    if answer_target(target) is not None:
        limits += ["--answer-target-bytes", str(answer_target(target))]
    return limits


class Context:
    """One workflow run's usable collect and digest checkpoints."""

    def __init__(self, run_dir: str, skills_root: str | None = None) -> None:
        self.wf = load_workflow()
        self.siblings = self.wf.Siblings(self.wf.resolve_siblings(skills_root))
        self.run = self.wf.Run(run_dir)
        self.request, self.state = self.run.load()
        self.run.revisions = self.siblings.revisions()
        stages = {}
        for stage in ("collect", "digest"):
            generation, reason = self.wf.usable(self.run, self.state, stage)
            if generation is None:
                raise StageError("%s checkpoint is not usable: %s" % (stage, reason))
            stages[stage] = generation
        self.corpus_path = self.wf.gen_dir(self.run, "collect", stages["collect"]) / "corpus.json"
        digest = self.wf.gen_dir(self.run, "digest", stages["digest"])
        self.digest_dir = digest / "extraction"
        self.extract_prompts = digest / "extract" / "prompts"
        self.corpus = read_json(self.corpus_path, CORPUS_MAX_BYTES, "corpus")
        records = self.corpus.get("records") if isinstance(self.corpus, dict) else None
        if not isinstance(records, list) or not records:
            raise StageError("corpus has no records")
        self.collections = self.state["stages"]["collect"]["generations"][stages["collect"]["generation"] - 1] \
            .get("details", {}).get("collections")
        self._check_scope(records)
        self.records = records
        self.keymap = read_json(self.digest_dir / "keymap.json", PROMPT_MAX_BYTES, "keymap")
        self.shards = len(self.keymap["shards"])
        self.references = list(self.request["source"].get("reference_repositories") or [])
        # as check_extraction and build_digest compare them (lower case), for the worst-case plan's own reads
        try:
            self.references_normalized = self.siblings.validate_corpus.normalize_reference_repositories(self.references)
        except ValueError as error:
            raise StageError("request reference_repositories: %s" % error) from error
        # Extraction-only reduce stages when the digest is sharded or its final prompt could not carry it.
        self.reduced = self.shards > 1 or self._final_cannot_carry_the_digest()
        self.mode = "extraction-only" if self.reduced else "digest-in-final"

    def _final_cannot_carry_the_digest(self) -> bool:
        try:
            fixed = len(legacy_final_prompt_text(self, [], {}, EMPTY_REPORT).encode("utf-8"))
        except StageError:
            return True
        return fixed + FINAL_RESERVE_BYTES > PROMPT_MAX_BYTES

    def _check_scope(self, records: list) -> None:
        """The corpus must fit the request's own caps (per repository, per collection run)."""
        source = self.request["source"]
        per_repository = source.get("max_per_repository")
        repositories = source.get("repositories") or []
        if type(per_repository) is not int or per_repository < 1:
            raise StageError("request max_per_repository must be a positive integer, got %r" % (per_repository,))
        collections = getattr(self, "collections", None)
        runs = sum(len(item.get("repositories") or []) for item in collections) if collections else len(repositories)
        allowed = per_repository * runs
        if len(records) > allowed:
            raise StageError("corpus has %d records; the request allows at most %d (%d per repository per collection "
                             "run, %d repository runs)" % (len(records), allowed, per_repository, runs))

    def reference_args(self) -> list[str]:
        args = []
        for repository in self.references:
            args += ["--reference-repo", repository]
        return args

    def pr_list(self) -> list[dict]:
        """What a prompt may name: digest key, PR-* id, URL, title, state. No local paths."""
        rows = []
        by_pr_id = {record.get("pr_id"): record for record in self.records}
        for key, section in self.keymap["sections"].items():
            record = by_pr_id.get(section.get("pr_id"), {})
            snapshot = record.get("evidence_snapshot") if isinstance(record.get("evidence_snapshot"), dict) else {}
            rows.append({"key": key, "pr_id": section.get("pr_id"), "url": section.get("url"),
                         "repository": section.get("repository"), "state": section.get("state"),
                         "title": section.get("title"),
                         "partial_categories": deepcopy(snapshot.get("partial_categories", []))})
        return rows

    def pr_index(self) -> list[dict]:
        """What a reduce stage knows of each PR: short meta from the digest keymap and the corpus.
        No body, no diff, no comment: the text stays in the digest."""
        rows = []
        by_pr_id = {record.get("pr_id"): record for record in self.records}
        for key, section in self.keymap["sections"].items():
            record = by_pr_id.get(section.get("pr_id"), {})
            pull = record.get("pull_request") if isinstance(record.get("pull_request"), dict) else {}
            snapshot = record.get("evidence_snapshot") if isinstance(record.get("evidence_snapshot"), dict) else {}
            files = pull.get("changed_files_count")
            if type(files) is not int:
                listed = snapshot.get("changed_files")
                files = len(listed) if isinstance(listed, list) else None
            rows.append({"key": key, "pr_id": section.get("pr_id"), "repository": section.get("repository"),
                         "number": pull.get("number"), "title": section.get("title"),
                         "outcome": pull.get("normalized_state") or section.get("state"), "files": files,
                         "url": section.get("url"),
                         "partial_categories": deepcopy(snapshot.get("partial_categories", []))})
        return rows

    def extract_stems(self) -> list[str]:
        """One extract prompt per lens, and per shard when the digest was sharded (``extract-<lens>-NN``)."""
        stems = sorted(path.stem for path in self.extract_prompts.glob("*.md"))
        lenses = sorted(self.siblings.build_prompts.LENSES)
        suffixes = [""] if self.shards == 1 else ["-%02d" % number for number in range(1, self.shards + 1)]
        expected = sorted("extract-%s%s" % (lens, suffix) for lens in lenses for suffix in suffixes)
        if stems != expected:
            raise StageError("digest must hold one extract prompt per lens (%s) and shard (%d)"
                             % (", ".join(lenses), self.shards))
        return stems

    def extract_prompt_texts(self, target=IN_FORCE) -> dict[str, str]:
        """The extract prompts of this digest, written again by the analyzer with the count limits the answers are
        checked against (EXTRACT_CAPS, from run_limits.json) and the answer size limit (extract_answer_max_bytes,
        #248), so the prompt and the checks say the same numbers, and the answer size target under it (#280):
        ``target`` as an extract stage recorded it (None: a stage from before the target), else the one in force.
        The digest's own prompts were written without them; only their names are taken from there."""
        stems = self.extract_stems()
        with tempfile.TemporaryDirectory(prefix="stage-extract-") as tmp:
            code, _, stderr = self.wf.run_main(self.siblings.build_prompts.main, [
                "extract", "--digest-dir", str(self.digest_dir), "--out", tmp, *extract_limit_args(target)])
            if code != 0:
                raise StageError("build_prompts extract failed: " + stderr.strip())
            built = sorted(path.stem for path in (Path(tmp) / "prompts").glob("*.md"))
            if built != stems:
                raise StageError("the analyzer wrote other extract prompts than the digest holds")
            return {stem: read_bounded(Path(tmp) / "prompts" / (stem + ".md"), 4 * PROMPT_MAX_BYTES,
                                       "extract prompt").decode("utf-8") for stem in stems}


# -- bounded, strict IO ------------------------------------------------------


def _unique_object(pairs):
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("duplicate JSON key")
    return document


def _refuse_constant(name):
    raise ValueError("non-standard JSON constant " + name)


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer before fstat can refuse it.
        fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise StageError("%s could not be opened without following a link: %s" % (label, path)) from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StageError("%s is not a regular file: %s" % (label, path))
        if info.st_size > limit:
            raise StageError("%s is %d bytes (> %d): %s" % (label, info.st_size, limit, path))
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(limit + 1)
    finally:
        if fd != -1:
            os.close(fd)
    if len(data) > limit:
        raise StageError("%s grew past %d bytes: %s" % (label, limit, path))
    return data


def read_json(path: Path, limit: int, label: str):
    return parse_json(read_bounded(Path(path), limit, label), label)


def parse_json(data: bytes, label: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise StageError("%s is not one strict JSON document: %s" % (label, error)) from error


def new_directory(path: str) -> Path:
    target = Path(path)
    if os.path.lexists(target):
        raise StageError("output must not exist yet: %s" % target)
    target.mkdir(mode=0o700, parents=False)
    return target


def write_prompt(directory: Path, text: str) -> dict:
    data = text.encode("utf-8")
    if len(data) > PROMPT_MAX_BYTES:
        raise StageError("prompt for %s is %d bytes (> %d)" % (directory.name, len(data), PROMPT_MAX_BYTES))
    if any(marker in text for marker in CREDENTIAL_MARKERS):
        raise StageError("credential marker in the prompt for " + directory.name)
    directory.mkdir(mode=0o700, exist_ok=False)
    path = directory / "task.txt"
    path.write_bytes(data)
    os.chmod(path, 0o444)
    return {"input_dir": directory.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def answer_bytes(value) -> int:
    """A model answer (or one candidate of it) measured the way a prompt block holds it: indent-1 JSON, UTF-8. The
    stage output limits are in this measure, whatever whitespace the model wrote."""
    return len(json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


LIMIT_TOKEN = re.compile(r"\{\{([a-z_]+)\}\}")


def output_limits() -> dict:
    """The stage output limits in force (module constants, read when used)."""
    return {"synthesis_max_candidates": MAX_CANDIDATES, "max_rejected": MAX_DROPPED, "max_patterns": MAX_FINAL_PATTERNS,
            "candidate_max_bytes": CANDIDATE_MAX_BYTES, "extract_answer_max_bytes": EXTRACT_ANSWER_MAX_BYTES,
            "verify_answer_max_bytes": VERIFY_ANSWER_MAX_BYTES}


def render_limits(text: str) -> str:
    """A prompt template with ``{{limit}}`` filled from the limits in force, so a prompt states the numbers its answer
    is checked against. A line naming a limit that is not set (None: a cap set from before that limit existed) is left
    out whole, so such a cap set renders the template as it was before the limit."""
    limits = output_limits()
    lines = []
    for line in text.split("\n"):
        names = LIMIT_TOKEN.findall(line)
        unknown = [name for name in names if name not in limits]
        if unknown:
            raise StageError("prompt template names unknown limits: " + ", ".join(unknown))
        if any(limits[name] is None for name in names):
            continue
        lines.append(LIMIT_TOKEN.sub(lambda match: str(limits[match.group(1)]), line))
    return "\n".join(lines)


def template(name: str) -> str:
    """A stage prompt template of this bundle, its limits filled in (render_limits)."""
    return render_limits(read_bounded(PROMPTS / name, PROMPT_MAX_BYTES, "template " + name).decode("utf-8"))


def block(title: str, value) -> str:
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    return "\n\n=== " + title + " ===\n" + body.rstrip() + "\n=== " + title + " 끝 ==="


EMPTY_REPORT = {"totals": {"verified": 0, "total": 0}, "failures": [], "verified_evidence": []}


def compact_report(report: dict) -> dict:
    """What a prompt needs from a check report: totals, failures without their text, and
    each verified (PR, quote) once. Deterministic: first appearance order."""
    seen, evidence = set(), []
    for item in report["verified_evidence"]:
        key = (item["pr_id"], item["quote"])
        if key not in seen:
            seen.add(key)
            evidence.append({"pr": item["pr"], "pr_id": item["pr_id"], "quote": item["quote"]})
    failures = [{"file": entry["file"], "pr": failure["pr"], "reason": failure["reason"]}
                for entry in report["files"] for failure in entry["failures"]]
    return {"totals": report["totals"], "failures": failures, "verified_evidence": evidence}


def final_prompt_text(context: "Context", candidates: list, verify: dict, report: dict) -> str:
    """The final prompt, built the same way for the early projections and for final-input."""
    if context.reduced:
        return reduced_final_prompt_text(context, candidates, verify, report)
    return legacy_final_prompt_text(context, candidates, verify, report)


QUOTE_HEAD = re.compile(r"- (PR-[0-9]+) (\S+)")


def quote_report_text(report: dict) -> str:
    """The quote report of the extraction-only final input, its verified quotes as the source text.

    In a JSON block a source " reads \\" and a source \\ reads \\\\; the 2026-10-05 r5 final answer copied that
    notation into a quote, which then matched neither its input nor the corpus (#82). So every key but
    verified_evidence stays one JSON line, and each verified quote is a head line ``- <PR-* id> <key>`` followed
    by the quote exactly as the source has it. A quote whose lines could be read another way (a line that looks
    like a head line, or a trailing newline) is refused, never cut or re-quoted."""
    lines = ["%s: %s" % (key, json.dumps(value, ensure_ascii=False))
             for key, value in report.items() if key != "verified_evidence"]
    lines.append("verified_evidence: 항목마다 머리줄 `- <PR-* id> <키>` 다음 줄부터 다음 머리줄 전까지가 원문 인용 "
                 "그대로다(JSON 이스케이프 없음).")
    for item in report["verified_evidence"]:
        quote = item["quote"]
        if not quote or quote.endswith("\n") or any(QUOTE_HEAD.fullmatch(line) for line in quote.split("\n")):
            raise StageError("verified quote of %s cannot be listed unambiguously as source text; refused, not cut"
                             % item["pr"])
        lines += ["- %s %s" % (item["pr_id"], item["pr"]), quote]
    return "\n".join(lines)


def parse_quote_lines(text: str) -> list:
    """The verified quotes of a quote_report_text block, in order: [{"pr_id", "pr", "quote"}]."""
    found, current = [], None
    for line in text.split("\n"):
        head = QUOTE_HEAD.fullmatch(line)
        if head:
            current = {"pr_id": head.group(1), "pr": head.group(2), "lines": []}
            found.append(current)
        elif current is not None:
            current["lines"].append(line)
    return [{"pr_id": item["pr_id"], "pr": item["pr"], "quote": "\n".join(item["lines"])} for item in found]


def reduced_final_prompt_text(context: "Context", candidates: list, verify: dict, report: dict, rows=None) -> str:
    """Extraction-only final: the PR index, the candidates, the verify answers and the verified quotes
    (as the source text, quote_report_text). ``rows`` replaces the run's PR index (the worst-case plan of a part)."""
    text = template("finalize-reduced.md")
    text += block("PR 색인", context.pr_index() if rows is None else rows)
    text += block("후보", candidates)
    for stem in sorted(verify):
        text += block("반박 검증 " + stem, verify[stem])
    text += block("인용 확인 보고서", quote_report_text(report))
    return text


def legacy_final_prompt_text(context: "Context", candidates: list, verify: dict, report: dict) -> str:
    """The digest-carrying final prompt (the one the 2026-09-28 run was given)."""
    shards = "\n\n".join(
        read_bounded(context.digest_dir / name, PROMPT_MAX_BYTES, "digest shard").decode("utf-8").rstrip()
        for name in context.keymap["shards"])
    text = template("finalize.md")
    text += block("PR 목록", context.pr_list())
    text += block("다이제스트", shards)
    text += block("후보", candidates)
    for stem in sorted(verify):
        text += block("반박 검증 " + stem, verify[stem])
    text += block("인용 확인 보고서", report)
    return text


def budget_refusal(stage: str, needed: int, what: str) -> StageError:
    return StageError("%s: the final prompt would need %d bytes (%s), over %d; refused before the next model "
                      "call (nothing is cut)" % (stage, needed, what, PROMPT_MAX_BYTES))


# -- answers -----------------------------------------------------------------


def load_results(paths: list[str], expected_stems: list[str], label: str) -> dict[str, object]:
    """Exactly one ``<stem>.result.json`` per expected stem, each a bounded strict JSON object."""
    found = {}
    for path in paths:
        name = Path(path).name
        if not name.endswith(".result.json"):
            raise StageError("%s must be named <prompt stem>.result.json: %s" % (label, name))
        stem = name[: -len(".result.json")]
        if stem in found:
            raise StageError("duplicate %s for %s" % (label, stem))
        document = read_json(Path(path), ANSWER_MAX_BYTES, label)
        if not isinstance(document, dict):
            raise StageError("%s %s is not a JSON object" % (label, name))
        found[stem] = (path, document)
    if sorted(found) != sorted(expected_stems):
        raise StageError("%s must be exactly %s, got %s" % (label, sorted(expected_stems), sorted(found)))
    return found


def check_quotes(context: Context, paths: list[str]) -> dict:
    """Run check_extraction.py; a failed quote is recorded and dropped, never repaired."""
    code, stdout, stderr = context.wf.run_main(context.siblings.check_extraction.main,
                                               [str(context.corpus_path)] + list(paths) + context.reference_args())
    if code not in (0, 1):
        raise StageError("check_extraction refused the results: " + stderr.strip())
    report = json.loads(stdout)
    # File paths are local; prompts carry only the result names.
    for entry in report["files"]:
        entry["file"] = Path(entry["file"]).name
    for entry in report["verified_evidence"]:
        entry["file"] = Path(entry["file"]).name
    report["clean_results"] = [entry["file"] for entry in report["files"] if not entry["failures"]]
    return report


def parse_synthesis(context: Context, path: str) -> list[dict]:
    answer = read_json(Path(path), ANSWER_MAX_BYTES, "synthesis answer")
    if not isinstance(answer, dict) or set(answer) != {"candidates", "dropped"}:
        raise StageError("synthesis answer must hold exactly candidates and dropped")
    candidates, dropped = answer["candidates"], answer["dropped"]
    if not isinstance(candidates, list) or not isinstance(dropped, list):
        raise StageError("synthesis candidates and dropped must be arrays")
    if len(candidates) > MAX_CANDIDATES or len(dropped) > MAX_DROPPED:
        raise StageError("synthesis may hold at most %d candidates and %d dropped ideas (got %d and %d)"
                         % (MAX_CANDIDATES, MAX_DROPPED, len(candidates), len(dropped)))
    for index, item in enumerate(dropped):
        if not isinstance(item, dict) or set(item) != {"idea", "reason"} or not all(isinstance(v, str) for v in item.values()):
            raise StageError("synthesis dropped[%d] must be {idea, reason} strings" % index)
    seen = set()
    keys = context.keymap["sections"]
    for index, candidate in enumerate(candidates):
        label = "synthesis candidates[%d]" % index
        if not isinstance(candidate, dict) or not {"id", "statement", "cited_prs"} <= set(candidate) \
                or set(candidate) - {"id", "statement", "cited_prs", "found_by"}:
            raise StageError(label + " must hold id, statement, cited_prs, and optional found_by")
        if not isinstance(candidate["id"], str) or not CANDIDATE_ID.fullmatch(candidate["id"]) or candidate["id"] in seen:
            raise StageError(label + " id must be a unique C1 … C99")
        seen.add(candidate["id"])
        if CANDIDATE_MAX_BYTES is not None and answer_bytes(candidate) > CANDIDATE_MAX_BYTES:
            raise StageError(label + " is %d bytes as a prompt holds it (> candidate_max_bytes %d); refused, not cut"
                             % (answer_bytes(candidate), CANDIDATE_MAX_BYTES))
        if not isinstance(candidate["statement"], str) or not candidate["statement"].strip() \
                or len(candidate["statement"]) > STATEMENT_MAX_CHARS:
            raise StageError(label + " statement must be a nonempty string of at most %d characters"
                             % STATEMENT_MAX_CHARS)
        if "found_by" in candidate and not isinstance(candidate["found_by"], str):
            raise StageError(label + " found_by must be a string")
        cited = candidate["cited_prs"]
        if not isinstance(cited, list) or not all(isinstance(key, str) and key in keys for key in cited) \
                or len(set(cited)) < 2:
            raise StageError(label + " must cite at least two distinct digest keys")
    return candidates


# -- worst-case plan (#248): every stage input at the stage output limits, before any model call ----------------
#
# The answers are not known before the extract calls, so each stage input is sized with every answer at its limit
# (extract_answer_max_bytes, synthesis_max_candidates x candidate_max_bytes, verify_answer_max_bytes) and every
# quote at the widest 300 characters (validate_corpus MAX_QUOTE_LENGTH) its PR's own text holds. Calls are packed in
# order (next fit), so with every answer within the limits each call holds at least ``per_call`` inputs and a level
# needs at most ceil(inputs / per_call) calls: the real run never needs more than the plan.

# An extract answer's share of its group's quote report: its verified quotes once each (an adopted answer has no
# failed quote: Q1) and its file entry. At most the answer's own bytes plus this.
REPORT_SHARE_SLACK_BYTES = 2048
# A verify answer in the final prompt: its block (indent-1 JSON, which may be a little larger than the answer bytes)
# and its verified quotes again in the quote report: at most twice the answer bytes plus this.
VERIFY_BLOCK_SLACK_BYTES = 512
WIDE_COUNT = 9999          # report totals measured at four digits
WIDE_ID = "C99"            # the longest candidate id (CANDIDATE_ID)


def json_width(char: str) -> int:
    """Bytes one character takes inside a JSON string of a prompt block (indent-1, ensure_ascii off)."""
    if char in '"\\':
        return 2
    if ord(char) < 0x20:
        return 2 if char in "\b\f\n\r\t" else 6
    return len(char.encode("utf-8"))


def widest_window(text: str, length: int, width) -> int:
    """The most bytes any ``length`` consecutive characters of ``text`` take."""
    widths = [width(char) for char in text]
    total = best = sum(widths[:length])
    for index in range(length, len(widths)):
        total += widths[index] - widths[index - length]
        best = max(best, total)
    return best


class PlanInputs:
    """What the worst-case plan needs of a run, or of a part of its corpus (the split suggestion): the corpus, the
    digest and the limits, never an answer."""

    def __init__(self, *, mode, shards, stems, rows, quotes, extract_bytes, verify_bytes, legacy=None):
        self.mode, self.shards, self.stems, self.rows = mode, shards, stems, rows
        self.quotes = quotes                    # key -> (widest quote in a JSON block, widest raw quote)
        self.extract_bytes = extract_bytes      # the largest extract prompt (known: the digest's own)
        self.verify_bytes = verify_bytes        # the verify prompt of a limit-size candidate citing the 2 largest PRs
        self.legacy = legacy                    # digest-in-final: {"synthesis": fixed bytes, "final": fixed bytes}
        self.pr_ids = {row["key"]: row["pr_id"] for row in rows}
        self.repositories = sorted({row["repository"] for row in rows})


def record_quote_bounds(validate, record, references) -> tuple[int, int]:
    """(JSON block bytes, raw bytes) of the widest quote a record can give: MAX_QUOTE_LENGTH characters of one of its
    quotable texts (what check_extraction accepts a quote from)."""
    texts = validate.quotable_texts(record, references) or [""]
    length = validate.MAX_QUOTE_LENGTH
    return (max(widest_window(text, length, json_width) for text in texts),
            max(widest_window(text, length, lambda char: len(char.encode("utf-8"))) for text in texts))


def worst_candidate(cited: list[str]) -> dict:
    """A candidate of exactly candidate_max_bytes (or as close as the characters allow) citing ``cited``."""
    candidate = {"id": WIDE_ID, "statement": "", "cited_prs": list(cited), "found_by": ""}
    budget = CANDIDATE_MAX_BYTES - answer_bytes(candidate)
    wide = min(STATEMENT_MAX_CHARS, max(budget, 0) // 3)
    candidate["statement"] = "가" * wide + "a" * min(STATEMENT_MAX_CHARS - wide, max(budget - 3 * wide, 0))
    return candidate


def candidate_shape(keys: list[str]) -> tuple[int, int]:
    """(the most cited PRs one candidate within candidate_max_bytes can hold, the most lines it then takes): every
    extra line of a nested block adds its indent."""
    shortest = min(keys, key=len)
    cited = 2
    while answer_bytes({"id": "C1", "statement": "x", "cited_prs": [shortest] * (cited + 1)}) <= CANDIDATE_MAX_BYTES:
        cited += 1
    return cited, cited + 7


def top_sum(values: list[int], count: int) -> int:
    return sum(sorted(values, reverse=True)[:count])


def merge_input_bound(inputs: PlanInputs, name: str) -> int:
    """One group (or lower merge) answer in a merge prompt: its block of synthesis_max_candidates limit-size
    candidates and, for every PR they can cite, its verified quote or a no-quote entry per citing candidate."""
    cited, lines = candidate_shape(list(inputs.quotes))
    empty = len(block("그룹 종합 " + name, {"candidates": []}).encode("utf-8"))
    candidates = MAX_CANDIDATES * (CANDIDATE_MAX_BYTES + 2 * lines + 2) + 8
    per_pr = []
    for key, (quote_json, _) in inputs.quotes.items():
        quoted = answer_bytes({"pr": key, "pr_id": inputs.pr_ids[key], "quote": ""}) + quote_json + 2 * 5 + 2
        missing = answer_bytes({"group": name, "candidate": WIDE_ID, "pr": key}) + 2 * 5 + 2
        per_pr.append(max(quoted, MAX_CANDIDATES * missing))
    return empty + candidates + top_sum(per_pr, MAX_CANDIDATES * cited)


def final_bound(inputs: PlanInputs) -> int:
    """The extraction-only final prompt with synthesis_max_candidates limit-size candidates, as many verify answers
    of verify_answer_max_bytes, and for every PR they can cite its widest verified quote (or no-quote entries)."""
    cited, lines = candidate_shape(list(inputs.quotes))
    empty_report = {"totals": {"verified": WIDE_COUNT, "total": WIDE_COUNT}, "verified_evidence": [], "no_quote": []}
    text = template("finalize-reduced.md") + block("PR 색인", inputs.rows) + block("후보", []) \
        + block("인용 확인 보고서", quote_report_text(empty_report))
    candidates = MAX_CANDIDATES * (CANDIDATE_MAX_BYTES + lines + 2) + 8
    verify_block = len(block("반박 검증 verify-" + WIDE_ID, {}).encode("utf-8")) - 2
    verify = MAX_CANDIDATES * (verify_block + 2 * VERIFY_ANSWER_MAX_BYTES + VERIFY_BLOCK_SLACK_BYTES)
    per_pr = []
    for key, (_, quote_raw) in inputs.quotes.items():
        quoted = len(("- %s %s\n" % (inputs.pr_ids[key], key)).encode("utf-8")) + quote_raw + 1
        missing = len(json.dumps({"candidate": WIDE_ID, "pr": key}, ensure_ascii=False).encode("utf-8")) + 2
        per_pr.append(max(quoted, MAX_CANDIDATES * missing))
    return len(text.encode("utf-8")) + candidates + verify + top_sum(per_pr, MAX_CANDIDATES * cited)


def synthesis_fixed(rows: list, note: str | None) -> int:
    report = {"totals": {"verified": WIDE_COUNT, "total": WIDE_COUNT}, "files": [], "verified_evidence": []}
    text = template("synthesize-reduced.md") + (block("범위", note) if note else "") + block("PR 색인", rows) \
        + block("인용 확인 보고서", report)
    return len(text.encode("utf-8"))


def merge_fixed(rows: list, note: str | None) -> int:
    report = {"totals": {"verified": WIDE_COUNT, "total": WIDE_COUNT}, "verified_evidence": [], "no_quote": []}
    text = template("synthesize-merge.md") + (block("범위", note) if note else "") + block("PR 색인", rows) \
        + block("인용 확인 보고서", report)
    return len(text.encode("utf-8"))


def extract_answer_bound(stem: str) -> int:
    """One extract answer in a synthesis prompt: its block, and its share of the quote report."""
    return len(block("추출 결과 " + stem, {}).encode("utf-8")) - 2 + 2 * EXTRACT_ANSWER_MAX_BYTES \
        + REPORT_SHARE_SLACK_BYTES


def widest(names: list[str], count: int) -> list[str]:
    return sorted(names, key=len, reverse=True)[:count]


def pack_level(names: list[str], bound, fixed, note) -> dict:
    """One reduce level in the worst case: every input at ``bound(name)``. One call (no note) when they all fit,
    else as many per call as fit beside the widest note."""
    single = fixed(None) + sum(bound(name) for name in names)
    if single <= PROMPT_MAX_BYTES:
        return {"inputs": len(names), "calls": 1, "per_call": len(names), "worst_bytes": single, "ok": True}
    largest = max(bound(name) for name in names)
    per_call = 0
    while per_call < len(names):
        size = fixed(note(widest(names, per_call + 1))) + (per_call + 1) * largest
        if size > PROMPT_MAX_BYTES:
            break
        per_call += 1
    worst = fixed(note(widest(names, max(per_call, 1)))) + max(per_call, 1) * largest
    calls = -(-len(names) // per_call) if per_call else len(names)
    return {"inputs": len(names), "calls": calls, "per_call": per_call, "worst_bytes": worst, "ok": per_call >= 2}


def synthesis_group_level(rows: list, stems: list[str]) -> dict:
    """The synthesis groups in the worst case: every extract answer at extract_answer_max_bytes."""
    level = pack_level(stems, extract_answer_bound, lambda note: synthesis_fixed(rows, note),
                       lambda names: group_note(99, 99, names))
    level["ok"] = level["per_call"] >= 1
    return level


def synthesis_group_bound(rows: list, stems: list[str]) -> int:
    """The most synthesis groups the answers within the limits can need (one per answer without a size limit)."""
    if EXTRACT_ANSWER_MAX_BYTES is None:
        return len(stems)
    level = synthesis_group_level(rows, stems)
    return level["calls"] if level["ok"] else len(stems)


def synthesis_levels(inputs: PlanInputs, groups: int | None = None) -> tuple[list, list]:
    """The reduce levels of an extraction-only run: the synthesis groups (unless ``groups`` gives their real number),
    then merge levels until one call. Returns (levels, refusals)."""
    levels, refusals = [], []
    if groups is None:
        if EXTRACT_ANSWER_MAX_BYTES is None:
            return [{"stage": "synthesis", "level": 1, "inputs": len(inputs.stems), "calls": len(inputs.stems),
                     "per_call": 0, "worst_bytes": None, "ok": False}], [
                "synthesis: no extract_answer_max_bytes in the limits, so the groups are not bounded"]
        level = synthesis_group_level(inputs.rows, inputs.stems)
        levels.append({"stage": "synthesis", "level": 1, **level})
        if not level["ok"]:
            refusals.append("synthesis: one extract answer at extract_answer_max_bytes %d makes a group prompt of %d "
                            "bytes (> %d)" % (EXTRACT_ANSWER_MAX_BYTES, level["worst_bytes"], PROMPT_MAX_BYTES))
            return levels, refusals
        groups = level["calls"]
    if groups == 1:
        return levels, refusals
    if CANDIDATE_MAX_BYTES is None:
        levels.append({"stage": "merge", "level": 2, "inputs": groups, "calls": None, "per_call": 0,
                       "worst_bytes": None, "ok": False})
        return levels, refusals + ["merge: no candidate_max_bytes in the limits, so the merge is not bounded"]
    names = ["synthesis-%02d" % number for number in range(1, groups + 1)]
    number = 2
    while True:
        level = pack_level(names, lambda name: merge_input_bound(inputs, name),
                           lambda note: merge_fixed(inputs.rows, note),
                           lambda members: merge_note(99, 99, 99, members))     # measured as synthesis-merge-input does
        levels.append({"stage": "merge", "level": number, **level})
        if level["calls"] == 1:
            return levels, refusals
        if not level["ok"]:
            refusals.append("merge level %d: %d group answers at the limits need %d calls (%d per call): another level "
                            "would merge nothing" % (number, level["inputs"], level["calls"], level["per_call"]))
            return levels, refusals
        names = ["merge-%02d-%02d" % (number, index) for index in range(1, level["calls"] + 1)]
        number += 1


def worst_case_plan(inputs: PlanInputs) -> dict:
    """Every stage input at the stage output limits, the reduce levels, and the model calls of the run, before any
    model call (#248). ``ok`` is false when a stage input can pass PROMPT_MAX_BYTES or the calls ABSOLUTE_MAX_CALLS."""
    lenses = len(inputs.stems) // inputs.shards
    stages = [{"stage": "extract", "calls": len(inputs.stems), "worst_bytes": inputs.extract_bytes,
               "ok": inputs.extract_bytes <= PROMPT_MAX_BYTES}]
    refusals = [] if stages[0]["ok"] else ["extract: the largest extract prompt is %d bytes (> %d)"
                                            % (inputs.extract_bytes, PROMPT_MAX_BYTES)]
    if inputs.mode == "extraction-only":
        levels, problems = synthesis_levels(inputs)
        refusals += problems
        final = final_bound(inputs) if CANDIDATE_MAX_BYTES is not None else None
    else:
        single = inputs.legacy["synthesis"] + (sum(extract_answer_bound(stem) for stem in inputs.stems)
                                               if EXTRACT_ANSWER_MAX_BYTES is not None else 0)
        levels = [{"stage": "synthesis", "level": 1, "inputs": len(inputs.stems), "calls": 1,
                   "per_call": len(inputs.stems), "worst_bytes": single,
                   "ok": EXTRACT_ANSWER_MAX_BYTES is not None and single <= PROMPT_MAX_BYTES}]
        if not levels[0]["ok"]:
            refusals.append("synthesis: the digest-in-final synthesis is one call and is %s bytes at the limits"
                            % single)
        final = None
    stages += levels
    synthesis = sum(level["calls"] or 0 for level in levels)
    stages.append({"stage": "verify", "calls": MAX_CANDIDATES, "worst_bytes": inputs.verify_bytes,
                   "ok": inputs.verify_bytes is not None and inputs.verify_bytes <= PROMPT_MAX_BYTES})
    if not stages[-1]["ok"]:
        refusals.append("verify: a limit-size candidate citing the two largest PR sections makes a verify prompt of "
                        "%s bytes (> %d)" % (inputs.verify_bytes, PROMPT_MAX_BYTES))
    stages.append({"stage": "final", "calls": 1, "worst_bytes": final,
                   "ok": final is not None and final <= PROMPT_MAX_BYTES})
    if not stages[-1]["ok"]:
        refusals.append("final: %s at the limits (> %d)" % ("%d bytes" % final if final is not None else
                                                            "not bounded (digest-in-final carries every quote)",
                                                            PROMPT_MAX_BYTES))
    extract = len(inputs.stems)
    planned = extract + synthesis + MAX_CANDIDATES + 1
    # the retry share of these calls counts against the cap too (#280): a plan that fits only without it is refused
    share = retry_policy.retry_share({"extract": extract, "synthesis": synthesis, "verify": MAX_CANDIDATES, "final": 1})
    total = planned + share["total"]
    if total > ABSOLUTE_MAX_CALLS:
        refusals.append("calls: %d model calls at the limits (%d planned + %d retry share), over the absolute cap %d"
                        % (total, planned, share["total"], ABSOLUTE_MAX_CALLS))
    counts = [level["calls"] or 0 for level in levels]
    plan = {"mode": inputs.mode, "shards": inputs.shards, "lenses": lenses, "extract": extract, "synthesis": synthesis,
            "synthesis_levels": counts, "verify": MAX_CANDIDATES, "final": 1, "retry": share, "max_model_calls": total,
            "formula": "%d shards x %d lenses + %d (synthesis levels %s) + %d (verify, at most "
                       "synthesis_max_candidates) + 1 (final) + %d (retry share: per call %d, per stage %d) = %d, "
                       "every answer at the stage output limits"
                       % (inputs.shards, lenses, synthesis, " + ".join(map(str, counts)), MAX_CANDIDATES,
                          share["total"], share["per_call"], share["per_stage"], total),
            "inputs": {"max_candidates": MAX_CANDIDATES, "shards": inputs.shards, "lenses": lenses,
                       "limits": output_limits()}}
    return {"ok": not refusals, "mode": inputs.mode, "cap": PROMPT_MAX_BYTES, "max_calls": ABSOLUTE_MAX_CALLS,
            "records": len(inputs.rows), "repositories": inputs.repositories, "stages": stages,
            "levels": len(levels), "synthesis_levels": counts, "total_calls": total, "planned_calls": planned,
            "retry_calls": share["total"], "call_plan": plan, "refusals": refusals}


def quote_bounds(context: Context) -> dict:
    """Per digest key, the widest quote its record can give (record_quote_bounds); computed once per run."""
    if getattr(context, "_quote_bounds", None) is None:
        validate = context.siblings.validate_corpus
        by_pr_id = {record.get("pr_id"): record for record in context.records}
        context._quote_bounds = {key: record_quote_bounds(validate, by_pr_id[section["pr_id"]],
                                                          context.references_normalized)
                                 for key, section in context.keymap["sections"].items()}
    return context._quote_bounds


def section_sizes(context: Context, keymap: dict, digest_dir: Path) -> dict:
    sections = {}
    for name in keymap["shards"]:
        text = read_bounded(digest_dir / name, PROMPT_MAX_BYTES * 4, "digest shard").decode("utf-8")
        sections.update(context.siblings.build_prompts._sections(text, keymap["sections"]))
    return {key: len(text.encode("utf-8")) for key, text in sections.items()}


def worst_verify_bytes(context: Context, keymap: dict, digest_dir: Path, reduced: bool) -> int | None:
    """The verify prompt of a candidate of candidate_max_bytes citing the two PRs that make it largest (the first two
    a verify input must hold, fit_verify_prompt): a cited PR brings its section and leaves the title list of the
    others, so the two with the most section bytes over their title line. None when candidates are not bounded."""
    if CANDIDATE_MAX_BYTES is None:
        return None
    sizes = section_sizes(context, keymap, digest_dir)

    def gain(key):
        section = keymap["sections"][key]
        line = "- " + key + " — " + str(section["title"]) + " (" + str(section["state"]) + ")\n"
        return sizes[key] - len(line.encode("utf-8"))

    largest = sorted(sizes, key=lambda key: (-gain(key), key))[:2]
    candidate = worst_candidate(largest)
    if reduced:
        return len(verify_prompt(context, candidate, keymap, digest_dir).encode("utf-8"))
    with tempfile.TemporaryDirectory(prefix="stage-plan-") as tmp:
        (Path(tmp) / "candidates.json").write_text(json.dumps([candidate], ensure_ascii=False), encoding="utf-8")
        code, _, stderr = context.wf.run_main(context.siblings.build_prompts.main, [
            "verify", "--digest-dir", str(digest_dir), "--candidates", str(Path(tmp) / "candidates.json"),
            "--out", str(Path(tmp) / "build")])
        if code != 0:
            raise StageError("build_prompts verify failed: " + stderr.strip())
        return len((Path(tmp) / "build" / "prompts" / ("verify-" + WIDE_ID + ".md")).read_bytes())


def legacy_fixed(context: Context, rows: list, shards_text: str) -> dict:
    """The fixed parts of the digest-in-final synthesis and final prompts (PR list, and for final the digest)."""
    synthesis = template("synthesize.md") + block("PR 목록", rows) + block(
        "인용 확인 보고서", {"totals": {"verified": WIDE_COUNT, "total": WIDE_COUNT}, "files": [], "verified_evidence": []})
    final = template("finalize.md") + block("PR 목록", rows) + block("다이제스트", shards_text) + block("후보", []) \
        + block("인용 확인 보고서", EMPTY_REPORT)
    return {"synthesis": len(synthesis.encode("utf-8")), "final": len(final.encode("utf-8"))}


def plan_inputs(context: Context) -> PlanInputs:
    """The worst-case plan inputs of this run: its digest, its extract prompts, its PR index and its quotes."""
    texts = context.extract_prompt_texts()
    shards_text = "\n\n".join(read_bounded(context.digest_dir / name, PROMPT_MAX_BYTES, "digest shard")
                              .decode("utf-8").rstrip() for name in context.keymap["shards"])
    return PlanInputs(mode=context.mode, shards=context.shards, stems=context.extract_stems(), rows=context.pr_index(),
                      quotes=quote_bounds(context),
                      extract_bytes=max(len(text.encode("utf-8")) for text in texts.values()),
                      verify_bytes=worst_verify_bytes(context, context.keymap, context.digest_dir, context.reduced),
                      legacy=None if context.reduced else legacy_fixed(context, context.pr_list(), shards_text))


def part_inputs(context: Context, repositories: list[str]) -> PlanInputs:
    """The plan inputs a run of only these repositories' PRs would have: the same sections, re-sharded the way the
    digest shards them (build_digest.plan_shards at the digest's own max_bytes) and given the analyzer's own extract
    prompts. The verify prompt is the whole run's (an upper bound: a part's two largest sections and its title list
    are no larger)."""
    digest = context.siblings.build_digest
    keys = [key for key, section in context.keymap["sections"].items() if section["repository"] in repositories]
    by_pr_id = {record.get("pr_id"): record for record in context.records}
    records = [by_pr_id[context.keymap["sections"][key]["pr_id"]] for key in keys]
    sections = {key: digest.render_section(key, record, context.references_normalized)
                for key, record in zip(keys, records)}
    shards = digest.plan_shards(keys, records, sections, context.keymap["max_bytes"])
    names = ["digest.md"] if len(shards) == 1 else ["digest-%02d.md" % index for index in range(1, len(shards) + 1)]
    keymap = deepcopy(context.keymap)
    keymap["shards"], keymap["sections"] = names, {}
    with tempfile.TemporaryDirectory(prefix="stage-plan-") as tmp:
        directory = Path(tmp) / "digest"
        directory.mkdir()
        for name, shard in zip(names, shards):
            (directory / name).write_text("\n\n".join(sections[key] for key in shard) + "\n", encoding="utf-8")
            for key in shard:
                keymap["sections"][key] = dict(context.keymap["sections"][key], shard=name)
        keymap["sections"] = {key: keymap["sections"][key] for key in keys}
        (directory / "keymap.json").write_text(json.dumps(keymap), encoding="utf-8")
        code, _, stderr = context.wf.run_main(context.siblings.build_prompts.main, [
            "extract", "--digest-dir", str(directory), "--out", str(Path(tmp) / "build"), *extract_limit_args()])
        if code != 0:
            raise StageError("build_prompts extract failed: " + stderr.strip())
        extract = max(path.stat().st_size for path in (Path(tmp) / "build" / "prompts").glob("*.md"))
        shards_text = "\n\n".join((directory / name).read_text(encoding="utf-8").rstrip() for name in names)
        listed = [row for row in context.pr_list() if row["key"] in keys]
        legacy = legacy_fixed(context, listed, shards_text)
        reduced = len(shards) > 1 or legacy["final"] + FINAL_RESERVE_BYTES > PROMPT_MAX_BYTES
        # a part whose own digest the final prompt carries is verified on that whole digest
        verify = None if reduced else worst_verify_bytes(context, keymap, directory, False)
    rows = [row for row in context.pr_index() if row["key"] in keys]
    lenses = sorted(context.siblings.build_prompts.LENSES)
    suffixes = [""] if len(shards) == 1 else ["-%02d" % number for number in range(1, len(shards) + 1)]
    stems = sorted("extract-%s%s" % (lens, suffix) for lens in lenses for suffix in suffixes)
    quotes = quote_bounds(context)
    return PlanInputs(mode="extraction-only" if reduced else "digest-in-final", shards=len(shards), stems=stems,
                      rows=rows, quotes={key: quotes[key] for key in keys}, extract_bytes=extract,
                      verify_bytes=worst_verify_bytes(context, context.keymap, context.digest_dir, True)
                      if reduced else verify, legacy=None if reduced else legacy)


def split_suggestion(context: Context) -> list:
    """Repository bundles whose own runs would each pass the worst-case plan: the repositories of one owner together
    first (an owner whose bundle does not pass is taken repository by repository), the largest first, each joining
    the first bundle it still passes with. A bundle that does not pass alone is listed as such (collect less)."""
    counts = {}
    for row in context.pr_index():
        counts[row["repository"]] = counts.get(row["repository"], 0) + 1
    owners = {}
    for repository in counts:
        owners.setdefault(repository.split("/", 1)[0], []).append(repository)
    plans = {}

    def plan_of(repositories):
        key = tuple(sorted(repositories))
        if key not in plans:
            plan = worst_case_plan(part_inputs(context, list(key)))
            plan["runs"] = runnable(plan)
            plans[key] = plan
        return plans[key]

    units = []
    for repositories in sorted(owners.values(), key=lambda group: (-sum(counts[r] for r in group), group)):
        if len(repositories) > 1 and not plan_of(repositories)["runs"]:
            units += [[repository] for repository in sorted(repositories, key=lambda r: (-counts[r], r))]
        else:
            units.append(sorted(repositories))
    bundles = []
    for unit in units:
        for bundle in bundles:
            if plan_of(bundle + unit)["runs"]:
                bundle += unit
                break
        else:
            bundles.append(list(unit))
    suggestion = []
    for bundle in bundles:
        plan = plan_of(bundle)
        suggestion.append({"repositories": sorted(bundle), "records": sum(counts[r] for r in bundle),
                           "mode": plan["mode"], "shards": plan["call_plan"]["shards"],
                           "synthesis_levels": plan["synthesis_levels"], "total_calls": plan["total_calls"],
                           "ok": plan["runs"], "refusals": plan["refusals"]})
    return suggestion


def runnable(plan: dict) -> bool:
    """A bundle the split may propose: its worst-case plan passes, or it is small enough for the digest-in-final path
    (its extract prompts fit, at most ABSOLUTE_MAX_CALLS calls), which keeps that path's dynamic checks (README 3.4)
    instead of a guarantee."""
    if plan["ok"]:
        return True
    extract = plan["stages"][0]
    return plan["mode"] == "digest-in-final" and extract["ok"] and plan["total_calls"] <= ABSOLUTE_MAX_CALLS


# -- commands ----------------------------------------------------------------


def planned(context: Context) -> bool:
    """An extraction-only run under stage output limits that bound every reduce input: its worst-case plan, made at
    extract-inputs, governs the later budgets (#248)."""
    return context.reduced and CANDIDATE_MAX_BYTES is not None and EXTRACT_ANSWER_MAX_BYTES is not None


def cmd_extract_inputs(context: Context, args) -> dict:
    """The extract prompts of the run and its call plan. An extraction-only run's plan is the worst-case plan
    (dry-plan, #248): every reduce level at the stage output limits; a run whose plan does not pass (a stage input
    over PROMPT_MAX_BYTES or more than ABSOLUTE_MAX_CALLS calls) is refused here, before its first model call."""
    lenses = len(context.siblings.build_prompts.LENSES)
    worst = None
    if context.reduced:
        worst = worst_case_plan(plan_inputs(context))
        plan = worst["call_plan"]
    else:
        plan = model_call_plan(context.shards, lenses, MAX_CANDIDATES)
        plan["inputs"] = {"max_candidates": MAX_CANDIDATES, "shards": context.shards, "lenses": lenses}
    # Every prompt is checked before anything is written: one over the cap refuses the whole stage.
    texts = context.extract_prompt_texts()
    oversize = ["%s is %d bytes" % (stem, len(text.encode("utf-8"))) for stem, text in sorted(texts.items())
                if len(text.encode("utf-8")) > PROMPT_MAX_BYTES]
    if oversize:
        raise StageError("extract prompts over %d bytes, refused before anything is written (lower digest "
                         "--max-bytes): %s" % (PROMPT_MAX_BYTES, "; ".join(oversize)))
    fixed = len(final_prompt_text(context, [], {}, EMPTY_REPORT).encode("utf-8"))
    final_budget = {"fixed_bytes": fixed, "reserve_bytes": FINAL_RESERVE_BYTES, "cap": PROMPT_MAX_BYTES,
                    "note": "the reserve is a policy floor, not a worst-case guarantee"}
    if fixed + FINAL_RESERVE_BYTES > PROMPT_MAX_BYTES:
        raise budget_refusal("extract-inputs", fixed + FINAL_RESERVE_BYTES,
                             "fixed part %d + reserve %d for candidates, verify answers and quotes"
                             % (fixed, FINAL_RESERVE_BYTES))
    if worst is not None and not worst["ok"]:
        raise StageError("extract-inputs: the worst-case plan does not pass, refused before any model call "
                         "(dry-plan shows the stages and a repository split): %s" % "; ".join(worst["refusals"]))
    out = new_directory(args.out)
    written = []
    for stem, text in sorted(texts.items()):
        entry = write_prompt(out / stem, text)
        entry["result_name"] = stem + ".result.json"
        written.append(entry)
    summary = {"stage": "extract", "mode": context.mode, "calls": written, "max_model_calls": plan["max_model_calls"],
               "call_plan": plan, "final_budget": final_budget}
    if answer_target() is not None:
        # the prompts state it (#280); continue-plan writes them again with this target to compare their bytes
        summary["extract_answer_target_bytes"] = answer_target()
    if worst is not None:
        summary["worst_case"] = {"stages": worst["stages"], "levels": worst["levels"]}
    write_json(out / "stage.json", summary)
    return summary


def cmd_dry_plan(context: Context, args) -> dict:
    """The worst-case plan of the run (#248), with no model call and nothing written: each stage's worst input,
    the reduce levels and the model calls. When it does not pass, repository bundles that would."""
    plan = worst_case_plan(plan_inputs(context))
    summary = {"stage": "dry-plan", **plan}
    if not plan["ok"] and plan["mode"] == "extraction-only" and len(plan["repositories"]) > 1:
        summary["split"] = split_suggestion(context)
    return summary


def cited_keys(document: dict) -> list[str]:
    """The digest keys an extract answer cites (pattern evidence and observations), in first-seen order."""
    keys = []
    for pattern in document.get("patterns", []):
        for item in pattern.get("evidence", []) if isinstance(pattern, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("pr"), str):
                keys.append(item["pr"])
    for item in document.get("observations", []):
        if isinstance(item, dict) and isinstance(item.get("pr"), str):
            keys.append(item["pr"])
    return list(dict.fromkeys(keys))


def subset_report(report: dict, names: list[str]) -> dict:
    """The quote report of some extract answers only: their files, totals and each verified quote once."""
    files = [entry for entry in report["files"] if entry["file"] in names]
    kept = {"files": files, "verified_evidence": [item for item in report["verified_evidence"] if item["file"] in names]}
    compact = compact_report({"totals": report["totals"], "files": files, "verified_evidence": kept["verified_evidence"]})
    totals = {"verified": sum(entry["quotes"]["verified"] for entry in files),
              "total": sum(entry["quotes"]["total"] for entry in files)}
    return {"totals": totals, "files": files, "verified_evidence": compact["verified_evidence"]}


def write_scope(out: Path, name: str, keys: list[str], quotes: list[dict] | None = None) -> None:
    document = {"keys": sorted(keys)}
    if quotes is not None:
        document["quotes"] = quotes
    write_json(out / (name + ".scope.json"), document)


def synthesis_group_text(context: Context, results: dict, report: dict, stems: list[str], note: str | None) -> str:
    text = template("synthesize-reduced.md")
    if note:
        text += block("범위", note)
    text += block("PR 색인", context.pr_index())
    for stem in stems:
        text += block("추출 결과 " + stem, results[stem][1])
    names = [results[stem][0] and Path(results[stem][0]).name for stem in stems]
    text += block("인용 확인 보고서", subset_report(report, names))
    return text


def group_note(number: int, total: int, group: list[str]) -> str:
    return ("그룹 %d/%d: 이 입력은 추출 결과 중 일부(%s)다. 다른 그룹은 따로 종합되고 조정자가 나중에 합친다. "
            "이 그룹의 결과만으로 후보를 만든다." % (number, total, ", ".join(group)))


def next_fit(units: list[list[str]], fits) -> list[list[str]]:
    """Pack the units in order: a unit opens a new call when it no longer fits the current one."""
    calls, current = [], []
    for unit in units:
        if current and not fits(current + unit):
            calls.append(current)
            current = []
        current = current + unit
    calls.append(current)
    return calls


def plan_synthesis_groups(context: Context, results: dict, report: dict, stems: list[str]) -> list[list[str]]:
    """One group when every answer fits one prompt; else whole lenses (all shards of a lens together, so a
    lens still sees every shard), a lens that does not fit alone split by shard, packed in order.

    There is no fixed group limit (#248): the merge above the groups is itself split when it does not fit. A lens
    packing that needs more groups than the worst-case plan counts (possible only when some lens is split) gives way
    to packing the answers one by one in the same order, which never needs more: with every answer within the stage
    output limits, any ``per_call`` answers fit one prompt (worst_case_plan)."""
    def fits(group):
        # the note's own numbers are measured at their widest (two digits)
        return len(synthesis_group_text(context, results, report, group, group_note(99, 99, group)).encode("utf-8")) \
            <= PROMPT_MAX_BYTES

    if len(synthesis_group_text(context, results, report, stems, None).encode("utf-8")) <= PROMPT_MAX_BYTES:
        return [stems]
    for stem in stems:
        if not fits([stem]):
            raise StageError("synthesis-input: the extract answer %s alone makes a prompt over %d bytes; refused, "
                             "not cut" % (stem, PROMPT_MAX_BYTES))
    units = []
    for lens in sorted({EXTRACT_STEM.fullmatch(stem).group(1) for stem in stems}):
        unit = [stem for stem in stems if EXTRACT_STEM.fullmatch(stem).group(1) == lens]
        units += [unit] if fits(unit) else [[stem] for stem in unit]
    groups = next_fit(units, fits)
    bound = synthesis_group_bound(context.pr_index(), stems)
    if len(groups) > bound:
        one_by_one = next_fit([[stem] for stem in stems], fits)
        if len(one_by_one) < len(groups):
            return one_by_one
    return groups


def reduced_synthesis_input(context: Context, args, stems: list[str], results: dict, report: dict, projected: int) -> dict:
    groups = plan_synthesis_groups(context, results, report, stems)
    out = new_directory(args.out)
    write_json(out / "check-extract.json", report)
    calls, listed = [], []
    for number, group in enumerate(groups, 1):
        name = "synthesis" if len(groups) == 1 else "synthesis-%02d" % number
        note = None if len(groups) == 1 else group_note(number, len(groups), group)
        entry = write_prompt(out / name, synthesis_group_text(context, results, report, group, note))
        entry["result_name"] = name + ".result.json"
        calls.append(entry)
        keys = list(dict.fromkeys(key for stem in group for key in cited_keys(results[stem][1])))
        write_scope(out, name, keys)
        listed.append({"input_dir": name, "stems": group, "keys": sorted(keys)})
    summary = {"stage": "synthesis", "mode": context.mode, "calls": calls, "groups": listed,
               "levels": 1 if len(groups) == 1 else 2, "merge_required": len(groups) > 1,
               "synthesis_calls": 1 if len(groups) == 1 else len(groups) + 1,
               "quotes": report["totals"], "clean_results": report["clean_results"], "final_projection_bytes": projected}
    write_json(out / "stage.json", summary)
    return summary


class QuoteSelection:
    """Verified quotes picked from a check report, each (PR, quote) listed once in the order picked. A file's
    quotes are in the order check_extraction reports them (pattern evidence, then observations)."""

    def __init__(self, report: dict) -> None:
        self.by_file, self.evidence, self.missing, self.seen = {}, [], [], set()
        for item in report["verified_evidence"]:
            self.by_file.setdefault(Path(item["file"]).name, []).append(item)

    def add(self, item: dict) -> None:
        if (item["pr"], item["quote"]) not in self.seen:
            self.seen.add((item["pr"], item["quote"]))
            self.evidence.append({"pr": item["pr"], "pr_id": item["pr_id"], "quote": item["quote"]})

    def first(self, files: list[str], candidate: dict, where: dict) -> None:
        """For each PR the candidate cites, the first verified quote of these files in this order; a PR with none
        is recorded as no quote (``where`` names the candidate), never filled from other files."""
        for key in dict.fromkeys(candidate["cited_prs"]):
            item = next((item for name in files for item in self.by_file.get(name, []) if item["pr"] == key), None)
            if item is None:
                self.missing.append({**where, "candidate": candidate["id"], "pr": key})
            else:
                self.add(item)

    def every(self, files: list[str]) -> None:
        for name in files:
            for item in self.by_file.get(name, []):
                self.add(item)


def merge_quotes(groups: list, candidates_of: dict, results: dict, report: dict) -> tuple[list, list]:
    """For each (group candidate, cited PR), the first verified quote of that candidate's own group: the group's
    extract answers in its stem order, then each answer's order. A pair with no verified quote in its group is
    listed in the second list (no quote), never filled from another group."""
    selection = QuoteSelection(report)
    for group in groups:
        files = [Path(results[stem][0]).name for stem in group["stems"]]
        for candidate in candidates_of[group["input_dir"]]:
            selection.first(files, candidate, {"group": group["input_dir"]})
    return selection.evidence, selection.missing


def final_quotes(context: Context, candidates: list, extract: dict, verify: dict, report: dict) -> tuple[list, list]:
    """For each (final candidate, cited PR), the first verified quote of the extract answers (the run's stem order,
    then each answer's order), then every verified quote the verify answers cite (verify stems in order)."""
    selection = QuoteSelection(report)
    files = [Path(extract[stem][0]).name for stem in context.extract_stems()]
    for candidate in candidates:
        selection.first(files, candidate, {})
    selection.every([Path(verify[stem][0]).name for stem in sorted(verify)])
    return selection.evidence, selection.missing


def merge_note(level: int, number: int, total: int, members: list[str]) -> str:
    return ("합침 %d층 묶음 %d/%d: 이 입력은 그룹 종합 중 일부(%s)다. 다른 묶음은 따로 합쳐지고 조정자가 다음 층에서 "
            "다시 합친다. 이 묶음의 후보만으로 합친다." % (level, number, total, ", ".join(members)))


def merge_text(context: Context, groups: list, candidates_of: dict, results: dict, report: dict,
               note: str | None) -> tuple[str, list, list]:
    """One merge prompt over some groups (in their order): the template, the PR index, each group's candidates and
    the quote report of their cited PRs. Returns (text, verified quotes listed, no-quote pairs)."""
    evidence, missing = merge_quotes(groups, candidates_of, results, report)
    text = template("synthesize-merge.md")
    if note:
        text += block("범위", note)
    text += block("PR 색인", context.pr_index())
    for group in groups:
        text += block("그룹 종합 " + group["input_dir"], {"candidates": candidates_of[group["input_dir"]]})
    text += block("인용 확인 보고서", {"totals": report["totals"], "verified_evidence": evidence, "no_quote": missing})
    return text, evidence, missing


def cmd_synthesis_merge_input(context: Context, args) -> dict:
    """The next level of a synthesis that did not fit one prompt: the group candidates (never their dropped
    ideas), the PR index, and one verified quote per (candidate, cited PR) from the candidate's own group.

    When every group fits one merge prompt this is the last level: one call named ``synthesis`` (its answer is the
    run's synthesis). Otherwise the groups are packed in order into merge calls (#248), each with a note naming its
    groups, and the stage asks for another level (merge_required): its calls are the next level's groups, each
    holding the extract answers of the groups it merged. A group too large for a merge prompt alone, or a level that
    would not merge anything (one group per call), is refused, never cut."""
    if not context.reduced:
        raise StageError("synthesis-merge-input is for the extraction-only path; this run's synthesis is one call")
    previous = read_json(Path(args.synthesis_stage) / "stage.json", PROMPT_MAX_BYTES, "synthesis stage")
    if not isinstance(previous, dict) or not previous.get("merge_required"):
        raise StageError("that synthesis stage was one call; there is nothing to merge")
    groups = previous["groups"]
    expected = [group["input_dir"] for group in groups]
    found = load_results(args.group_result, expected, "group result")
    group_keys = {group["input_dir"]: set(group["keys"]) for group in groups}
    candidates_of = {}
    for name in expected:
        candidates = parse_synthesis(context, found[name][0])
        outside = sorted({key for c in candidates for key in c["cited_prs"]} - group_keys[name])
        if outside:
            raise StageError("group result %s cites keys its own input did not hold: %s" % (name, ", ".join(outside)))
        candidates_of[name] = candidates
    results = load_results(args.extract_result, context.extract_stems(), "extract result")
    report = check_quotes(context, [path for path, _ in results.values()])
    level = previous.get("level", 1) + 1
    calls_before = previous.get("calls_made", len(previous["calls"]))

    def size(members, note):
        return len(merge_text(context, members, candidates_of, results, report, note)[0].encode("utf-8"))

    def fits(names):
        members = [group for group in groups if group["input_dir"] in names]
        return size(members, merge_note(99, 99, 99, names)) <= PROMPT_MAX_BYTES

    if size(groups, None) <= PROMPT_MAX_BYTES:
        packed = [expected]
    else:
        for name in expected:
            if not fits([name]):
                raise StageError("synthesis-merge-input: the group answer %s alone makes a merge prompt over %d bytes; "
                                 "refused, not cut" % (name, PROMPT_MAX_BYTES))
        packed = next_fit([[name] for name in expected], fits)
        if len(packed) == len(expected):
            raise StageError("synthesis-merge-input: no two of the %d group answers fit one merge prompt (%d bytes), so "
                             "another level would merge nothing; refused, not cut" % (len(expected), PROMPT_MAX_BYTES))
    out = new_directory(args.out)
    calls, listed, quotes = [], [], {"listed": 0, "no_quote": 0}
    for number, names in enumerate(packed, 1):
        members = [group for group in groups if group["input_dir"] in names]
        name = "synthesis" if len(packed) == 1 else "merge-%02d-%02d" % (level, number)
        note = None if len(packed) == 1 else merge_note(level, number, len(packed), names)
        text, evidence, missing = merge_text(context, members, candidates_of, results, report, note)
        if len(text.encode("utf-8")) > PROMPT_MAX_BYTES:
            raise StageError("synthesis-merge-input: the merge prompt is %d bytes (> %d) with these group answers; "
                             "refused, not cut" % (len(text.encode("utf-8")), PROMPT_MAX_BYTES))
        entry = write_prompt(out / name, text)
        entry["result_name"] = name + ".result.json"
        calls.append(entry)
        cited = list(dict.fromkeys(key for group in members for c in candidates_of[group["input_dir"]]
                                   for key in c["cited_prs"]))
        write_scope(out, name, cited, [{"pr": item["pr"], "quote": item["quote"]} for item in evidence])
        listed.append({"input_dir": name, "members": names,
                       "stems": [stem for group in members for stem in group["stems"]], "keys": sorted(cited)})
        quotes["listed"] += len(evidence)
        quotes["no_quote"] += len(missing)
    if len(packed) == 1:
        summary = {"stage": "synthesis-merge", "mode": context.mode, "calls": calls, "groups": len(expected),
                   "synthesis_calls": calls_before + 1, "quotes": report["totals"], "merge_quotes": quotes}
        if level > 2:
            summary["level"] = level
    else:
        summary = {"stage": "synthesis-merge", "mode": context.mode, "level": level, "calls": calls, "groups": listed,
                   "merge_required": True, "calls_made": calls_before + len(calls), "quotes": report["totals"],
                   "merge_quotes": quotes}
    write_json(out / "stage.json", summary)
    return summary


def check_extract_results(results: dict) -> None:
    """Each extract answer is its lens object within the caps and the answer size limit."""
    for stem, (_, document) in results.items():
        lens = EXTRACT_STEM.fullmatch(stem).group(1)
        if set(document) != {"lens", "patterns", "observations", "rejected"} or document["lens"] != lens:
            raise StageError("extract result %s must be the %s lens object" % (stem, lens))
        for key, cap in EXTRACT_CAPS.items():
            if not isinstance(document[key], list) or len(document[key]) > cap:
                raise StageError("extract result %s may hold at most %d %s" % (stem, cap, key))
        if EXTRACT_ANSWER_MAX_BYTES is not None and answer_bytes(document) > EXTRACT_ANSWER_MAX_BYTES:
            raise StageError("extract result %s is %d bytes as a prompt holds it (> extract_answer_max_bytes %d); "
                             "refused, not cut" % (stem, answer_bytes(document), EXTRACT_ANSWER_MAX_BYTES))


def checked_extract_answers(context: Context, paths: list[str], stage: str) -> tuple[list[str], dict, dict, int]:
    """Every extract answer of the run, each its lens object within the caps, quotes checked, and the final
    prompt's projection with those quotes within the budget."""
    stems = context.extract_stems()
    results = load_results(paths, stems, "extract result")
    check_extract_results(results)
    report = check_quotes(context, [path for path, _ in results.values()])
    projected = len(final_prompt_text(context, [], {}, compact_report(report)).encode("utf-8"))
    # An extraction-only run under the stage output limits has its final prompt bounded by the worst-case plan
    # (final_bound, which holds only the quotes final_quotes keeps); this projection with every verified quote is then
    # recorded, not a refusal (#248). Without those limits it refuses as before.
    if projected > PROMPT_MAX_BYTES and not planned(context):
        raise budget_refusal(stage, projected, "digest and the verified extract quotes alone"
                             if not context.reduced else "PR index and the verified extract quotes alone")
    return stems, results, report, projected


def cmd_synthesis_input(context: Context, args) -> dict:
    stems, results, report, projected = checked_extract_answers(context, args.extract_result, "synthesis-input")
    compact = compact_report(report)
    if context.reduced:
        return reduced_synthesis_input(context, args, stems, results, report, projected)
    text = template("synthesize.md")
    text += block("PR 목록", context.pr_list())
    for stem in stems:
        text += block("추출 결과 " + stem, results[stem][1])
    text += block("인용 확인 보고서", {"totals": report["totals"], "files": report["files"],
                                  "verified_evidence": compact["verified_evidence"]})
    if len(text.encode("utf-8")) > PROMPT_MAX_BYTES:
        raise StageError("synthesis prompt is %d bytes (> %d) with these extract answers; refused, not cut"
                         % (len(text.encode("utf-8")), PROMPT_MAX_BYTES))
    out = new_directory(args.out)
    write_json(out / "check-extract.json", report)
    entry = write_prompt(out / "synthesis", text)
    entry["result_name"] = "synthesis.result.json"
    summary = {"stage": "synthesis", "calls": [entry], "quotes": report["totals"],
               "clean_results": report["clean_results"], "final_projection_bytes": projected}
    write_json(out / "stage.json", summary)
    return summary


def recorded_call_problems(kind: str, path: str, call_dir: Path, prompt_sha: str | None) -> tuple:
    """The strict verdict of the call that made an adopted answer (ok, strict limits, this answer kind, P3 the
    prompt sha, P4 PASS, adopted under this file name) and its picked-up worker manifest (that prompt sha, and the
    answer's bytes as the report). prompt_sha None takes the verdict's own P3. Returns (problems, answer sha,
    prompt sha)."""
    answer = read_bounded(Path(path), ANSWER_MAX_BYTES, kind + " result")
    verdict = read_json(call_dir / "verdict.json", ANSWER_MAX_BYTES, "verdict of " + call_dir.name)
    manifest = read_json(call_dir / "result" / "manifest.json", ANSWER_MAX_BYTES, "manifest of " + call_dir.name)
    if not isinstance(verdict, dict) or not isinstance(manifest, dict) or not isinstance(verdict.get("items"), list):
        raise StageError("the record of %s is not a verdict and a worker manifest" % call_dir.name)
    items = {item.get("id"): item for item in verdict["items"] if isinstance(item, dict)}
    problems = []
    if verdict.get("ok") is not True or verdict.get("limits") != "strict" or verdict.get("answer_kind") != kind:
        problems.append("the verdict is not an ok strict %s verdict" % kind)
    checked = items.get("P3-prompt-sha256", {}).get("detail")
    if prompt_sha is None:
        prompt_sha = checked if isinstance(checked, str) and re.fullmatch(r"[0-9a-f]{64}", checked) else None
        if prompt_sha is None:
            problems.append("the verdict records no prompt sha")
    elif checked != prompt_sha:
        problems.append("the verdict checked another prompt")
    if items.get("P4-prompt-binding", {}).get("status") != "PASS":
        problems.append("the verdict did not pass the prompt binding")
    adopted = verdict.get("adopted_to")
    if not isinstance(adopted, str) or Path(adopted).name != Path(path).name:
        problems.append("the verdict adopted another file (%r)" % (adopted,))
    if manifest.get("prompt_sha256") != prompt_sha:
        problems.append("the worker manifest records another prompt")
    answer_sha = hashlib.sha256(answer).hexdigest()
    if manifest.get("report_sha256") != answer_sha:
        problems.append("the answer bytes are not the report the worker manifest records")
    return problems, answer_sha, prompt_sha


def attempts(records: Path, call: str) -> list[str]:
    """The call directories of a call and of its retries (<call>-r2, -r3, ..., #280: call_with_retry.py), in attempt
    order; a retry made by hand under the same name (the split gate run's extract-structure-01-r2) is one too."""
    names, number = [call], 2
    while (records / ("%s-r%d" % (call, number))).is_dir():
        names.append("%s-r%d" % (call, number))
        number += 1
    return names


def adopted_extract_answer(context: Context, stem: str, path: str, prompt_sha: str, records: Path) -> dict:
    """One adopted extract answer, bound to the prompt its extract stage recorded (recorded_call_problems) by the
    attempt of its call that adopted it (#280: the first one, or a retry)."""
    refusals = []
    for call in attempts(records, stem):
        if (records / call).is_symlink():
            refusals.append("%s: a link" % call)
            continue
        problems, answer_sha, _ = recorded_call_problems("extract", path, records / call, prompt_sha)
        if not problems:
            answer = {"stem": stem, "result_name": Path(path).name, "answer_sha256": answer_sha,
                      "prompt_sha256": prompt_sha}
            return dict(answer, call=call) if call != stem else answer
        refusals.append("%s%s" % ("%s: " % call if call != stem else "", "; ".join(problems)))
    raise StageError("the adopted answer %s is not bound to its recorded prompt: %s" % (stem, " / ".join(refusals)))


def ledger_lines(records: Path, label: str) -> list:
    """The ledger.jsonl of a ledger directory, one JSON object per line (anything else is refused)."""
    ledger = []
    for line in read_bounded(records / "ledger.jsonl", ANSWER_MAX_BYTES, label).decode("utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise StageError("the %s is not one JSON object per line: %s" % (label, error)) from error
        ledger.append(entry if isinstance(entry, dict) else {})
    return ledger


def chain_ledgers(records: Path, bound: dict, ancestors: list) -> list:
    """The parent ledger and its ancestor ledgers, nearest first (#258): each ancestor must be the ledger the one
    before it recorded as its parent in ledger-limit.json (analysis_call.sh: the parent ledger.jsonl sha256, and
    for a chain the parent ledger-limit.json sha256 too). Returns [(directory, ledger lines)]."""
    ledgers, child, child_bound = [(records, ledger_lines(records, "parent ledger"))], records, bound
    for directory in map(Path, ancestors):
        recorded = child_bound.get("parent") if isinstance(child_bound, dict) else None
        if directory.is_symlink() or not directory.is_dir() or not isinstance(recorded, dict):
            raise StageError("%s is not the parent ledger %s records" % (directory, child))
        data = read_bounded(directory / "ledger.jsonl", ANSWER_MAX_BYTES, "ancestor ledger")
        limit = read_bounded(directory / "ledger-limit.json", ANSWER_MAX_BYTES, "ancestor ledger limit")
        if (hashlib.sha256(data).hexdigest() != recorded.get("ledger_sha256")
                or "limit_sha256" in recorded and hashlib.sha256(limit).hexdigest() != recorded["limit_sha256"]):
            raise StageError("%s is not the parent ledger %s records (another ledger or ledger limit sha)"
                             % (directory, child))
        child, child_bound = directory, read_json(directory / "ledger-limit.json", ANSWER_MAX_BYTES, "ancestor ledger limit")
        ledgers.append((directory, ledger_lines(directory, "ancestor ledger")))
    return ledgers


def reduce_answer_key(answer: dict) -> tuple:
    return tuple(answer.get(key) for key in ("kind", "call", "result_name", "answer_sha256", "prompt_sha256"))


def adopted_reduce_answer(kind: str, path: str, ledgers: list, recorded: list) -> dict:
    """One adopted synthesis or verify answer of the parent ledger (#82) or of an ancestor ledger (#258): its call
    record (recorded_call_problems) and the ledger line of that call, the one analysis_call.sh appended (a token
    call, succeeded, the same input, prompt and report sha). The call directory is the answer's stem as the harness
    named it (lower case). The nearest ledger (chain_ledgers) that binds it wins; one bound to an ancestor ledger
    must also be an answer the continue stage adopted (``recorded``), and is marked with its ancestor depth."""
    name = Path(path).name
    if not name.endswith(".result.json"):
        raise StageError("%s is not a <stem>.result.json answer" % name)
    stem = name[: -len(".result.json")]
    refusals = []
    for depth, (records, ledger) in enumerate(ledgers):
        entries = set(os.listdir(records))
        first = stem if stem in entries else stem.lower() if stem.lower() in entries else None
        if first is None or (records / first).is_symlink() or not (records / first).is_dir():
            continue
        where = "the parent ledger" if depth == 0 else "the ancestor ledger %s" % records
        # the attempt that adopted it: the call itself, or a retry of it (#280)
        for call in attempts(records, first):
            if (records / call).is_symlink():
                continue
            problems, answer_sha, prompt_sha = recorded_call_problems(kind, path, records / call, None)
            lines = [line for line in ledger if line.get("name") == call and line.get("kind") == "token"]
            if len(lines) != 1:
                problems.append("%s holds %d token lines for the call %s" % (where, len(lines), call))
            elif (lines[0].get("status") != "succeeded" or lines[0].get("input_sha256") != prompt_sha
                  or lines[0].get("prompt_sha256_recorded") != prompt_sha or lines[0].get("report_sha256") != answer_sha):
                problems.append("%s line of %s records another input, prompt or report" % (where, call))
            answer = {"kind": kind, "call": call, "result_name": name, "answer_sha256": answer_sha,
                      "prompt_sha256": prompt_sha}
            if depth and not problems and reduce_answer_key(answer) not in map(reduce_answer_key, recorded):
                problems.append("bound to %s but not adopted by the continue stage" % where)
            if not problems:
                return dict(answer, ancestor=depth) if depth else answer
            refusals.append("not bound to %s%s: %s" % (where, " (%s)" % call if call != first else "",
                                                        "; ".join(problems)))
    if not refusals:
        raise StageError("no call record for the adopted answer %s" % name)
    raise StageError("the adopted answer %s is %s" % (name, " / ".join(refusals)))


def continue_chain_plan(context: Context, args, data: bytes, recorded: dict) -> dict:
    """continue-plan on a continue stage (#82): the parent ledger was itself a continuation (bound to this continue
    stage) and has adopted the synthesis answer and some or all verify answers. The extract answers must be the
    ones the continue stage recorded (same bytes); each reduce answer must be bound to the parent ledger
    (adopted_reduce_answer), or, for an answer the continue stage itself adopted from further up, to an ancestor
    ledger given with --ancestor-records and linked through the recorded parent shas (chain_ledgers, #258): so the
    latest ledger of a straight chain is the one continued. The new plan holds the verify calls of candidates
    without an adopted answer and the final, bound to this continue stage with parent_stage "continue" so the
    harness counts the ancestors too."""
    if not args.synthesis_result:
        raise StageError("a continue stage needs its adopted synthesis answer (--synthesis-result)")
    before, listed = recorded.get("call_plan"), recorded.get("answers")
    pending = recorded.get("pending", [])
    done = before.get("continues", {}).get("completed", {}) if isinstance(before, dict) else {}
    if (not isinstance(before, dict) or not isinstance(listed, list) or recorded.get("mode") != context.mode
            or before.get("shards") != context.shards or not isinstance(done.get("extract"), int)
            or not isinstance(before.get("synthesis"), int) or not isinstance(pending, list)
            or not all(isinstance(item, dict) and isinstance(item.get("stem"), str) for item in listed + pending)):
        raise StageError("the continue stage.json is not a continue plan of this run")
    stems, results, report, projected = checked_extract_answers(context, args.extract_result, "continue-plan")
    expected = {item["stem"]: item for item in listed}
    left = {item["stem"]: item for item in pending}
    if sorted(list(expected) + list(left)) != sorted(stems):
        raise StageError("the extract answers are not the ones the continue stage recorded%s"
                         % (" and left (%d extract calls left)" % len(left) if left else ""))
    records = Path(args.records)
    answers = list(listed)
    for stem in stems:
        path = results[stem][0]
        if stem in left:
            # an extract call the continue stage left (#280): made under the parent ledger, bound to its prompt there
            if not isinstance(left[stem].get("prompt_sha256"), str):
                raise StageError("the continue stage records no prompt for the extract calls left")
            answers.append(adopted_extract_answer(context, stem, path, left[stem]["prompt_sha256"], records))
        elif (Path(path).name != expected[stem].get("result_name")
                or hashlib.sha256(read_bounded(Path(path), ANSWER_MAX_BYTES, "extract result")).hexdigest()
                != expected[stem].get("answer_sha256")):
            raise StageError("the extract answer %s is not the one the continue stage recorded" % stem)
    bound = read_json(records / "ledger-limit.json", ANSWER_MAX_BYTES, "parent ledger limit")
    if not isinstance(bound, dict) or bound.get("call_plan_sha256") != hashlib.sha256(data).hexdigest():
        raise StageError("the records are not the ledger bound to this continue stage")
    ledgers = chain_ledgers(records, bound, args.ancestor_records)
    adopted = recorded.get("reduce_answers", [])
    if not isinstance(adopted, list) or not all(isinstance(answer, dict) for answer in adopted):
        raise StageError("the continue stage.json records no list of reduce answers")
    candidates = parse_synthesis(context, args.synthesis_result)
    reduce_answers = [adopted_reduce_answer("synthesis", args.synthesis_result, ledgers, adopted)]
    ids, verified = [candidate["id"] for candidate in candidates], []
    for path in args.verify_result:
        document = read_json(Path(path), ANSWER_MAX_BYTES, "verify result")
        answer_id = document.get("id") if isinstance(document, dict) else None
        if answer_id not in ids or answer_id in verified or Path(path).name != "verify-%s.result.json" % answer_id:
            raise StageError("%s is not the verify answer of one synthesis candidate" % Path(path).name)
        verified.append(answer_id)
        reduce_answers.append(adopted_reduce_answer("verify", path, ledgers, adopted))
    verify = len(ids) - len(verified)
    share = retry_policy.retry_share({"extract": 0, "synthesis": 0, "verify": verify, "final": 1})
    total = verify + 1 + share["total"]
    plan = {"mode": context.mode,
            "continues": {"extract_stage_sha256": hashlib.sha256(data).hexdigest(),
                          "completed": {"extract": done["extract"] + len(left),
                                        "synthesis": done.get("synthesis", 0) + before["synthesis"],
                                        "verify": len(verified)},
                          "parent_stage": "continue"},
            "shards": context.shards, "lenses": len(context.siblings.build_prompts.LENSES), "extract": 0,
            "synthesis": 0, "verify": verify, "final": 1, "retry": share, "max_model_calls": total,
            "formula": "%d extract and %d synthesis calls done, %d of %d verify answers adopted; %d (verify) + 1 "
                       "(final) + %d (retry share) = %d"
                       % (done["extract"] + len(left), done.get("synthesis", 0) + before["synthesis"], len(verified),
                          len(ids), verify, share["total"], total),
            "inputs": {"max_candidates": MAX_CANDIDATES, "candidates": len(ids)}}
    out = new_directory(args.out)
    summary = {"stage": "continue", "mode": context.mode, "answers": sorted(answers, key=lambda item: item["stem"]),
               "reduce_answers": reduce_answers,
               "max_model_calls": total, "call_plan": plan, "final_projection_bytes": projected}
    write_json(out / "stage.json", summary)
    return summary


def partial_continue_plan(context: Context, args, data: bytes, prompts: dict, given: list[str]) -> dict:
    """continue-plan of a run stopped during its extract calls (#280: the split gate run adopted 10 of its 15): the
    adopted answers are kept (each bound to its recorded prompt by the attempt that adopted it), the other extract
    prompts are left to call as the extract stage recorded them (``pending``: stem and prompt sha), and every later
    stage is planned at the stage output limits (the worst-case plan's levels, since the groups are not known before
    every answer is), with the retry share of these calls."""
    results = load_results(args.extract_result, given, "extract result")
    check_extract_results(results)
    try:
        answers = [adopted_extract_answer(context, stem, results[stem][0], prompts[stem], Path(args.records))
                   for stem in given]
        if context.reduced:
            worst = worst_case_plan(plan_inputs(context))
            failed = ["%s: %s bytes" % (stage["stage"], stage["worst_bytes"]) for stage in worst["stages"]
                      if not stage["ok"]]
            if failed:
                raise StageError("the later stages do not pass the worst-case plan: %s" % "; ".join(failed))
            levels = worst["synthesis_levels"]
        else:
            levels = [1]
    except StageError as error:
        raise StageError("continue-plan: %s" % error) from error
    left = sorted(set(prompts) - set(given))
    calls = {"extract": len(left), "synthesis": sum(levels), "verify": MAX_CANDIDATES, "final": 1}
    share = retry_policy.retry_share(calls)
    total = sum(calls.values()) + share["total"]
    if total > ABSOLUTE_MAX_CALLS:
        raise StageError("continue-plan: %d model calls remain at the limits (%d planned + %d retry share), over the "
                         "absolute cap %d" % (total, total - share["total"], share["total"], ABSOLUTE_MAX_CALLS))
    plan = {"mode": context.mode,
            "continues": {"extract_stage_sha256": hashlib.sha256(data).hexdigest(), "completed": {"extract": len(given)}},
            "shards": context.shards, "lenses": len(context.siblings.build_prompts.LENSES), **calls,
            "synthesis_levels": levels, "retry": share, "max_model_calls": total,
            "formula": "%d of %d extract calls done, %d of %d extract calls left; %d (synthesis levels %s at the limits) "
                       "+ %d (verify, at most synthesis_max_candidates) + 1 (final) + %d (retry share) = %d"
                       % (len(given), len(prompts), len(left), len(prompts), sum(levels), " + ".join(map(str, levels)),
                          MAX_CANDIDATES, share["total"], total),
            "inputs": {"max_candidates": MAX_CANDIDATES, "limits": output_limits()}}
    out = new_directory(args.out)
    summary = {"stage": "continue", "mode": context.mode, "answers": answers,
               "pending": [{"stem": stem, "result_name": stem + ".result.json", "prompt_sha256": prompts[stem]}
                           for stem in left],
               "max_model_calls": total, "call_plan": plan}
    write_json(out / "stage.json", summary)
    return summary


def cmd_continue_plan(context: Context, args) -> dict:
    """The stages not yet run of a run whose extract answers were all adopted (#82): a new call plan with
    extract 0, bound to the extract stage.json that made the prompts (and that the parent ledger is bound to)."""
    try:
        data = read_bounded(Path(args.extract_stage) / "stage.json", PROMPT_MAX_BYTES, "extract stage")
        try:
            recorded = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_refuse_constant)
        except (UnicodeDecodeError, ValueError) as error:
            raise StageError("the extract stage.json is not one strict JSON document: %s" % error) from error
        if isinstance(recorded, dict) and recorded.get("stage") == "continue":
            return continue_chain_plan(context, args, data, recorded)
        if not isinstance(recorded, dict) or recorded.get("stage") != "extract":
            raise StageError("that stage.json is not an extract stage (nor a continue stage)")
        if args.synthesis_result or args.verify_result or args.ancestor_records:
            raise StageError("adopted synthesis and verify answers (and ancestor records) are taken only with a "
                             "continue stage")
        calls = recorded.get("calls")
        if not isinstance(calls, list) or not all(isinstance(call, dict) and isinstance(call.get("input_dir"), str)
                                                  and isinstance(call.get("sha256"), str) for call in calls):
            raise StageError("the extract stage.json holds no calls")
        prompts = {call["input_dir"]: call["sha256"] for call in calls}
        if sorted(prompts) != context.extract_stems() or len(prompts) != len(calls):
            raise StageError("the extract stage.json calls are not this run's extract prompts")
        # written again with the answer size target the stage recorded (#280; none before it), not the one in force
        target = recorded.get("extract_answer_target_bytes")
        if target is not None and (type(target) is not int or target < 1):
            raise StageError("the extract stage.json records an answer size target that is not a positive integer")
        written = {stem: hashlib.sha256(text.encode("utf-8")).hexdigest()
                   for stem, text in context.extract_prompt_texts(target).items()}
        changed = [stem for stem in sorted(prompts) if prompts[stem] != written[stem]]
        if changed:
            raise StageError("the extract stage.json records other prompts than this run's analyzer writes: %s"
                             % ", ".join(changed))
    except StageError as error:
        raise StageError("continue-plan: %s" % error) from error
    given = [Path(path).name[: -len(".result.json")] for path in args.extract_result]
    if (all(Path(path).name.endswith(".result.json") for path in args.extract_result)
            and len(set(given)) == len(given) and set(given) < set(prompts)):
        return partial_continue_plan(context, args, data, prompts, sorted(given))
    stems, results, report, projected = checked_extract_answers(context, args.extract_result, "continue-plan")
    try:
        answers = [adopted_extract_answer(context, stem, results[stem][0], prompts[stem], Path(args.records))
                   for stem in stems]
        groups = len(plan_synthesis_groups(context, results, report, stems)) if context.reduced else 1
        # the merge levels above the real groups, at the stage output limits (#248)
        merges, refusals = synthesis_levels(plan_inputs(context), groups) if groups > 1 else ([], [])
        if refusals:
            raise StageError("the merge of these %d groups does not pass the worst-case plan: %s"
                             % (groups, "; ".join(refusals)))
    except StageError as error:
        raise StageError("continue-plan: %s" % error) from error
    levels = [groups] + [level["calls"] for level in merges]
    synthesis = sum(levels)
    share = retry_policy.retry_share({"extract": 0, "synthesis": synthesis, "verify": MAX_CANDIDATES, "final": 1})
    total = synthesis + MAX_CANDIDATES + 1 + share["total"]
    lenses = len(context.siblings.build_prompts.LENSES)
    plan = {"mode": context.mode,
            "continues": {"extract_stage_sha256": hashlib.sha256(data).hexdigest(), "completed": {"extract": len(stems)}},
            "shards": context.shards, "lenses": lenses, "extract": 0, "synthesis": synthesis,
            "synthesis_groups": groups, "synthesis_levels": levels, "verify": MAX_CANDIDATES, "final": 1,
            "retry": share, "max_model_calls": total,
            "formula": "%d extract calls done; %s + %d (verify, at most synthesis_max_candidates) + 1 (final) + %d "
                       "(retry share) = %d"
                       % (len(stems), "1 (synthesis)" if groups == 1 else
                          "%d (synthesis: %d group calls, then merge levels %s at the limits)"
                          % (synthesis, groups, " + ".join(map(str, levels[1:]))), MAX_CANDIDATES, share["total"],
                          total),
            "inputs": {"max_candidates": MAX_CANDIDATES, "limits": output_limits()}}
    if total > ABSOLUTE_MAX_CALLS:
        raise StageError("continue-plan: %d model calls remain at the limits, over the absolute cap %d"
                         % (total, ABSOLUTE_MAX_CALLS))
    out = new_directory(args.out)
    summary = {"stage": "continue", "mode": context.mode, "answers": answers, "max_model_calls": total,
               "call_plan": plan, "final_projection_bytes": projected}
    write_json(out / "stage.json", summary)
    return summary


def verify_prompt(context: Context, candidate: dict, keymap: dict | None = None, digest_dir: Path | None = None) -> str:
    """The analyzer's verify prompt (build_prompts verify) for one candidate, with the digest sections of the
    PRs it cites and only a title list of the others. A digest of one shard gets an empty second shard so the
    analyzer's own multi-shard form is used: the cited sections, never the whole digest. ``keymap`` and
    ``digest_dir`` replace the run's digest (the worst-case plan)."""
    digest_dir = context.digest_dir if digest_dir is None else digest_dir
    with tempfile.TemporaryDirectory(prefix="stage-verify-") as tmp:
        directory = Path(tmp) / "digest"
        directory.mkdir()
        keymap = deepcopy(context.keymap if keymap is None else keymap)
        for name in keymap["shards"]:
            (directory / name).write_bytes(read_bounded(digest_dir / name, PROMPT_MAX_BYTES * 4, "digest shard"))
        if len(keymap["shards"]) == 1:
            keymap["shards"].append("digest-none.md")
            (directory / "digest-none.md").write_text("", encoding="utf-8")
        (directory / "keymap.json").write_text(json.dumps(keymap), encoding="utf-8")
        (Path(tmp) / "candidates.json").write_text(json.dumps([candidate], ensure_ascii=False), encoding="utf-8")
        code, _, stderr = context.wf.run_main(context.siblings.build_prompts.main, [
            "verify", "--digest-dir", str(directory), "--candidates", str(Path(tmp) / "candidates.json"),
            "--out", str(Path(tmp) / "build")])
        if code != 0:
            raise StageError("build_prompts verify failed: " + stderr.strip())
        return (Path(tmp) / "build" / "prompts" / ("verify-" + candidate["id"] + ".md")).read_text(encoding="utf-8")


def fit_verify_prompt(context: Context, candidate: dict) -> tuple[str, list[str]]:
    """The verify prompt with as many of the candidate's cited PR sections as fit one call, the first n in the
    candidate's order (at least two); the cited list the verifier sees is exactly those n."""
    cited = list(dict.fromkeys(candidate["cited_prs"]))
    for count in range(len(cited), 1, -1):
        text = verify_prompt(context, dict(candidate, cited_prs=cited[:count]))
        if len(text.encode("utf-8")) <= PROMPT_MAX_BYTES:
            return text, cited[:count]
    raise StageError("verify-inputs: candidate %s does not fit one prompt even with the sections of its first two cited "
                     "PRs; refused, not cut" % candidate["id"])


def reduced_verify_inputs(context: Context, args, candidates: list, budget: dict) -> dict:
    prompts = []
    for candidate in candidates:
        text, included = fit_verify_prompt(context, candidate)
        prompts.append((candidate, text, included))
    out = new_directory(args.out)
    write_json(out / "candidates.json", candidates)
    written = []
    for candidate, text, included in prompts:
        stem = "verify-" + candidate["id"]
        entry = write_prompt(out / stem, text)
        total = len(set(candidate["cited_prs"]))
        entry.update(result_name=stem + ".result.json", cited_total=total, cited_included=len(included),
                     trimmed=len(included) < total)
        write_scope(out, stem, included)
        written.append(entry)
    summary = {"stage": "verify", "mode": context.mode, "candidates": len(candidates), "calls": written,
               "final_budget": budget,
               "note": "no candidate: no verify call; the final call may still accept no pattern" if not candidates else ""}
    write_json(out / "stage.json", summary)
    return summary


def cmd_verify_inputs(context: Context, args) -> dict:
    candidates = parse_synthesis(context, args.synthesis)
    extract = load_results(args.extract_result, context.extract_stems(), "extract result")
    report = check_quotes(context, [path for path, _ in extract.values()])
    if planned(context):
        # the quotes the final input will hold for these candidates (final_quotes), not every verified quote (#248)
        kept, missing = final_quotes(context, candidates, extract, {}, report)
        quotes = {"totals": report["totals"], "verified_evidence": kept, "no_quote": missing}
    else:
        quotes = compact_report(report)
    base = len(final_prompt_text(context, candidates, {}, quotes).encode("utf-8"))
    k = len(candidates)
    floor, bound = base + k * VERIFY_FLOOR_BYTES, base + k * 2 * VERIFY_ANSWER_MAX_BYTES
    if floor > PROMPT_MAX_BYTES:
        raise budget_refusal("verify-inputs", floor, "%d candidates with at least %d bytes per verify answer"
                             % (k, VERIFY_FLOOR_BYTES))
    budget = {"base_bytes": base, "floor_bytes": floor, "bound_bytes": bound, "cap": PROMPT_MAX_BYTES,
              "guaranteed": bound <= PROMPT_MAX_BYTES,
              "note": "bound assumes each verify answer at most verify_answer_max_bytes, counted twice (block and "
                      "quotes); when not guaranteed, final-input may still refuse after the verify calls"}
    if context.reduced:
        return reduced_verify_inputs(context, args, candidates, budget)
    out = new_directory(args.out)
    write_json(out / "candidates.json", candidates)
    written = []
    if candidates:
        code, _, stderr = context.wf.run_main(context.siblings.build_prompts.main, [
            "verify", "--digest-dir", str(context.digest_dir), "--candidates", str(out / "candidates.json"),
            "--out", str(out / "build")])
        if code != 0:
            raise StageError("build_prompts verify failed: " + stderr.strip())
        for candidate in candidates:
            stem = "verify-" + candidate["id"]
            text = read_bounded(out / "build" / "prompts" / (stem + ".md"), PROMPT_MAX_BYTES, "verify prompt").decode("utf-8")
            entry = write_prompt(out / stem, text)
            entry["result_name"] = stem + ".result.json"
            written.append(entry)
    summary = {"stage": "verify", "candidates": len(candidates), "calls": written, "final_budget": budget,
               "note": "no candidate: no verify call; the final call may still accept no pattern" if not candidates else ""}
    write_json(out / "stage.json", summary)
    return summary


def cmd_final_input(context: Context, args) -> dict:
    candidates = parse_synthesis(context, args.synthesis)
    stems = context.extract_stems()
    extract = load_results(args.extract_result, stems, "extract result")
    for path in args.verify_result:
        size = os.lstat(path).st_size if os.path.lexists(path) else 0
        if size > VERIFY_ANSWER_MAX_BYTES:
            raise StageError("verify answer %s is %d bytes (> verify_answer_max_bytes %d); refused, not cut"
                             % (Path(path).name, size, VERIFY_ANSWER_MAX_BYTES))
    verify = load_results(args.verify_result, ["verify-" + c["id"] for c in candidates], "verify result")
    for stem, (_, document) in verify.items():
        if document.get("id") != stem[len("verify-"):] or not isinstance(document.get("evidence_checks"), list):
            raise StageError("verify result %s must answer its own candidate id with evidence_checks" % stem)
    report = check_quotes(context, [path for path, _ in extract.values()] + [path for path, _ in verify.values()])
    if context.reduced:
        # the quotes the candidates and the verify answers point at, never the whole report (#82)
        kept, missing = final_quotes(context, candidates, extract, verify, report)
        quote_report = {"totals": report["totals"], "verified_evidence": kept, "no_quote": missing}
    else:
        quote_report = compact_report(report)
    text = final_prompt_text(context, candidates, {stem: doc for stem, (_, doc) in verify.items()}, quote_report)
    if len(text.encode("utf-8")) > PROMPT_MAX_BYTES:
        raise StageError("final-input: the final prompt is %d bytes (> %d) with these verify answers; a dynamic "
                         "overflow the earlier projections could not rule out; refused, not cut"
                         % (len(text.encode("utf-8")), PROMPT_MAX_BYTES))
    out = new_directory(args.out)
    write_json(out / "check-all.json", report)
    entry = write_prompt(out / "final", text)
    entry["result_name"] = "final.result.json"
    summary = {"stage": "final", "calls": [entry], "candidates": len(candidates), "quotes": report["totals"],
               "clean_results": report["clean_results"]}
    if context.reduced:
        write_scope(out, "final", list(context.keymap["sections"]),
                    [{"pr": item["pr"], "quote": item["quote"]} for item in kept])
        summary["final_quotes"] = {"listed": len(kept), "no_quote": len(missing)}
    write_json(out / "stage.json", summary)
    return summary


def _strings(value, label: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value) \
            or (nonempty and not value):
        raise StageError(label + " must be an array of nonempty strings")


def _claim(value, label: str, url: str) -> None:
    if not isinstance(value, dict) or set(value) != CLAIM_KEYS:
        raise StageError(label + " must hold exactly value, basis, evidence_links")
    if value["basis"] not in BASIS:
        raise StageError(label + ".basis must be one of " + ", ".join(BASIS))
    _strings(value["evidence_links"], label + ".evidence_links")
    if any(link != url for link in value["evidence_links"]):
        raise StageError(label + ".evidence_links may name only the PR's own URL")
    if value["basis"] == "unknown":
        if value["value"] is not None or value["evidence_links"]:
            raise StageError(label + " with basis unknown must have value null and no links")
    elif not isinstance(value["value"], str) or not value["value"].strip():
        raise StageError(label + ".value must be a nonempty string unless basis is unknown")


def _confidence(value, label: str) -> None:
    if not isinstance(value, dict) or set(value) != CONFIDENCE_KEYS or value["level"] not in LEVELS:
        raise StageError(label + " must be {level: high|medium|low, evidence, limitations}")
    _strings(value["evidence"], label + ".evidence")
    _strings(value["limitations"], label + ".limitations")


def check_final_answer(context: Context, answer) -> None:
    prefix = context.siblings.validate_corpus.EXTRACTION_LEDGER_PREFIX
    if not isinstance(answer, dict) or set(answer) != {"records", "patterns", "extraction_ledger", "limitations"}:
        raise StageError("final answer must hold exactly records, patterns, extraction_ledger, limitations")
    urls = {record["pr_id"]: record["pull_request"]["url"] for record in context.records}
    records, patterns = answer["records"], answer["patterns"]
    if not isinstance(records, dict) or set(records) != set(urls):
        raise StageError("final answer records must name every corpus PR-* id exactly once: %s" % sorted(urls))
    if not isinstance(patterns, list):
        raise StageError("final answer patterns must be an array")
    if len(patterns) > MAX_FINAL_PATTERNS:
        raise StageError("final answer may hold at most %d patterns (got %d)" % (MAX_FINAL_PATTERNS, len(patterns)))
    pattern_ids = []
    for index, pattern in enumerate(patterns):
        label = "patterns[%d]" % index
        if not isinstance(pattern, dict):
            raise StageError(label + " must be an object")
        if set(pattern) & DETERMINISTIC_PATTERN_KEYS:
            raise StageError(label + " carries deterministic fields %s" % sorted(set(pattern) & DETERMINISTIC_PATTERN_KEYS))
        if set(pattern) != MODEL_PATTERN_KEYS:
            raise StageError(label + " must hold exactly %s" % sorted(MODEL_PATTERN_KEYS))
        if not isinstance(pattern["pattern_id"], str) or not PATTERN_ID.fullmatch(pattern["pattern_id"]) \
                or pattern["pattern_id"] in pattern_ids:
            raise StageError(label + ".pattern_id must be a unique PAT-NNN")
        pattern_ids.append(pattern["pattern_id"])
        if not isinstance(pattern["description"], str) or not pattern["description"].strip():
            raise StageError(label + ".description must be a nonempty string")
        _strings(pattern["evidence_pr_ids"], label + ".evidence_pr_ids", nonempty=True)
        if set(pattern["evidence_pr_ids"]) - set(urls) or len(set(pattern["evidence_pr_ids"])) != len(pattern["evidence_pr_ids"]):
            raise StageError(label + ".evidence_pr_ids must be distinct corpus PR-* ids")
        for key in ("applicability", "counterconditions", "search_clues", "expected_tests", "maintainer_judgment_required"):
            _strings(pattern[key], label + "." + key)
        if pattern["provenance_mode"] != PROVENANCE:
            raise StageError(label + ".provenance_mode must be " + PROVENANCE)
        _confidence(pattern["confidence"], label + ".confidence")
    for pr_id, value in records.items():
        label = "records[%s]" % pr_id
        if not isinstance(value, dict):
            raise StageError(label + " must be an object")
        if set(value) & DETERMINISTIC_RECORD_KEYS:
            raise StageError(label + " carries deterministic fields %s" % sorted(set(value) & DETERMINISTIC_RECORD_KEYS))
        if set(value) != MODEL_RECORD_KEYS:
            raise StageError(label + " must hold exactly %s" % sorted(MODEL_RECORD_KEYS))
        for key in ("change_summary", "motivation", "review_judgment", "closure_reason"):
            _claim(value[key], label + "." + key, urls[pr_id])
        if not isinstance(value["test_evidence"], list):
            raise StageError(label + ".test_evidence must be an array")
        for position, claim in enumerate(value["test_evidence"]):
            _claim(claim, "%s.test_evidence[%d]" % (label, position), urls[pr_id])
        _strings(value["evidence_links"], label + ".evidence_links")
        if any(link != urls[pr_id] for link in value["evidence_links"]):
            raise StageError(label + ".evidence_links may name only the PR's own URL")
        _strings(value["pattern_ids"], label + ".pattern_ids")
        expected = sorted(p["pattern_id"] for p in patterns if pr_id in p["evidence_pr_ids"])
        if sorted(value["pattern_ids"]) != expected:
            raise StageError(label + ".pattern_ids must list exactly the patterns it is evidence for: %s" % expected)
        if value["provenance_mode"] != PROVENANCE:
            raise StageError(label + ".provenance_mode must be " + PROVENANCE)
        _confidence(value["confidence"], label + ".confidence")
    ledger = answer["extraction_ledger"]
    if not isinstance(ledger, str) or not ledger.strip() or "\n" in ledger or ledger.startswith(prefix.strip()):
        raise StageError("extraction_ledger must be one nonempty line without the %r prefix" % prefix)
    _strings(answer["limitations"], "limitations")
    if any(item.startswith(prefix.strip()) for item in answer["limitations"]):
        raise StageError("limitations must not carry a second extraction ledger")


def build_envelope(context: Context, answer, generated_at: str) -> dict:
    validate = context.siblings.validate_corpus
    revision = validate._canonical_revision()
    by_pr_id = {record["pr_id"]: record for record in context.records}
    records = []
    for record in context.records:
        snapshot = record.get("evidence_snapshot") if isinstance(record.get("evidence_snapshot"), dict) else {}
        license_id = record.get("license", {}).get("spdx_id") if isinstance(record.get("license"), dict) else None
        projection = deepcopy(answer["records"][record["pr_id"]])
        projection.update({
            "files_changed": [item["path"] for item in snapshot.get("changed_files", [])
                              if isinstance(item, dict) and isinstance(item.get("path"), str)],
            "evidence_manifest": {"completeness": deepcopy(snapshot.get("completeness", {})),
                                  "partial_categories": deepcopy(snapshot.get("partial_categories", []))},
            "license_spdx": license_id if isinstance(license_id, str) and license_id else "NOASSERTION",
            "superseded_by": None,
        })
        enriched = deepcopy(record)
        enriched["analysis"] = projection
        enriched["analysis_history"] = deepcopy(record.get("analysis_history") or []) + [{
            "revision": revision, "generated_at": generated_at,
            "evidence_manifest": deepcopy(projection["evidence_manifest"]), "conclusion": deepcopy(projection)}]
        records.append(enriched)
    patterns = []
    for item in answer["patterns"]:
        pattern = deepcopy(item)
        pattern.update({
            "generated_by": {"name": validate.ANALYZER_NAME, "revision": revision},
            "source_licenses": [{"pr_id": pr_id, "spdx_id": records[[r["pr_id"] for r in records].index(pr_id)]
                                 ["analysis"]["license_spdx"]} for pr_id in item["evidence_pr_ids"]
                                if pr_id in by_pr_id],
            "superseded_by": None,
        })
        pattern["pattern_history"] = [{"revision": revision, "generated_at": generated_at,
                                       "conclusion": {key: deepcopy(pattern[key]) for key in validate.PATTERN_KEYS}}]
        patterns.append(pattern)
    references = context.keymap.get("reference_repositories", {}).get("limitations", [])
    limitations = [validate.EXTRACTION_LEDGER_PREFIX + answer["extraction_ledger"].strip()] \
        + list(references) + list(answer["limitations"])
    return {"schema_version": "1.0.0", "generated_by": deepcopy(context.corpus["generated_by"]),
            "analysis_generated_by": {"name": validate.ANALYZER_NAME, "revision": revision},
            "records": records, "patterns": patterns, "limitations": limitations}


def cmd_assemble(context: Context, args) -> dict:
    if not context.wf.parse_utc(args.generated_at) or not args.generated_at.endswith("Z"):
        raise StageError("--generated-at must be an RFC3339 UTC time ending in Z")
    out = Path(args.out)
    if os.path.lexists(out):
        raise StageError("output must not exist yet: %s" % out)
    answer = read_json(Path(args.final), ANSWER_MAX_BYTES, "final answer")
    check_final_answer(context, answer)
    envelope = build_envelope(context, answer, args.generated_at)
    analysis_keys = {"analysis", "analysis_history"}
    for original, enriched in zip(context.records, envelope["records"]):
        if {k: v for k, v in enriched.items() if k not in analysis_keys} != {k: v for k, v in original.items() if k not in analysis_keys}:
            raise StageError("assembled record %s lost a corpus field" % original.get("pr_id"))
    handle, temporary = tempfile.mkstemp(prefix=".analysis-", suffix=".json", dir=str(out.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        code, stdout, stderr = context.wf.run_main(context.siblings.validate_corpus.main,
                                                   [str(context.corpus_path), "--analysis-output", temporary]
                                                   + context.reference_args())
        if code != 0:
            raise StageError("validate_corpus rejected the assembled analysis: " + (stderr or stdout).strip())
        os.replace(temporary, out)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return {"stage": "assemble", "output": str(out), "records": len(envelope["records"]),
            "patterns": [pattern["pattern_id"] for pattern in envelope["patterns"]], "validator": stdout.strip(),
            "note": "an empty patterns array is a recorded insufficient outcome, not a failure"}


# -- assess (#259): the candidate assessment of one discovery, by the verifier's own contract ----------------

ASSESS_MAX_ASSESSMENTS = 10
ASSESS_STEM = "assess"
# The repository facts every discovery record of one repository repeats; the prompt carries them once.
REPOSITORY_FACT_KEYS = ("repository", "repository_node_id", "head_sha", "observed_at", "repository_checks",
                        "upstream_checks")
# What a prompt needs of a pattern to judge it; pattern_history and generated_by stay in analysis.json.
ASSESS_PATTERN_KEYS = ("pattern_id", "description", "applicability", "counterconditions", "search_clues",
                       "expected_tests", "maintainer_judgment_required", "provenance_mode", "confidence",
                       "source_licenses", "evidence_pr_ids")


def assess_call_plan(calls: int = 1) -> dict:
    """The model calls of an assess stage: one per discovery document (every combination it holds)."""
    return {"mode": "assess", "assess": calls, "max_model_calls": calls,
            "formula": "%d (assess: one call per discovery, all of its pattern x repository combinations) = %d"
                       % (calls, calls)}


def load_assess_contract():
    """assess_contract.py beside this file (the assessment field types, #266)."""
    existing = sys.modules.get("assess_contract")
    if existing is not None and os.path.realpath(getattr(existing, "__file__", "")) == os.path.realpath(HERE / "assess_contract.py"):
        return existing
    spec = importlib.util.spec_from_file_location("assess_contract", HERE / "assess_contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["assess_contract"] = module
    spec.loader.exec_module(module)
    return module


def load_verifier(skills_root: str | None):
    """The candidate verifier, resolved like the workflow resolves its siblings (never from input data)."""
    wf = load_workflow()
    return wf.Siblings(wf.resolve_siblings(skills_root)).verifier


def https_values(value, found: list) -> list:
    """Every https:// string in an observation, except the body of an untrusted remote text (its words are not
    an observation the verifier made)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "text" and value.get("untrusted") is True:
                continue
            https_values(item, found)
    elif isinstance(value, list):
        for item in value:
            https_values(item, found)
    elif isinstance(value, str) and value.startswith("https://") and not any(c.isspace() for c in value):
        found.append(value)
    return found


def policy_source(source_repository: str, path: str) -> str:
    return "%s:%s" % (source_repository, path)


def policy_table(vc, repository: str, policy_files: list, evidence: list) -> dict:
    """Per policy key: what a ``found: true`` may name (source and digest of bytes the input holds), and how each
    mapped path was observed, by the verifier's own key-to-path map."""
    owner = repository.split("/", 1)[0]
    sources = [repository] + ([owner + "/.github"] if owner + "/.github" != repository else [])
    observed = {}
    for item in policy_files:
        if isinstance(item, dict):
            observed.setdefault((item.get("source_repository"), item.get("path")), []).append(item)
    table = {}
    for key in vc.REVIEWABLE_POLICY_KEYS:
        paths = vc.POLICY_CHECK_PATHS[key]
        found, states = [], {"absent": [], "request-failed": [], "not-observed": []}
        for source in sources:
            for path in paths:
                items = observed.get((source, path)) or []
                if not items:
                    states["not-observed"].append(policy_source(source, path))
                for item in items:
                    if item.get("status") == "found" and item.get("found") is True and isinstance(item.get("sha256"), str):
                        found.append({"source": policy_source(source, path), "sha256": item["sha256"]})
                    elif item.get("status") in ("absent", "request-failed"):
                        states[item["status"]].append(policy_source(source, path))
                    else:
                        states["not-observed"].append(policy_source(source, path))
        reviewed = [{"source": policy_source(entry["source_repository"], entry["path"]), "sha256": entry["sha256"]}
                    for entry in evidence if entry["repository"] == repository and entry["policy_key"] == key]
        table[key] = {"mapped_paths": list(paths), "found": found, "hand_reviewed": reviewed,
                      "absent": states["absent"], "request_failed": states["request-failed"],
                      "not_observed": states["not-observed"]}
    table["program_rules"] = {"found": [], "hand_reviewed": [], "note": "no program rules input: found null, sha256 null"}
    return table


def assess_pattern(pattern: dict, urls: dict) -> dict:
    view = {key: deepcopy(pattern[key]) for key in ASSESS_PATTERN_KEYS if key in pattern}
    view["evidence_pr_urls"] = [urls[pr_id] for pr_id in pattern.get("evidence_pr_ids") or []
                                if isinstance(urls.get(pr_id), str)]
    return view


def static_reads_not_assessable(discovery: dict, records: list) -> list:
    """The discovery's top-level ``static_reads`` (verifier discover --static-reads rest, #266 defect 2): one entry per
    repository whose finished static reads stay even when the budget ended its combinations.

    A repository with a discovery record is assessed from that record, whose ``static_search`` must be the entry's
    copy (refused otherwise: the input would hold two readings). A repository without one is not assessable: the
    record gate refuses an assessment of a combination that is absent, skipped or failed. Its reads are listed for the
    report only, never put into the prompt."""
    entries = discovery.get("static_reads")
    if entries is None:
        return []
    if not isinstance(entries, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("repository"), str)
            and isinstance(entry.get("static_search"), dict) for entry in entries):
        raise StageError("discovery static_reads must be a list of {repository, head_sha, static_search}")
    listed = []
    for entry in entries:
        recorded = [record for record in records if record.get("repository") == entry["repository"]]
        if recorded:
            if any(record.get("static_search") != entry["static_search"] for record in recorded):
                raise StageError("the static_reads of %s are not the static_search its discovery records hold"
                                 % entry["repository"])
            continue
        static = entry["static_search"]
        listed.append({"repository": entry["repository"], "head_sha": entry.get("head_sha"),
                       "hits": len(static.get("hits") or []), "files": len(static.get("files") or []),
                       "error": static.get("error"),
                       "reason": "no discovery record: the record gate refuses its combinations"})
    return listed


def cmd_assess_inputs(args) -> dict:
    """One assess call for one discovery document: its prompt, its scope and its call plan (#259).

    The analysis must be the one discovery read (``inputs.analysis_sha256``). Every combination discovery recorded
    is one unit of the call; skipped and failed scopes cannot be assessed (verifier contract) and are only named.
    The prompt is measured whole and refused over PROMPT_MAX_BYTES before anything is written; it is never cut and
    never split here (several answers would need one merged assessment for locus-search and record)."""
    vc = load_verifier(args.skills_root)
    analysis_bytes = read_bounded(Path(args.analysis), CORPUS_MAX_BYTES, "analysis")
    discovery_bytes = read_bounded(Path(args.discovery), CORPUS_MAX_BYTES, "discovery")
    analysis, discovery = parse_json(analysis_bytes, "analysis"), parse_json(discovery_bytes, "discovery")
    if not isinstance(analysis, dict) or not isinstance(discovery, dict):
        raise StageError("the analysis and the discovery must be JSON objects")
    try:
        vc._validate_discovery_document(discovery)
    except vc.CliInputError as error:
        raise StageError("discovery: %s" % error) from error
    analysis_sha = hashlib.sha256(analysis_bytes).hexdigest()
    discovery_sha = hashlib.sha256(discovery_bytes).hexdigest()
    if discovery["inputs"].get("analysis_sha256") != analysis_sha:
        raise StageError("the analysis is not the one this discovery read (inputs.analysis_sha256 %s, analysis %s)"
                         % (discovery["inputs"].get("analysis_sha256"), analysis_sha))
    records = discovery["records"]
    not_assessable_reads = static_reads_not_assessable(discovery, records)
    if not records:
        held = "; ".join("static_reads of %s (%d hits) stay in the discovery for the report"
                         % (item["repository"], item["hits"]) for item in not_assessable_reads if item["hits"])
        raise StageError("no discovery record to assess (skipped or failed scopes cannot be assessed)"
                         + ("; " + held if held else ""))
    if len(records) > ASSESS_MAX_ASSESSMENTS:
        raise StageError("%d combinations are more than one call may assess (%d)" % (len(records), ASSESS_MAX_ASSESSMENTS))
    patterns = {pattern.get("pattern_id"): pattern for pattern in analysis.get("patterns") or [] if isinstance(pattern, dict)}
    urls = {record.get("pr_id"): (record.get("pull_request") or {}).get("url") for record in analysis.get("records") or []
            if isinstance(record, dict)}
    evidence = []
    evidence_sha = None
    if args.policy_evidence:
        try:
            _, evidence = vc._policy_evidence_input(args.policy_evidence)
        except vc.CliInputError as error:
            raise StageError("policy evidence: %s" % error) from error
        evidence_sha = hashlib.sha256(read_bounded(Path(args.policy_evidence), ANSWER_MAX_BYTES,
                                                   "policy evidence")).hexdigest()

    combinations, by_repository, links = [], {}, []
    for record in records:
        if record["pattern_id"] not in patterns:
            raise StageError("discovery names %s, which the analysis does not hold" % record["pattern_id"])
        combinations.append({"pattern_id": record["pattern_id"], "repository": record["repository"]})
        by_repository.setdefault(record["repository"], []).append(record)
        https_values(record, links)
        links += assess_pattern(patterns[record["pattern_id"]], urls)["evidence_pr_urls"]
        base = "https://github.com/" + record["repository"]
        links.append(base)
        if isinstance(record.get("head_sha"), str):
            links.append(base + "/commit/" + record["head_sha"])
            static = record.get("static_search") if isinstance(record.get("static_search"), dict) else {}
            for hit in static.get("hits") or []:
                if isinstance(hit, dict) and isinstance(hit.get("path"), str) and type(hit.get("line")) is int:
                    links.append("%s/blob/%s/%s#L%d" % (base, record["head_sha"], hit["path"], hit["line"]))
    for entry in evidence:
        if isinstance(entry.get("source_url"), str) and entry["source_url"].startswith("https://"):
            links.append(entry["source_url"])
    links = sorted(set(links))

    text = read_bounded(PROMPTS / "assess.md", PROMPT_MAX_BYTES, "assess template").decode("utf-8")
    # every field's type from the one table the strict check uses (assess_contract, #266)
    text = text.replace("{{assessment_field_types}}", load_assess_contract().prompt_lines(vc))
    text += block("판정 단위", {"discovery_sha256": discovery_sha, "combinations": combinations,
                              "max_assessments": ASSESS_MAX_ASSESSMENTS,
                              "not_assessable": [{"section": section, "pattern_id": item.get("pattern_id"),
                                                  "repository": item.get("repository"), "reason": item.get("reason")}
                                                 for section in ("skipped_by_cap", "failed_scopes")
                                                 for item in discovery[section]]})
    for pattern_id in dict.fromkeys(item["pattern_id"] for item in combinations):
        text += block("패턴 " + pattern_id, assess_pattern(patterns[pattern_id], urls))
    allowed_policy = []
    sizes = {}
    for repository, group in by_repository.items():
        first = group[0]
        if any(record.get("policy_files") != first.get("policy_files") for record in group):
            raise StageError("the discovery records of %s hold different policy observations" % repository)
        text += block("저장소 사실 " + repository, {key: first.get(key) for key in REPOSITORY_FACT_KEYS})
        text += block("정책 관찰 " + repository, first.get("policy_files") or [])
        table = policy_table(vc, repository, first.get("policy_files") or [], evidence)
        text += block("정책 키별 허용 값 " + repository, table)
        for key in vc.REVIEWABLE_POLICY_KEYS:
            for value in table[key]["found"] + table[key]["hand_reviewed"]:
                allowed_policy.append({"key": key, "source": value["source"], "sha256": value["sha256"]})
        for record in group:
            view = {key: deepcopy(value) for key, value in record.items() if key != "policy_files"
                    and not (key in REPOSITORY_FACT_KEYS[1:] and value == first.get(key))}
            title = "discovery 관찰 %s × %s" % (record["pattern_id"], repository)
            observation = block(title, view)
            sizes[title] = len(observation.encode("utf-8"))
            text += observation
    if evidence:
        text += block("손으로 확인한 정책 근거", [{key: entry[key] for key in ("repository", "policy_key", "path",
                                                                     "source_repository", "source_url", "sha256",
                                                                     "excerpt")} for entry in evidence])
    text += block("허용 근거 링크", "\n".join(links))
    size = len(text.encode("utf-8"))
    if size > PROMPT_MAX_BYTES:
        raise StageError("the assess prompt would be %d bytes, over %d (65536); refused before anything is written, "
                         "nothing is cut (discovery observation blocks: %s)"
                         % (size, PROMPT_MAX_BYTES, ", ".join("%s %d B" % item for item in sizes.items())))
    unique_policy = []
    for item in allowed_policy:
        if item not in unique_policy:
            unique_policy.append(item)

    out = new_directory(args.out)
    entry = write_prompt(out / ASSESS_STEM, text)
    entry["result_name"] = ASSESS_STEM + ".result.json"
    scope = {"kind": "assess", "discovery_sha256": discovery_sha, "analysis_sha256": analysis_sha,
             "combinations": combinations, "max_assessments": ASSESS_MAX_ASSESSMENTS, "links": links,
             "policy": unique_policy, "policy_evidence_sha256": evidence_sha}
    write_json(out / (ASSESS_STEM + ".scope.json"), scope)
    plan = assess_call_plan(1)
    plan["inputs"] = {"combinations": len(combinations), "repositories": len(by_repository)}
    summary = {"stage": "assess", "mode": "assess", "calls": [entry], "max_model_calls": plan["max_model_calls"],
               "call_plan": plan, "discovery_sha256": discovery_sha, "analysis_sha256": analysis_sha,
               "policy_evidence_sha256": evidence_sha, "combinations": combinations,
               "static_reads_not_assessable": not_assessable_reads,
               "prompt_budget": {"bytes": entry["bytes"], "cap": PROMPT_MAX_BYTES}}
    write_json(out / "stage.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--skills-root")
        return sub

    command("extract-inputs").add_argument("--out", required=True)
    command("dry-plan")
    resume = command("continue-plan")
    resume.add_argument("--extract-stage", required=True,
                        help="the extract-inputs output directory whose stage.json made the adopted answers' prompts, "
                             "or a continue-plan output directory whose ledger has adopted reduce answers")
    resume.add_argument("--extract-result", action="append", required=True)
    resume.add_argument("--records", required=True,
                        help="directory with <stem>/verdict.json (strict) and <stem>/result/manifest.json per call "
                             "(for a continue stage: the parent ledger directory, with ledger.jsonl)")
    resume.add_argument("--synthesis-result", help="continue stage only: the adopted synthesis (or merge) answer")
    resume.add_argument("--verify-result", action="append", default=[],
                        help="continue stage only: the adopted verify answers (verify-<id>.result.json)")
    resume.add_argument("--ancestor-records", action="append", default=[],
                        help="continue stage only: the ancestor ledger directories of --records, nearest first "
                             "(each the parent its successor's ledger-limit.json records), where the adopted "
                             "answers the continue stage took from them are bound (#258)")
    resume.add_argument("--out", required=True)
    synthesis = command("synthesis-input")
    synthesis.add_argument("--extract-result", action="append", required=True)
    synthesis.add_argument("--out", required=True)
    merge = command("synthesis-merge-input")
    merge.add_argument("--synthesis-stage", required=True,
                       help="the synthesis-input output directory whose stage.json lists the groups")
    merge.add_argument("--group-result", action="append", required=True)
    merge.add_argument("--extract-result", action="append", required=True)
    merge.add_argument("--out", required=True)
    verify = command("verify-inputs")
    verify.add_argument("--synthesis", required=True)
    verify.add_argument("--extract-result", action="append", required=True)
    verify.add_argument("--out", required=True)
    final = command("final-input")
    final.add_argument("--synthesis", required=True)
    final.add_argument("--extract-result", action="append", required=True)
    final.add_argument("--verify-result", action="append", default=[])
    final.add_argument("--out", required=True)
    assess = commands.add_parser("assess-inputs", help="the assess call of one discovery (#259); no run directory")
    assess.add_argument("--analysis", required=True, help="the analysis.json the discovery read")
    assess.add_argument("--discovery", required=True, help="the verifier discover output (discovery.json)")
    assess.add_argument("--policy-evidence", help="hand-reviewed policy evidence, as record --policy-evidence takes it")
    assess.add_argument("--skills-root")
    assess.add_argument("--out", required=True)
    assemble = command("assemble")
    assemble.add_argument("--final", required=True)
    assemble.add_argument("--generated-at", required=True)
    assemble.add_argument("--out", required=True)
    return parser


HANDLERS = {
    "extract-inputs": cmd_extract_inputs,
    "dry-plan": cmd_dry_plan,
    "continue-plan": cmd_continue_plan,
    "synthesis-input": cmd_synthesis_input,
    "synthesis-merge-input": cmd_synthesis_merge_input,
    "verify-inputs": cmd_verify_inputs,
    "final-input": cmd_final_input,
    "assemble": cmd_assemble,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "assess-inputs":
            summary = cmd_assess_inputs(args)
        else:
            context = Context(args.run_dir, args.skills_root)
            summary = HANDLERS[args.command](context, args)
    except (StageError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    except Exception as error:  # workflow errors carry their own message
        if type(error).__name__ != "WorkflowError":
            raise
        print("error: %s" % error, file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.command == "dry-plan" and not summary["ok"]:
        print("error: the worst-case plan does not pass: %s" % "; ".join(summary["refusals"]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
