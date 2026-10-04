#!/usr/bin/env python3
"""#78 actual E2E: host-side glue between isolated model calls, by the analyzer's own contract.

One workflow run (``oss_workflow.py init`` → ``adopt-collection`` → ``digest``) already
holds the collected corpus, the digest, and the five lens extraction prompts. This
script only moves bytes between that run and one input directory per isolated model
call, and assembles the final analysis envelope deterministically:

  extract-inputs   RUN → OUT/extract-<lens>/task.txt           (5 independent calls)
  synthesis-input  RUN + extract results → OUT/task.txt        (1 call: candidates)
  verify-inputs    RUN + synthesis answer → OUT/verify-<id>/task.txt  (k calls, k may be 0)
  final-input      RUN + all answers → OUT/task.txt            (1 call: judgment fields)
  assemble         RUN + final answer → analysis.json          (no model; validated)

Then the existing workflow commands finish the run:
``adopt-analysis --analysis analysis.json --origin ai --extraction-result ...`` and ``report``.

Reused, never re-implemented: build_prompts.py (extract and verify prompts),
check_extraction.py (every quote against the corpus), validate_corpus.py
(``--analysis-output``), and oss_workflow.py (run state, integrity, report).
``assemble`` deep-copies every corpus record and fills the deterministic fields
(files_changed, evidence_manifest, license_spdx, revisions, times, pattern
generated_by/source_licenses/history) itself; a model answer that carries any of
them is refused. Nothing here opens the network, starts a model, or reads a credential.

Counts follow run_limits.json and are refused, never truncated: at most
``max_patterns`` candidates (so at most that many verify calls) and final
patterns, ``max_rejected`` dropped ideas, and the extract caps per lens result.
The model calls of one run are therefore at most
``shards x lenses + 1 (synthesis) + max_candidates (verify) + 1 (final)``: 12
for one shard (5 + 1 + 5 + 1), 17 for two. The digest is sharded by
``oss_workflow.py digest --max-bytes`` so that each lens prompt stays under the
worker's input limit; every lens runs on every shard, and a prompt over
PROMPT_MAX_BYTES is refused before anything is written, never cut.

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
REPO = HERE.parents[2]
WORKFLOW = REPO / "dot_agents/skills/researching-open-source-contributions/scripts/oss_workflow.py"
PROMPTS = HERE / "prompts"
LIMITS = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))
MAX_CANDIDATES = LIMITS["max_patterns"]
MAX_FINAL_PATTERNS = LIMITS["max_patterns"]
MAX_DROPPED = LIMITS["max_rejected"]
EXTRACT_CAPS = {"patterns": LIMITS["max_patterns"], "observations": LIMITS["max_observations"],
                "rejected": LIMITS["max_rejected"]}
EXTRACT_CALLS = 5


def model_call_plan(shards: int, lenses: int, max_candidates: int) -> dict:
    """The most model calls one run can make, from the corpus-dependent shard count.

    Every lens reads every shard (extract), then one synthesis, at most one verify per
    candidate, and one final. The limit is recorded in the extract stage's manifest.
    """
    total = shards * lenses + 1 + max_candidates + 1
    return {"shards": shards, "lenses": lenses, "extract": shards * lenses, "synthesis": 1,
            "verify": max_candidates, "final": 1, "max_model_calls": total,
            "formula": "%d shards x %d lenses + 1 (synthesis) + %d (verify, at most max_candidates) + 1 (final) = %d"
                       % (shards, lenses, max_candidates, total)}


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
    data = read_bounded(Path(path), limit, label)
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
    shards = "\n\n".join(
        read_bounded(context.digest_dir / name, PROMPT_MAX_BYTES, "digest shard").decode("utf-8").rstrip()
        for name in context.keymap["shards"])
    text = read_bounded(PROMPTS / "finalize.md", PROMPT_MAX_BYTES, "finalize template").decode("utf-8")
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
        if not isinstance(candidate["id"], str) or not context.siblings.build_prompts.CANDIDATE_ID.fullmatch(candidate["id"]) \
                or candidate["id"] in seen:
            raise StageError(label + " id must be unique [A-Za-z0-9_-]+")
        seen.add(candidate["id"])
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


# -- commands ----------------------------------------------------------------


def cmd_extract_inputs(context: Context, args) -> dict:
    lenses = len(context.siblings.build_prompts.LENSES)
    plan = model_call_plan(context.shards, lenses, MAX_CANDIDATES)
    plan["inputs"] = {"max_candidates": MAX_CANDIDATES, "shards": context.shards, "lenses": lenses}
    # Every prompt is checked before anything is written: one over the cap refuses the whole stage.
    texts = {stem: read_bounded(context.extract_prompts / (stem + ".md"), 4 * PROMPT_MAX_BYTES,
                                "extract prompt").decode("utf-8") for stem in context.extract_stems()}
    oversize = ["%s is %d bytes" % (stem, len(text.encode("utf-8"))) for stem, text in sorted(texts.items())
                if len(text.encode("utf-8")) > PROMPT_MAX_BYTES]
    if oversize:
        raise StageError("extract prompts over %d bytes, refused before anything is written (lower digest "
                         "--max-bytes): %s" % (PROMPT_MAX_BYTES, "; ".join(oversize)))
    fixed = len(final_prompt_text(context, [], {}, EMPTY_REPORT).encode("utf-8"))
    final_budget = {"fixed_bytes": fixed, "reserve_bytes": FINAL_RESERVE_BYTES, "cap": PROMPT_MAX_BYTES,
                    "note": "the reserve is a policy floor, not a worst-case guarantee"}
    if fixed + FINAL_RESERVE_BYTES > PROMPT_MAX_BYTES:
        what = ("fixed part %d + reserve %d for candidates, verify answers and quotes"
                % (fixed, FINAL_RESERVE_BYTES))
        if context.shards == 1 or not args.allow_final_overflow:
            error = budget_refusal("extract-inputs", fixed + FINAL_RESERVE_BYTES, what)
            if context.shards > 1:
                error = StageError("%s; the digest has %d shards and the final prompt carries all of them. Pass "
                                   "--allow-final-overflow to write the extract prompts anyway (every prompt is "
                                   "still checked; synthesis-input and final-input will still refuse an oversize "
                                   "prompt)" % (error, context.shards))
            raise error
        final_budget.update(status="over-cap", note="the final prompt carries the whole sharded digest and cannot "
                            "fit one call: final-input will refuse (nothing is cut); a final stage for sharded "
                            "digests is needed before the model run reaches it")
    out = new_directory(args.out)
    written = []
    for stem, text in sorted(texts.items()):
        entry = write_prompt(out / stem, text)
        entry["result_name"] = stem + ".result.json"
        written.append(entry)
    summary = {"stage": "extract", "calls": written, "max_model_calls": plan["max_model_calls"], "call_plan": plan,
               "final_budget": final_budget}
    write_json(out / "stage.json", summary)
    return summary


def cmd_synthesis_input(context: Context, args) -> dict:
    stems = context.extract_stems()
    results = load_results(args.extract_result, stems, "extract result")
    for stem, (_, document) in results.items():
        lens = EXTRACT_STEM.fullmatch(stem).group(1)
        if set(document) != {"lens", "patterns", "observations", "rejected"} or document["lens"] != lens:
            raise StageError("extract result %s must be the %s lens object" % (stem, lens))
        for key, cap in EXTRACT_CAPS.items():
            if not isinstance(document[key], list) or len(document[key]) > cap:
                raise StageError("extract result %s may hold at most %d %s" % (stem, cap, key))
    report = check_quotes(context, [path for path, _ in results.values()])
    compact = compact_report(report)
    projected = len(final_prompt_text(context, [], {}, compact).encode("utf-8"))
    if projected > PROMPT_MAX_BYTES:
        raise budget_refusal("synthesis-input", projected, "digest and the verified extract quotes alone")
    text = read_bounded(PROMPTS / "synthesize.md", PROMPT_MAX_BYTES, "synthesis template").decode("utf-8")
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


def cmd_verify_inputs(context: Context, args) -> dict:
    candidates = parse_synthesis(context, args.synthesis)
    extract = load_results(args.extract_result, context.extract_stems(), "extract result")
    compact = compact_report(check_quotes(context, [path for path, _ in extract.values()]))
    base = len(final_prompt_text(context, candidates, {}, compact).encode("utf-8"))
    k = len(candidates)
    floor, bound = base + k * VERIFY_FLOOR_BYTES, base + k * 2 * VERIFY_ANSWER_MAX_BYTES
    if floor > PROMPT_MAX_BYTES:
        raise budget_refusal("verify-inputs", floor, "%d candidates with at least %d bytes per verify answer"
                             % (k, VERIFY_FLOOR_BYTES))
    budget = {"base_bytes": base, "floor_bytes": floor, "bound_bytes": bound, "cap": PROMPT_MAX_BYTES,
              "guaranteed": bound <= PROMPT_MAX_BYTES,
              "note": "bound assumes each verify answer at most verify_answer_max_bytes, counted twice (block and "
                      "quotes); when not guaranteed, final-input may still refuse after the verify calls"}
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
    text = final_prompt_text(context, candidates, {stem: doc for stem, (_, doc) in verify.items()},
                             compact_report(report))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--skills-root")
        return sub

    extract = command("extract-inputs")
    extract.add_argument("--out", required=True)
    extract.add_argument("--allow-final-overflow", action="store_true",
                         help="sharded digest only: write the extract prompts although the final prompt "
                              "(which carries every shard) cannot fit one call")
    synthesis = command("synthesis-input")
    synthesis.add_argument("--extract-result", action="append", required=True)
    synthesis.add_argument("--out", required=True)
    verify = command("verify-inputs")
    verify.add_argument("--synthesis", required=True)
    verify.add_argument("--extract-result", action="append", required=True)
    verify.add_argument("--out", required=True)
    final = command("final-input")
    final.add_argument("--synthesis", required=True)
    final.add_argument("--extract-result", action="append", required=True)
    final.add_argument("--verify-result", action="append", default=[])
    final.add_argument("--out", required=True)
    assemble = command("assemble")
    assemble.add_argument("--final", required=True)
    assemble.add_argument("--generated-at", required=True)
    assemble.add_argument("--out", required=True)
    return parser


HANDLERS = {
    "extract-inputs": cmd_extract_inputs,
    "synthesis-input": cmd_synthesis_input,
    "verify-inputs": cmd_verify_inputs,
    "final-input": cmd_final_input,
    "assemble": cmd_assemble,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
