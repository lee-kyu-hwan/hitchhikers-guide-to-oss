#!/usr/bin/env python3
"""Checkpoint glue for the offline open-source contribution research workflow.

This script connects the existing sibling skills; it does not replace them.

* collecting-recent-closed-prs produces a corpus and manifest (network step,
  never run here);
* collecting-curated-contribution-prs produces the same kind of pull request
  corpus from one tracker Issue (network step, never run here);
* analyzing-open-source-pr-patterns validates the corpus, builds the digest
  and prompts, and gates the AI-written analysis output;
* verifying-open-source-contribution-candidates discovers and locus-searches
  (network steps, never run here) and records candidates offline.

Every command works inside one run directory. Adopted inputs are copied into
a new generation directory, validated with the sibling validators, and only
then made active. Checkpoints bind the request hash, input and output SHA-256
values, and the sibling revisions. Nothing in this file opens a socket or
starts a process: network steps are printed as plan data and are never
executed, and live execution belongs only to a verified isolated research
process.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0.0"
SKILL_NAME = "researching-open-source-contributions"
SKILL_DIR = Path(__file__).resolve().parents[1]
REVISION_PATHS = (
    "SKILL.md",
    "references/workflow-contract.md",
    "scripts/oss_workflow.py",
)
ROOT_ENVIRONMENT = "OSS_WORKFLOW_SKILLS_ROOT"
SIBLINGS = {
    "collector": ("collecting-recent-closed-prs", "scripts/collect_recent_closed_prs.py"),
    "curated": ("collecting-curated-contribution-prs", "scripts/collect_curated_contribution_prs.py"),
    "analyzer": ("analyzing-open-source-pr-patterns", "scripts/validate_corpus.py"),
    "verifier": ("verifying-open-source-contribution-candidates", "scripts/verify_candidates.py"),
}
MODES = ("offline-fixture", "offline-reuse")
SUPPORTED_SOURCE_KINDS = ("recent-closed-prs", "curated-tracker")
# The Issue corpus is not a pull request corpus; its skill forbids the analyzer handoff.
UNSUPPORTED_SOURCE_KINDS = ("issues",)
CURATED_METHOD = "tracker-issue-comments"
CURATED_GENERATOR = "collecting-curated-contribution-prs"
ORIGINS = ("ai", "synthetic-fixture")
STAGES = ("collect", "digest", "analysis", "discovery", "locus-search", "record")
CANDIDATE_STAGES = ("discovery", "locus-search", "record")
TARGET_UNSPECIFIED = ("no target repository was specified (request target is null); candidate verification "
                      "(discovery, locus-search, record) is skipped")
DOWNSTREAM = {
    "collect": ("digest", "analysis", "discovery", "locus-search", "record"),
    "digest": (),
    "analysis": ("discovery", "locus-search", "record"),
    "discovery": ("locus-search", "record"),
    "locus-search": ("record",),
    "record": (),
}
USABLE = {
    "collect": ("complete", "partial"),
    "digest": ("complete",),
    "analysis": ("complete",),
    "discovery": ("complete", "partial"),
    "locus-search": ("complete", "partial"),
    "record": ("complete", "partial"),
}
ACTIONABLE_STATUSES = ("reproduced", "issue-ready", "pr-ready", "private-report-ready")
DECISIVE_NON_ACTIONABLE = ("duplicate", "not-applicable", "stale")
# One lens prompt is the analyzer's instructions around one digest shard. A worker takes a prompt of at
# most 65,536 bytes, so the target leaves room under it, and the digest shard size is the target minus a
# reserve for those instructions (measured 4,193 bytes around a shard of a four-repository corpus; the
# reserve also covers a longer repository list). The analyzer's own default (160,000) is for readers
# without that limit and never fits one worker call.
DIGEST_PROMPT_TARGET_BYTES = 60_000
DIGEST_PROMPT_RESERVE_BYTES = 6_000
DIGEST_MAX_BYTES = DIGEST_PROMPT_TARGET_BYTES - DIGEST_PROMPT_RESERVE_BYTES
OUTCOME_KINDS = ("merged", "closed-unmerged")
COLLECTOR_WINDOW_DAYS = 31
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
RUN_MARKER = "workflow-run.json"
STATE = "state.json"
REQUEST = "request.json"
EXIT = {"complete": 0, "partial": 3, "failed": 4, "blocked": 4}


class WorkflowError(Exception):
    """A rejected command; nothing in the run directory changed."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_sha256(value: object) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def read_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError("%s is not UTF-8 JSON: %s" % (label, error)) from error


def write_json_atomic(path: Path, value: object) -> None:
    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def compute_revision(skill_dir: Path = SKILL_DIR) -> str:
    """Framed SHA-256 over this skill's contract files, like the siblings."""
    digest = hashlib.sha256()
    for relative in sorted(REVISION_PATHS):
        path_bytes = relative.encode("utf-8")
        content = (Path(skill_dir) / relative).read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def run_main(main, argv: Sequence[str], **kwargs) -> Tuple[int, str, str]:
    """Call a sibling CLI ``main`` in process and capture its output."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(list(argv), **kwargs)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 2
    return int(code or 0), stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# Sibling skill resolution (caller-chosen trusted roots only)
# ---------------------------------------------------------------------------


def candidate_roots(explicit: Optional[str], environ=os.environ, home: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """Roots are taken only from the caller: flag, environment, or install layout.

    Corpus, analysis, or remote text never supplies an import path.
    """
    if explicit:
        return [("--skills-root", Path(explicit))]
    if environ.get(ROOT_ENVIRONMENT):
        return [(ROOT_ENVIRONMENT, Path(environ[ROOT_ENVIRONMENT]))]
    roots = [("shared-skill-parent", SKILL_DIR.parent)]
    if SKILL_DIR.parent.name == "skills" and SKILL_DIR.parent.parent.name == "dot_agents":
        roots.append(("chezmoi-source-checkout", SKILL_DIR.parent.parent.parent / "dot_codex" / "skills"))
    roots.append(("codex-install", (home or Path.home()) / ".codex" / "skills"))
    return roots


def resolve_siblings(explicit: Optional[str] = None, environ=os.environ, home: Optional[Path] = None) -> Dict[str, object]:
    tried = []
    for label, root in candidate_roots(explicit, environ, home):
        scripts = {key: root / skill / script for key, (skill, script) in SIBLINGS.items()}
        if all(path.is_file() for path in scripts.values()):
            return {
                "source": label,
                "root": str(root),
                "realpath": os.path.realpath(root),
                "scripts": {key: str(path) for key, path in scripts.items()},
            }
        tried.append("%s=%s" % (label, root))
    raise WorkflowError("sibling skills not found in: " + ", ".join(tried))


class Siblings:
    """Sibling modules loaded by path from one resolved root."""

    def __init__(self, resolution: Dict[str, object]):
        self.resolution = resolution
        scripts = {key: Path(value) for key, value in resolution["scripts"].items()}
        analyzer_dir = scripts["analyzer"].parent
        self.collector = _load_module("collect_recent_closed_prs", scripts["collector"])
        self.curated = _load_module("collect_curated_contribution_prs", scripts["curated"])
        self.validate_corpus = _load_module("validate_corpus", analyzer_dir / "validate_corpus.py")
        self.build_digest = _load_module("build_digest", analyzer_dir / "build_digest.py")
        self.build_prompts = _load_module("build_prompts", analyzer_dir / "build_prompts.py")
        self.check_extraction = _load_module("check_extraction", analyzer_dir / "check_extraction.py")
        self.verifier = _load_module("verify_candidates", scripts["verifier"])

    def revisions(self) -> Dict[str, str]:
        return {
            "collector": self.collector.compute_skill_revision(),
            "curated": self.curated.compute_skill_revision(),
            "analyzer": self.validate_corpus._canonical_revision(),
            "verifier": self.verifier.compute_revision(),
            "workflow": compute_revision(),
        }


def _load_module(name: str, path: Path):
    """Load a sibling script under its plain name (build_digest imports validate_corpus)."""
    path = Path(path)
    existing = sys.modules.get(name)
    if existing is not None:
        loaded = getattr(existing, "__file__", None)
        if loaded and os.path.realpath(loaded) == os.path.realpath(path):
            return existing
        raise WorkflowError("module %s is already loaded from %s, not %s" % (name, loaded, path))
    directory = str(path.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise WorkflowError("cannot load " + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def _positive_int(value: object, label: str, errors: List[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(label + " must be a positive integer")


def _repositories(value: object, label: str, errors: List[str]) -> List[str]:
    if not isinstance(value, list) or not value:
        errors.append(label + " must be a non-empty list")
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or REPOSITORY.fullmatch(item) is None or item.split("/")[1] in (".", ".."):
            errors.append("%s has an invalid owner/name: %r" % (label, item))
        elif item.lower() in {existing.lower() for existing in result}:
            errors.append("%s repeats %s" % (label, item))
        else:
            result.append(item)
    return result


def _curated_source_errors(source: dict, errors: List[str]) -> None:
    """One tracker Issue; `repositories` is the caller's allow list for the pull requests it names."""
    source_keys = {"kind", "tracker", "repositories", "max_prs", "request_budget", "reference_repositories"}
    if set(source) != source_keys:
        errors.append("source keys must be exactly %s for curated-tracker" % sorted(source_keys))
        return
    tracker = source["tracker"]
    if not isinstance(tracker, dict) or set(tracker) != {"repository", "issue_number"}:
        errors.append("source.tracker must be {repository, issue_number}")
    else:
        _repositories([tracker["repository"]], "source.tracker.repository", errors)
        _positive_int(tracker["issue_number"], "source.tracker.issue_number", errors)
    _positive_int(source["max_prs"], "source.max_prs", errors)
    _positive_int(source["request_budget"], "source.request_budget", errors)
    references = source["reference_repositories"]
    if not isinstance(references, list):
        errors.append("source.reference_repositories must be a list")
    elif references:
        _repositories(references, "source.reference_repositories", errors)


# The candidate-reads entry's relay budget limit (dot_local/bin/executable_oss-research-candidate-reads
# MAX_REQUEST_BUDGET). Discover and locus-search are read through that entry, and adopt-discovery and
# adopt-locus-search require the run's budget to equal the request's, so a larger request budget could never be
# adopted (#82). init refuses it.
CANDIDATE_READS_MAX_REQUEST_BUDGET = 60


def validate_request(request: object) -> List[str]:
    errors: List[str] = []
    if not isinstance(request, dict):
        return ["request must be an object"]
    expected = {"schema_version", "mode", "source", "target", "budget", "static_only", "provenance"}
    if set(request) != expected:
        errors.append("request keys must be exactly %s" % sorted(expected))
        return errors
    if request["schema_version"] != SCHEMA_VERSION:
        errors.append("schema_version must be " + SCHEMA_VERSION)
    if request["mode"] not in MODES:
        errors.append("mode must be one of %s (there is no live mode)" % (MODES,))
    if request["static_only"] is not True:
        errors.append("static_only must be true; executing repository code is out of scope")
    source = request["source"]
    if not isinstance(source, dict):
        errors.append("source must be an object")
    else:
        kind = source.get("kind")
        if kind not in SUPPORTED_SOURCE_KINDS + UNSUPPORTED_SOURCE_KINDS:
            errors.append("source.kind is unknown: %r" % (kind,))
        _repositories(source.get("repositories"), "source.repositories", errors)
        if kind == "recent-closed-prs":
            source_keys = {"kind", "repositories", "interval", "outcome", "max_per_repository",
                           "request_budget", "reference_repositories"}
            if set(source) != source_keys:
                errors.append("source keys must be exactly %s" % sorted(source_keys))
            else:
                interval = source["interval"]
                if not isinstance(interval, dict) or set(interval) != {"start_at", "end_at"}:
                    errors.append("source.interval must be {start_at, end_at}")
                else:
                    start, end = parse_utc(interval["start_at"]), parse_utc(interval["end_at"])
                    if start is None or end is None:
                        errors.append("source.interval values must be YYYY-MM-DDTHH:MM:SSZ")
                    elif start >= end:
                        errors.append("source.interval must end after it starts")
                if request_outcomes(source["outcome"]) is None:
                    errors.append("source.outcome must be all, merged, closed-unmerged, or the list "
                                  "[\"merged\", \"closed-unmerged\"]")
                _positive_int(source["max_per_repository"], "source.max_per_repository", errors)
                _positive_int(source["request_budget"], "source.request_budget", errors)
                references = source["reference_repositories"]
                if not isinstance(references, list):
                    errors.append("source.reference_repositories must be a list")
                elif references:
                    _repositories(references, "source.reference_repositories", errors)
        elif kind == "curated-tracker":
            _curated_source_errors(source, errors)
    target = request["target"]
    target_keys = {"repositories", "max_candidates_per_repo", "max_candidates_total",
                   "discover_request_budget", "locus_request_budget", "allow_clone"}
    if target is None:
        pass  # collection and analysis only; candidate verification is skipped, so no target caps or budgets exist
    elif not isinstance(target, dict) or set(target) != target_keys:
        errors.append("target keys must be exactly %s" % sorted(target_keys))
    else:
        _repositories(target["repositories"], "target.repositories", errors)
        for key in ("max_candidates_per_repo", "max_candidates_total", "discover_request_budget",
                    "locus_request_budget"):
            _positive_int(target[key], "target." + key, errors)
        for key in ("discover_request_budget", "locus_request_budget"):
            if type(target[key]) is int and target[key] > CANDIDATE_READS_MAX_REQUEST_BUDGET:
                errors.append("target.%s %d is over the candidate-reads entry budget limit %d"
                              % (key, target[key], CANDIDATE_READS_MAX_REQUEST_BUDGET))
        if target["allow_clone"] is not False:
            errors.append("target.allow_clone must be false; clone-based static search is not connected")
    budget = request["budget"]
    if not isinstance(budget, dict) or set(budget) != {"total_api_requests", "max_elapsed_seconds"}:
        errors.append("budget keys must be exactly total_api_requests and max_elapsed_seconds")
    else:
        _positive_int(budget["total_api_requests"], "budget.total_api_requests", errors)
        if budget["max_elapsed_seconds"] is not None:
            _positive_int(budget["max_elapsed_seconds"], "budget.max_elapsed_seconds", errors)
    provenance = request["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"model", "effort"}:
        errors.append("provenance must be {model, effort}; use \"unknown\" when the session does not say")
    else:
        for key in ("model", "effort"):
            if not isinstance(provenance[key], str) or not provenance[key].strip():
                errors.append("provenance.%s must be a non-empty string or \"unknown\"" % key)
    return errors


# ---------------------------------------------------------------------------
# Run directory and state
# ---------------------------------------------------------------------------


class Run:
    def __init__(self, directory: str):
        self.path = Path(os.path.abspath(directory))
        # Current sibling/workflow revisions; set by main, compared with every active checkpoint.
        self.revisions: Optional[Dict[str, str]] = None

    def _check_real(self) -> None:
        if self.path.is_symlink() or not self.path.is_dir():
            raise WorkflowError("run directory must be a real directory: %s" % self.path)
        for name in (RUN_MARKER, REQUEST, STATE):
            path = self.path / name
            if path.is_symlink() or not path.is_file():
                raise WorkflowError("not a workflow run directory (%s missing or symlinked): %s" % (name, self.path))

    def _json(self, name: str) -> Any:
        return read_json_bytes((self.path / name).read_bytes(), name)

    def load(self) -> Tuple[dict, dict]:
        self._check_real()
        request, state, marker = self._json(REQUEST), self._json(STATE), self._json(RUN_MARKER)
        if marker.get("realpath") != os.path.realpath(self.path):
            raise WorkflowError("run directory moved or an ancestor was redirected: %s" % self.path)
        if sha256_file(self.path / REQUEST) != state.get("request_sha256") or marker.get("request_sha256") != state.get("request_sha256"):
            raise WorkflowError("request.json changed after init; start a new run directory for a new scope")
        return request, state

    def write_report(self, text: str) -> Path:
        path = self.path / "report.md"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise WorkflowError("refusing to write through a symlinked or non-file report.md")
        handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(self.path))
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
        return path

    def save(self, state: dict) -> None:
        write_json_atomic(self.path / STATE, state)

    def stage_dir(self, stage: str) -> Path:
        directory = self.path / "stages" / stage
        for part in (self.path / "stages", directory):
            if part.is_symlink():
                raise WorkflowError("refusing symlinked stage directory: %s" % part)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def incoming(self, stage: str, state: dict) -> Path:
        """Reserve the next generation directory; it only becomes active when commit updates state.

        Working in the final location keeps every path written into stage
        outputs (policy content, verifier manifest inputs) valid after commit.
        A failed command removes the directory again.
        """
        number = len(state["stages"][stage]["generations"]) + 1
        path = self.stage_dir(stage) / ("gen-%03d" % number)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError as error:
            raise WorkflowError("uncommitted leftover %s exists (an interrupted command?); inspect and remove it"
                                % path) from error
        return path


def new_state(request_sha: str, run_id: str, resolution: dict, revisions: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "request_sha256": request_sha,
        "created_at": utc_now(),
        "skills": {"resolution": resolution, "revisions": revisions},
        "stages": {stage: {"active": None, "generations": []} for stage in STAGES},
        "ledger": [],
        "events": [],
    }


def active(state: dict, stage: str) -> Optional[dict]:
    number = state["stages"][stage]["active"]
    if number is None:
        return None
    return state["stages"][stage]["generations"][number - 1]


def gen_dir(run: Run, stage: str, generation: dict) -> Path:
    return run.path / "stages" / stage / ("gen-%03d" % generation["generation"])


def integrity_failures(run: Run, state: dict) -> Dict[str, List[str]]:
    """Active checkpoints that can no longer be trusted.

    An output whose bytes changed, a generation path that is a symlink or
    resolves outside the run, and a checkpoint recorded under other sibling or
    workflow revisions all block the stage until it is re-adopted.
    """
    failures: Dict[str, List[str]] = {}
    root = os.path.realpath(run.path)
    for stage in STAGES:
        generation = active(state, stage)
        if generation is None:
            continue
        base = gen_dir(run, stage, generation)
        chain = (run.path / "stages", run.path / "stages" / stage, base)
        if any(part.is_symlink() for part in chain) or os.path.commonpath([root, os.path.realpath(base)]) != root:
            failures.setdefault(stage, []).append("gen-%03d:not-a-real-directory-in-run" % generation["generation"])
            continue
        for relative, digest in generation["outputs"].items():
            path = base / relative
            if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
                failures.setdefault(stage, []).append(relative)
        if run.revisions is not None:
            for key in sorted(set(run.revisions) | set(generation["tool_revisions"])):
                if run.revisions.get(key) != generation["tool_revisions"].get(key):
                    failures.setdefault(stage, []).append("tool-revision-changed:" + key)
    return failures


def usable(run: Run, state: dict, stage: str) -> Tuple[Optional[dict], Optional[str]]:
    generation = active(state, stage)
    if generation is None:
        return None, "%s has no active checkpoint" % stage
    blocked = integrity_failures(run, state).get(stage)
    if blocked:
        return None, "%s checkpoint is blocked (%s); re-adopt it" % (stage, ", ".join(blocked))
    if generation["status"] not in USABLE[stage]:
        return None, "%s checkpoint status is %s" % (stage, generation["status"])
    return generation, None


def event(state: dict, command: str, result: str, detail: str) -> None:
    state["events"].append({"at": utc_now(), "command": command, "result": result, "detail": detail})


def output_hashes(directory: Path) -> Dict[str, str]:
    hashes = {}
    for root, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(root, name)
            if path.is_symlink():
                raise WorkflowError("refusing symlink in stage output: %s" % path)
            hashes[str(path.relative_to(directory))] = sha256_file(path)
    return hashes


def copy_input(source: str, destination: Path, label: str) -> Tuple[bytes, str]:
    """Copy caller input bytes once; the checkpoint hashes exactly those bytes."""
    path = Path(source)
    if not path.is_file():
        raise WorkflowError("%s is not a file: %s" % (label, source))
    data = path.read_bytes()
    destination.write_bytes(data)
    return data, sha256_bytes(data)


def commit(run: Run, state: dict, stage: str, incoming: Path, generation: dict, replace: bool) -> dict:
    """Make a validated incoming directory the next active generation."""
    stage_state = state["stages"][stage]
    previous = active(state, stage)
    guard_replace(state, stage, replace)
    generation["generation"] = len(stage_state["generations"]) + 1
    generation["recorded_at"] = utc_now()
    generation["request_sha256"] = state["request_sha256"]
    generation["outputs"] = output_hashes(incoming)
    if incoming != run.path / "stages" / stage / ("gen-%03d" % generation["generation"]):
        raise WorkflowError("generation directory does not match the reserved checkpoint number")
    if previous is not None:
        previous["superseded_by"] = generation["generation"]
    stage_state["generations"].append(generation)
    stage_state["active"] = generation["generation"]
    for downstream in DOWNSTREAM[stage]:
        old = active(state, downstream)
        if old is not None:
            old["invalidated"] = "%s replaced by gen-%03d" % (stage, generation["generation"])
            state["stages"][downstream]["active"] = None
    return generation


def guard_replace(state: dict, stage: str, replace: bool) -> None:
    """A different input for an adopted stage needs an explicit --replace."""
    previous = active(state, stage)
    if previous is not None and not replace and stage not in ("digest", "record"):
        raise WorkflowError("%s already has gen-%03d; pass --replace to record a different input"
                            % (stage, previous["generation"]))


def reuse_if_same(run: Run, state: dict, stage: str, input_sha: Dict[str, str], revisions: Dict[str, str]) -> Optional[dict]:
    """Return the active generation when inputs and tools are unchanged and intact."""
    generation = active(state, stage)
    if generation is None:
        return None
    if {key: value["sha256"] for key, value in generation["inputs"].items()} != input_sha:
        return None
    if generation["tool_revisions"] != revisions:
        return None
    if stage in integrity_failures(run, state):
        return None
    return generation


def ledger_entry(request: dict, producer: str, command: object, run_id: object, requests: object,
                 started: object, completed: object, identity: dict, cumulative: bool = False) -> dict:
    """One producer-reported run; missing or malformed counts are rejected, never read as zero.

    ``cumulative`` marks a producer whose same-run resume replaces the run record
    with a running total (the curated collector): the identity then leaves out
    the completion time, and a later total supersedes the earlier one instead of
    being added to it.
    """
    if not isinstance(run_id, str) or not run_id:
        raise WorkflowError("%s manifest run has no run_id" % producer)
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 0:
        raise WorkflowError("%s manifest run %s has no valid request count: %r" % (producer, run_id, requests))
    return {"key": canonical_sha256(dict(identity, producer=producer)), "producer": producer, "command": command,
            "run_id": run_id, "requests": requests, "elapsed_seconds": _elapsed(started, completed),
            "simulated": request["mode"] == "offline-fixture", "cumulative": cumulative}


def record_ledger(state: dict, stage: str, entries: List[dict]) -> Tuple[int, int]:
    """Append producer-reported request counts once per stable run identity."""
    known = {entry["key"]: entry for entry in state["ledger"]}
    added = 0
    for entry in entries:
        if entry["key"] in known:
            previous = known[entry["key"]]
            if previous.get("cumulative") and entry["requests"] > previous["requests"]:
                # A resumed run reports its running total: replace, never add.
                previous["requests"], previous["elapsed_seconds"] = entry["requests"], entry["elapsed_seconds"]
                continue
            if previous["requests"] != entry["requests"]:
                raise WorkflowError("conflicting request count for %s run %s: %d != %d" % (
                    entry["producer"], entry["run_id"], known[entry["key"]]["requests"], entry["requests"]))
            continue
        entry = dict(entry)
        entry["stage"] = stage
        state["ledger"].append(entry)
        known[entry["key"]] = entry
        added += 1
    return added, sum(entry["requests"] for entry in state["ledger"])


def budget_violations(request: dict, state: dict) -> List[str]:
    violations = []
    total = sum(entry["requests"] for entry in state["ledger"])
    if total > request["budget"]["total_api_requests"]:
        violations.append("api-budget-exceeded:%d>%d" % (total, request["budget"]["total_api_requests"]))
    limit = request["budget"]["max_elapsed_seconds"]
    if limit is not None:
        unknown = [entry["run_id"] for entry in state["ledger"] if entry["elapsed_seconds"] is None]
        elapsed = sum(entry["elapsed_seconds"] or 0 for entry in state["ledger"])
        if unknown:
            violations.append("time-budget-unverifiable:%d runs without timestamps" % len(unknown))
        if elapsed > limit:
            violations.append("time-budget-exceeded:%d>%d" % (elapsed, limit))
    return violations


def _elapsed(started: object, completed: object) -> Optional[int]:
    start, end = parse_utc(started), parse_utc(completed)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args, siblings: Siblings) -> dict:
    run = Run(args.run_dir)
    data = Path(args.request).read_bytes()
    if run.path.is_symlink():
        raise WorkflowError("run directory must not be a symlink: %s" % run.path)
    request = read_json_bytes(data, "request")
    errors = validate_request(request)
    if errors:
        raise WorkflowError("invalid request: " + "; ".join(errors))
    request_sha = sha256_bytes(data)
    if run.path.exists():
        if (run.path / RUN_MARKER).exists():
            _, state = run.load()
            if state["request_sha256"] != request_sha:
                raise WorkflowError("scope-conflict: %s belongs to request %s; use a new run directory"
                                    % (run.path, state["request_sha256"]))
            return {"run_dir": str(run.path), "run_id": state["run_id"], "result": "existing-run"}
        if run.path.is_symlink() or not run.path.is_dir() or any(run.path.iterdir()):
            raise WorkflowError("run directory exists and is not an empty workflow run: %s" % run.path)
    else:
        run.path.mkdir(parents=True, mode=0o700)
    run_id = "WF-" + request_sha[:12] + "-" + utc_now().replace("-", "").replace(":", "")
    (run.path / REQUEST).write_bytes(data)
    state = new_state(request_sha, run_id, siblings.resolution, siblings.revisions())
    if request["source"]["kind"] in UNSUPPORTED_SOURCE_KINDS:
        event(state, "init", "pending-unsupported",
              "source.kind %s has no connected collector route in this workflow" % request["source"]["kind"])
    run.save(state)
    write_json_atomic(run.path / RUN_MARKER, {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                                              "request_sha256": request_sha, "skill": SKILL_NAME,
                                              "realpath": os.path.realpath(run.path)})
    return {"run_dir": str(run.path), "run_id": run_id, "result": "created"}


def _require_supported(request: dict) -> None:
    if request["source"]["kind"] not in SUPPORTED_SOURCE_KINDS:
        raise WorkflowError("pending-unsupported: source.kind %s is not connected" % request["source"]["kind"])


def _require_target(request: dict, command: str) -> dict:
    """Candidate verification needs a caller-specified target; a null target never borrows a source repository."""
    if request["target"] is None:
        raise WorkflowError("target-unspecified: %s refused: %s" % (command, TARGET_UNSPECIFIED))
    return request["target"]


def request_outcomes(outcome: object) -> Optional[Tuple[str, ...]]:
    """The outcomes a request names: one of all/merged/closed-unmerged, or the list of both kinds.

    ``all`` stays one collector mode (both kinds in one run). The list form is for a
    request whose collections are made per outcome, one run for each kind.
    """
    if isinstance(outcome, str):
        return (outcome,) if outcome in ("all",) + OUTCOME_KINDS else None
    if (isinstance(outcome, list) and len(outcome) == 2 and all(isinstance(item, str) for item in outcome)
            and sorted(outcome) == sorted(OUTCOME_KINDS)):
        return tuple(outcome)
    return None


def outcome_label(outcome: object) -> str:
    return outcome if isinstance(outcome, str) else " + ".join(str(item) for item in outcome)


def format_utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_int(value: object, expected: int) -> bool:
    """Exact integer equality: a JSON boolean never stands in for a number."""
    return type(value) is int and value == expected


def _curated_scope_errors(request: dict, record: dict) -> List[str]:
    """A curated manifest run must match the request; its own content never widens the allow list."""
    source = request["source"]
    tracker = source["tracker"]
    errors = []
    if not isinstance(record.get("run_id"), str) or not record["run_id"]:
        errors.append("manifest run_id must be a non-empty string")
    if record.get("collection_method") != CURATED_METHOD:
        errors.append("manifest collection_method %r is not %s" % (record.get("collection_method"), CURATED_METHOD))
    recorded = record.get("tracker")
    if (not isinstance(recorded, dict) or set(recorded) != {"repository", "issue_number"}
            or recorded["repository"] != tracker["repository"].lower()
            or not _same_int(recorded["issue_number"], tracker["issue_number"])):
        errors.append("manifest tracker %r differs from source.tracker" % (recorded,))
    if not _same_int(record.get("max_prs"), source["max_prs"]):
        errors.append("manifest max_prs differs from source.max_prs")
    if not _same_int(record.get("request_budget"), source["request_budget"]):
        errors.append("manifest request_budget differs from source.request_budget")
    count = record.get("request_count")
    if type(count) is not int or count < 0:
        errors.append("manifest request_count is missing")
    elif count > source["request_budget"]:
        errors.append("manifest request_count exceeds source.request_budget")
    counts = record.get("reference_counts")
    if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
        errors.append("manifest reference_counts must map names to non-negative integers")
    for key in ("hydration_outcomes", "failed_scopes"):
        if not isinstance(record.get(key), list) or not all(isinstance(item, dict) for item in record[key]):
            errors.append("manifest %s must be a list of objects" % key)
    for scope in record.get("failed_scopes") if isinstance(record.get("failed_scopes"), list) else []:
        if isinstance(scope, dict) and "repository" in scope and (
                not isinstance(scope["repository"], str) or type(scope.get("number")) is not int):
            errors.append("manifest failed scope %r needs a repository and an integer number" % (scope.get("kind"),))
    allowed = {repository.lower() for repository in source["repositories"]}
    for outcome in record.get("hydration_outcomes") if isinstance(record.get("hydration_outcomes"), list) else []:
        repository = outcome.get("repository") if isinstance(outcome, dict) else None
        if not isinstance(repository, str) or repository.lower() not in allowed:
            errors.append("manifest hydrated pull request outside source.repositories: %r" % (repository,))
    return errors


def _curated_corpus_errors(request: dict, corpus: object, manifest: dict) -> List[str]:
    """Every corpus record must carry only this tracker's source metadata and runs of this manifest.

    Repository checks alone never prove where a record came from: a valid corpus
    from another tracker next to a matching manifest is refused here.
    """
    tracker = request["source"]["tracker"]
    repository, number = tracker["repository"].lower(), tracker["issue_number"]
    api_url = "https://api.github.com/repos/%s/issues/%d" % (repository, number)
    html_url = "https://github.com/%s/issues/%d" % (repository, number)
    # Identifiers are checked as strings before any set use, so malformed JSON is an error, not a TypeError.
    run_ids = {record["run_id"] for record in manifest["records"] if isinstance(record.get("run_id"), str)}
    records = corpus.get("records") if isinstance(corpus, dict) else None
    if not isinstance(records, list):
        return ["corpus records must be a list"]
    errors, nodes = [], set()
    for index, record in enumerate(records):
        sources = record.get("sources") if isinstance(record, dict) else None
        if not isinstance(sources, list) or not sources:
            errors.append("corpus record %d has no tracker source" % index)
            continue
        for entry in sources:
            if not isinstance(entry, dict) or entry.get("collection_method") != CURATED_METHOD:
                errors.append("corpus record %d has a source that is not %s" % (index, CURATED_METHOD))
                continue
            meta = entry.get("tracker") if isinstance(entry.get("tracker"), dict) else {}
            generator = entry.get("generated_by") if isinstance(entry.get("generated_by"), dict) else {}
            if (not _same_int(meta.get("issue_number"), number) or not isinstance(meta.get("api_url"), str)
                    or meta["api_url"].lower() != api_url or not isinstance(meta.get("html_url"), str)
                    or meta["html_url"].lower() != html_url or generator.get("name") != CURATED_GENERATOR):
                errors.append("corpus record %d names another tracker source: %r" % (index, meta))
                continue
            if not isinstance(meta.get("issue_node_id"), str) or not meta["issue_node_id"]:
                errors.append("corpus record %d tracker issue_node_id must be a non-empty string" % index)
                continue
            nodes.add(meta["issue_node_id"])
            observations = entry.get("observations")
            if not isinstance(observations, list) or not observations:
                errors.append("corpus record %d tracker source has no observations" % index)
                continue
            for observation in observations:
                run_id = observation.get("run_id") if isinstance(observation, dict) else None
                if not isinstance(run_id, str) or run_id not in run_ids:
                    errors.append("corpus record %d observation run %r is not in the manifest" % (index, run_id))
    if len(nodes) > 1:
        errors.append("corpus names more than one tracker Issue node: %s" % sorted(nodes))
    return errors


def _collection_scope_errors(request: dict, record: dict, repositories: Optional[List[str]] = None) -> List[str]:
    """One manifest run against the request; ``repositories`` is the subset one of several collections covers."""
    source = request["source"]
    if source["kind"] == "curated-tracker":
        return _curated_scope_errors(request, record)
    errors = []
    requested = record.get("repositories_requested")
    expected = source["repositories"] if repositories is None else repositories
    if not isinstance(requested, list) or sorted(r.lower() for r in requested) != sorted(r.lower() for r in expected):
        errors.append("manifest repositories_requested %r differ from %s" % (
            requested, "source.repositories" if repositories is None else "the collection's repositories"))
    interval = record.get("interval") if isinstance(record.get("interval"), dict) else {}
    if repositories is None:
        if (interval.get("start_at"), interval.get("end_at")) != (source["interval"]["start_at"], source["interval"]["end_at"]):
            errors.append("manifest interval %r differs from source.interval" % ((interval.get("start_at"), interval.get("end_at")),))
        if record.get("outcome") != source["outcome"]:
            errors.append("manifest outcome differs from source.outcome")
    else:
        # One of several collections: its window lies inside the request's interval (the whole set is
        # checked to tile it), and its outcome is one the request names.
        start, end = parse_utc(interval.get("start_at")), parse_utc(interval.get("end_at"))
        if (start is None or end is None or start >= end or start < parse_utc(source["interval"]["start_at"])
                or end > parse_utc(source["interval"]["end_at"])):
            errors.append("manifest interval %r is outside source.interval" % ((interval.get("start_at"), interval.get("end_at")),))
        if record.get("outcome") not in request_outcomes(source["outcome"]):
            errors.append("manifest outcome %r is not in source.outcome %r" % (record.get("outcome"), source["outcome"]))
    if record.get("max_per_repository") != source["max_per_repository"]:
        errors.append("manifest max_per_repository differs from source.max_per_repository")
    if record.get("request_budget") != source["request_budget"]:
        errors.append("manifest request_budget differs from source.request_budget")
    count = record.get("request_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        errors.append("manifest request_count is missing")
    elif count > source["request_budget"]:
        errors.append("manifest request_count exceeds source.request_budget")
    return errors


def _recent_collection_status(request: dict, latest: dict) -> Tuple[str, List[str], dict, List[str]]:
    reported = {entry.get("repository", "").lower(): entry for entry in latest.get("repositories", []) if isinstance(entry, dict)}
    missing = sorted(r for r in request["source"]["repositories"] if r.lower() not in reported)
    states = {name: entry.get("collection_status") for name, entry in reported.items()}
    status = latest.get("collection_status")
    reasons = []
    if latest.get("checkpoint") is True:
        status, reasons = "partial", ["collector checkpoint record: the run did not finish"]
    if status not in ("complete", "partial", "failed"):
        raise WorkflowError("unknown collection_status %r" % (status,))
    if missing:
        reasons.append("repositories not reported: " + ", ".join(missing))
        status = "partial" if status == "complete" else status
    capped = sorted(name for name, entry in reported.items() if (entry.get("excluded_by_cap") or 0) > 0)
    return status, reasons, states, capped


def _curated_collection_status(latest: dict) -> Tuple[str, List[str], dict, List[str]]:
    """The curated collector's own status; a tracker names no fixed repository set, so none is "missing"."""
    status = latest.get("collection_status")
    if status not in ("complete", "partial", "failed"):
        raise WorkflowError("unknown collection_status %r" % (status,))
    reasons = []
    for scope in latest.get("failed_scopes") or []:
        if isinstance(scope, dict):
            where = "%s#%d" % (scope["repository"], scope["number"]) if "repository" in scope else "tracker"
            reasons.append("%s gap: %s" % (scope.get("kind", "collection"), where))
    states = {}
    for outcome in latest.get("hydration_outcomes") or []:
        if isinstance(outcome, dict):
            states["%s#%s" % (outcome.get("repository"), outcome.get("number"))] = outcome.get("status")
    counts = latest.get("reference_counts") if isinstance(latest.get("reference_counts"), dict) else {}
    capped = []
    if type(counts.get("excluded_by_cap")) is int and counts["excluded_by_cap"] > 0:
        capped = ["%s#%s" % (latest["tracker"]["repository"], latest["tracker"]["issue_number"])]
    return status, reasons, states, capped


def cmd_adopt_collection(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    _require_supported(request)
    curated = request["source"]["kind"] == "curated-tracker"
    corpora, manifests, inventories = args.corpus, args.manifest, args.inventory
    if len(corpora) != len(manifests):
        raise WorkflowError("pair every --corpus with one --manifest (got %d --corpus, %d --manifest)"
                            % (len(corpora), len(manifests)))
    if len(corpora) > 1:
        if curated:
            raise WorkflowError("a curated tracker takes one collection; adopt one --corpus and --manifest")
        if inventories and len(inventories) != len(corpora):
            raise WorkflowError("give --inventory for every collection or for none (got %d for %d collections)"
                                % (len(inventories), len(corpora)))
        return _adopt_collections(args, siblings, request, state, run)
    if len(inventories) > 1:
        raise WorkflowError("--inventory given more than once for one collection")
    corpus_path, manifest_path = corpora[0], manifests[0]
    inventory_path = inventories[0] if inventories else None
    if curated and inventory_path:
        raise WorkflowError("the curated collector writes no inventory; adopt only --corpus and --manifest")
    revisions = siblings.revisions()
    incoming = run.incoming("collect", state)
    try:
        corpus_bytes, corpus_sha = copy_input(corpus_path, incoming / "corpus.json", "corpus")
        manifest_bytes, manifest_sha = copy_input(manifest_path, incoming / "manifest.json", "manifest")
        inputs = {"corpus": {"sha256": corpus_sha, "source_path": os.path.abspath(corpus_path)},
                  "manifest": {"sha256": manifest_sha, "source_path": os.path.abspath(manifest_path)}}
        if inventory_path:
            _, inventory_sha = copy_input(inventory_path, incoming / "inventory.md", "inventory")
            inputs["inventory"] = {"sha256": inventory_sha, "source_path": os.path.abspath(inventory_path)}
        reused = reuse_if_same(run, state, "collect", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            code, _, stderr = run_main(siblings.validate_corpus.main, [str(gen_dir(run, "collect", reused) / "corpus.json")])
            if code != 0:
                raise WorkflowError("reused corpus no longer validates: " + stderr.strip())
            event(state, "adopt-collection", "reused", "gen-%03d revalidated" % reused["generation"])
            return reused
        guard_replace(state, "collect", args.replace)
        corpus = read_json_bytes(corpus_bytes, "corpus")
        manifest = read_json_bytes(manifest_bytes, "manifest")
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0.0" or not isinstance(manifest.get("records"), list) or not manifest["records"]:
            raise WorkflowError("collector manifest must be a v2 manifest with records (migrate v1 with the collector)")
        errors = []
        generator = manifest.get("generated_by") if isinstance(manifest.get("generated_by"), dict) else {}
        if curated and generator.get("name") != CURATED_GENERATOR:
            errors.append("manifest generated_by %r is not %s" % (generator.get("name"), CURATED_GENERATOR))
        if curated:
            try:  # the curated collector's own manifest check, as for its --existing-manifest
                siblings.curated.validate_existing_manifest(manifest, request["source"]["tracker"]["repository"].lower(),
                                                             request["source"]["tracker"]["issue_number"])
            except ValueError as error:
                errors.append("curated manifest check: %s" % error)
        for index, record in enumerate(manifest["records"]):
            if not isinstance(record, dict):
                errors.append("manifest record %d is not an object" % index)
                continue
            errors.extend("record %d: %s" % (index, error) for error in _collection_scope_errors(request, record))
        allowed = {repository.lower() for repository in request["source"]["repositories"]}
        for record in corpus.get("records", []) if isinstance(corpus, dict) else []:
            name = siblings.collector._record_repository_name(record) if isinstance(record, dict) else ""
            if name.lower() not in allowed:
                errors.append("corpus record from out-of-scope repository %r" % name)
        if curated and not errors:
            errors.extend(_curated_corpus_errors(request, corpus, manifest))
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))
        code, stdout, stderr = run_main(siblings.validate_corpus.main, [str(incoming / "corpus.json")])
        if code != 0:
            raise WorkflowError("analyzer validate_corpus rejected the corpus: " + stderr.strip())
        latest = manifest["records"][-1]
        if curated:
            status, reasons, states, capped = _curated_collection_status(latest)
            cumulative, identity_keys = True, ("run_id", "started_at", "request_fingerprint")
        else:
            status, reasons, states, capped = _recent_collection_status(request, latest)
            cumulative, identity_keys = False, ("run_id", "started_at", "completed_at", "request_fingerprint")
        ledger = []
        for record in manifest["records"]:
            identity = {key: record.get(key) for key in identity_keys}
            ledger.append(ledger_entry(request, "collector", "collect", record.get("run_id"), record.get("request_count"),
                                       record.get("started_at"), record.get("completed_at"), identity, cumulative))
        generation = {
            "status": status, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
            "producer": {"generated_by": manifest.get("generated_by"), "run_id": latest.get("run_id")},
            "details": {"records": len(corpus.get("records", [])), "repository_status": states,
                        "capped_repositories": capped, "validator": stdout.strip()},
        }
        added, total = record_ledger(state, "collect", ledger)
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", generation["reasons"] + violations
        commit(run, state, "collect", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-collection", generation["status"], "gen-%03d ledger+%d total=%d" % (generation["generation"], added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


COLLECTION_RANK = {"complete": 0, "partial": 1, "failed": 2}


def _bundle_scope(request: dict, siblings: Siblings, index: int, corpus: object, manifest: object) -> Tuple[List[str], dict]:
    """One of several collections: its manifest runs and corpus records, checked on their own.

    Cross-collection rules (coverage, overlap, duplicates) are checked by the caller.
    """
    label = "collection %d" % index
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0.0"
            or not isinstance(manifest.get("records"), list) or not manifest["records"]):
        raise WorkflowError("%s: collector manifest must be a v2 manifest with records (migrate v1 with the collector)" % label)
    records = manifest["records"]
    errors: List[str] = []
    latest = records[-1]
    requested = latest.get("repositories_requested") if isinstance(latest, dict) else None
    if not isinstance(requested, list) or not requested or not all(isinstance(item, str) for item in requested):
        return [label + ": manifest repositories_requested must list the repositories collected"], {}
    repositories = sorted(requested, key=str.lower)
    lowered = {item.lower() for item in repositories}
    interval = latest.get("interval") if isinstance(latest.get("interval"), dict) else {}
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append("%s: manifest record %d is not an object" % (label, position))
            continue
        errors.extend("%s: record %d: %s" % (label, position, error)
                      for error in _collection_scope_errors(request, record, repositories))
        same_window = record.get("interval") if isinstance(record.get("interval"), dict) else {}
        if (same_window.get("start_at"), same_window.get("end_at")) != (interval.get("start_at"), interval.get("end_at")):
            errors.append("%s: record %d: interval differs from the collection's" % (label, position))
        if record.get("outcome") != latest.get("outcome"):
            errors.append("%s: record %d: outcome differs from the collection's" % (label, position))
    allowed = {item.lower() for item in request["source"]["repositories"]}
    outside = sorted(item for item in repositories if item.lower() not in allowed)
    if outside:
        errors.append("%s: repositories outside source.repositories: %s" % (label, ", ".join(outside)))
    corpus_records = corpus.get("records") if isinstance(corpus, dict) else None
    if not isinstance(corpus_records, list):
        return errors + [label + ": corpus records must be a list"], {}
    counts: Dict[str, int] = {}
    identities = []
    for record in corpus_records:
        if not isinstance(record, dict):
            errors.append("%s: corpus record is not an object" % label)
            continue
        name = siblings.collector._record_repository_name(record)
        if name.lower() not in lowered:
            errors.append("%s: corpus record from out-of-scope repository %r" % (label, name))
        counts[name] = counts.get(name, 0) + 1
        if record.get("analysis_history"):
            errors.append("%s: corpus record carries analysis_history; adopt fresh collections only" % label)
        pull = record.get("pull_request") if isinstance(record.get("pull_request"), dict) else {}
        # A record keeps its own outcome in normalized_state; it must not contradict its collection's.
        wanted = {"merged": ("merged",), "closed-unmerged": ("closed-unmerged",),
                  "all": OUTCOME_KINDS}.get(latest.get("outcome") if isinstance(latest.get("outcome"), str) else "")
        if wanted and pull.get("normalized_state") in OUTCOME_KINDS and pull["normalized_state"] not in wanted:
            errors.append("%s: corpus record state %s does not fit its collection's outcome %s"
                          % (label, pull["normalized_state"], latest["outcome"]))
        identity = record.get("pull_request_node_id") if isinstance(record.get("pull_request_node_id"), str) else pull.get("url")
        if isinstance(identity, str) and identity:
            identities.append(identity)
    run_ids = [item["run_id"] for item in records if isinstance(item, dict) and isinstance(item.get("run_id"), str)]
    return errors, {"index": index, "repositories": repositories, "latest": latest, "records": records,
                    "interval": {"start_at": interval.get("start_at"), "end_at": interval.get("end_at")},
                    "window": (parse_utc(interval.get("start_at")), parse_utc(interval.get("end_at"))),
                    "outcome": latest.get("outcome"),
                    "corpus_records": corpus_records, "records_by_repository": counts, "identities": identities,
                    "run_ids": run_ids}


def _collections_cross_errors(request: dict, bundles: List[dict]) -> List[str]:
    """What only the whole set shows: coverage, overlap, repeated pull requests and runs, a single filter."""
    errors: List[str] = []
    source = request["source"]
    windows: Dict[Tuple[str, str], List[tuple]] = {}
    for bundle in bundles:
        start, end = bundle["window"]
        if start is None or end is None:
            continue  # its own scope check already refused the interval
        for repository in bundle["repositories"]:
            windows.setdefault((repository.lower(), bundle["outcome"]), []).append((start, end, bundle["index"]))
    covered = {repository for repository, _ in windows}
    missing = [item for item in source["repositories"] if item.lower() not in covered]
    if missing:
        errors.append("source.repositories not covered by any collection: " + ", ".join(missing))
    request_start, request_end = parse_utc(source["interval"]["start_at"]), parse_utc(source["interval"]["end_at"])
    for repository in source["repositories"]:
        if repository.lower() not in covered:
            continue
        for outcome in request_outcomes(source["outcome"]):
            spans = sorted(windows.get((repository.lower(), outcome), []))
            if not spans:
                errors.append("repository %s is not covered for outcome %s by any collection" % (repository, outcome))
                continue
            position, last = request_start, None
            for start, end, index in spans:
                if start < position and last is not None:
                    errors.append("repository %s is in more than one collection for outcome %s over overlapping "
                                  "intervals (%d and %d)" % (repository, outcome, last, index))
                elif start > position:
                    errors.append("repository %s outcome %s: interval not covered from %s to %s"
                                  % (repository, outcome, format_utc(position), format_utc(start)))
                if end > position:
                    position, last = end, index
            if position < request_end:
                errors.append("repository %s outcome %s: interval not covered from %s to %s"
                              % (repository, outcome, format_utc(position), format_utc(request_end)))
    for key, noun in (("identities", "pull request"), ("run_ids", "run_id")):
        seen: Dict[str, int] = {}
        for bundle in bundles:
            for value in sorted(set(bundle[key])):
                if value in seen:
                    errors.append("duplicate %s %s is in more than one collection (%d and %d)"
                                  % (noun, value, seen[value], bundle["index"]))
                seen.setdefault(value, bundle["index"])
    filters = sorted({json.dumps(record.get("docs_only")) for bundle in bundles for record in bundle["records"]
                      if isinstance(record, dict)})
    if len(filters) > 1:
        errors.append("manifests disagree on docs_only: " + ", ".join(filters))
    return errors


def _adopt_collections(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    """Adopt several collections of one request as one corpus.

    Each collection is one isolated run over some of ``source.repositories``.
    The collections must cover the whole set, once each, under the request's own
    interval, outcome and caps. Pull request identifiers (``pr_id``) are only
    unique inside one corpus, so the merged corpus renumbers them in order and
    records the mapping in the merged manifest.
    """
    corpora, manifests = args.corpus, args.manifest
    inventories = args.inventory or [None] * len(corpora)
    revisions = siblings.revisions()
    incoming = run.incoming("collect", state)
    try:
        inputs, raw = {}, []
        for index, (corpus_path, manifest_path, inventory_path) in enumerate(zip(corpora, manifests, inventories), 1):
            folder = incoming / "bundles" / ("%02d" % index)
            folder.mkdir(parents=True)
            corpus_bytes, corpus_sha = copy_input(corpus_path, folder / "corpus.json", "corpus %d" % index)
            manifest_bytes, manifest_sha = copy_input(manifest_path, folder / "manifest.json", "manifest %d" % index)
            inputs["corpus-%02d" % index] = {"sha256": corpus_sha, "source_path": os.path.abspath(corpus_path)}
            inputs["manifest-%02d" % index] = {"sha256": manifest_sha, "source_path": os.path.abspath(manifest_path)}
            if inventory_path:
                _, inventory_sha = copy_input(inventory_path, folder / "inventory.md", "inventory %d" % index)
                inputs["inventory-%02d" % index] = {"sha256": inventory_sha, "source_path": os.path.abspath(inventory_path)}
            raw.append((corpus_bytes, manifest_bytes))
        reused = reuse_if_same(run, state, "collect", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            code, _, stderr = run_main(siblings.validate_corpus.main, [str(gen_dir(run, "collect", reused) / "corpus.json")])
            if code != 0:
                raise WorkflowError("reused corpus no longer validates: " + stderr.strip())
            event(state, "adopt-collection", "reused", "gen-%03d revalidated" % reused["generation"])
            return reused
        guard_replace(state, "collect", args.replace)

        bundles, corpus_docs, errors = [], [], []
        for index, (corpus_bytes, manifest_bytes) in enumerate(raw, 1):
            corpus = read_json_bytes(corpus_bytes, "corpus %d" % index)
            manifest = read_json_bytes(manifest_bytes, "manifest %d" % index)
            found, bundle = _bundle_scope(request, siblings, index, corpus, manifest)
            errors.extend(found)
            if bundle:
                bundle["manifest"] = manifest
                bundles.append(bundle)
                corpus_docs.append(corpus)
        if len(bundles) == len(raw):
            errors.extend(_collections_cross_errors(request, bundles))
            for key, label in (("schema_version", "corpus schema_version"), ("generated_by", "corpus generated_by")):
                if len({json.dumps(doc.get(key), sort_keys=True) for doc in corpus_docs}) > 1:
                    errors.append("collections differ in %s" % label)
            if len({json.dumps(b["manifest"].get("generated_by"), sort_keys=True) for b in bundles}) > 1:
                errors.append("collections differ in manifest generated_by")
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))
        for index in range(1, len(bundles) + 1):
            code, _, stderr = run_main(siblings.validate_corpus.main, [str(incoming / "bundles" / ("%02d" % index) / "corpus.json")])
            if code != 0:
                raise WorkflowError("analyzer validate_corpus rejected collection %d corpus: %s" % (index, stderr.strip()))

        merged_records, pr_id_map = [], []
        for bundle in bundles:
            for record in bundle["corpus_records"]:
                record = json.loads(json.dumps(record))
                if record.get("pr_id") not in (None, ""):
                    renumbered = "PR-%03d" % (len(pr_id_map) + 1)
                    pr_id_map.append({"collection": bundle["index"], "from": record["pr_id"], "to": renumbered,
                                      "record_key": record.get("record_key")})
                    record["pr_id"] = renumbered
                merged_records.append(record)
        merged_corpus = dict(corpus_docs[0], records=merged_records)
        write_json_atomic(incoming / "corpus.json", merged_corpus)
        code, stdout, stderr = run_main(siblings.validate_corpus.main, [str(incoming / "corpus.json")])
        if code != 0:
            raise WorkflowError("analyzer validate_corpus rejected the merged corpus: " + stderr.strip())

        states, capped, reasons, collections, ledger = {}, set(), [], [], []
        overall = "complete"
        # Windows and outcomes are shown only when the set is not just one interval and outcome per repository.
        source_interval = request["source"]["interval"]
        periodic = isinstance(request["source"]["outcome"], list) or any(
            (b["interval"]["start_at"], b["interval"]["end_at"]) != (source_interval["start_at"], source_interval["end_at"])
            for b in bundles)
        for bundle in bundles:
            where = "collection %d (%s)" % (bundle["index"], ", ".join(bundle["repositories"]))
            status, why, per_repository, over_cap = _recent_collection_status(
                {"source": {"repositories": bundle["repositories"]}}, bundle["latest"])
            if COLLECTION_RANK[status] > COLLECTION_RANK[overall]:
                overall = status
            if status != "complete" or why:
                reasons.append("%s: %s" % (where, status))
                reasons.extend("%s: %s" % (where, item) for item in why)
            states.update(per_repository)
            capped.update(over_cap)
            for record in bundle["records"]:
                identity = {key: record.get(key) for key in ("run_id", "started_at", "completed_at", "request_fingerprint")}
                ledger.append(ledger_entry(request, "collector", "collect", record.get("run_id"), record.get("request_count"),
                                           record.get("started_at"), record.get("completed_at"), identity, False))
            collections.append({
                "index": bundle["index"], "repositories": bundle["repositories"], "status": status,
                "records": len(bundle["corpus_records"]), "records_by_repository": bundle["records_by_repository"],
                "run_ids": bundle["run_ids"],
                "request_count": sum(item["request_count"] for item in bundle["records"]),
                "request_budget": bundle["latest"]["request_budget"],
            })
            if periodic:
                collections[-1].update(interval=bundle["interval"], outcome=bundle["outcome"])
        merged_manifest = {
            "schema_version": "2.0.0", "generated_by": bundles[0]["manifest"].get("generated_by"),
            "records": [record for bundle in bundles for record in bundle["records"]],
            "workflow_merge": {
                "collections": len(bundles), "bundles": collections, "pr_id_map": pr_id_map,
                "request_count_total": sum(item["request_count"] for item in collections),
                "request_budget_total": sum(item["request_budget"] for item in collections),
            },
        }
        write_json_atomic(incoming / "manifest.json", merged_manifest)
        generation = {
            "status": overall, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
            "producer": {"generated_by": merged_manifest["generated_by"],
                         "run_ids": [item["latest"].get("run_id") for item in bundles]},
            "details": {"records": len(merged_records), "repository_status": states,
                        "capped_repositories": sorted(capped), "validator": stdout.strip(), "collections": collections},
        }
        added, total = record_ledger(state, "collect", ledger)
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", generation["reasons"] + violations
        commit(run, state, "collect", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-collection", generation["status"],
              "gen-%03d from %d collections ledger+%d total=%d" % (generation["generation"], len(bundles), added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def cmd_digest(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    collect, reason = usable(run, state, "collect")
    if collect is None:
        raise WorkflowError("digest needs a usable collection: " + reason)
    revisions = siblings.revisions()
    corpus = gen_dir(run, "collect", collect) / "corpus.json"
    inputs = {"corpus": {"sha256": collect["outputs"]["corpus.json"], "source_path": "stages/collect/gen-%03d/corpus.json" % collect["generation"]}}
    max_bytes = DIGEST_MAX_BYTES if args.max_bytes is None else args.max_bytes
    # The shard size changes the digest, so it is an input: another size is never a reuse.
    inputs["max_bytes"] = {"sha256": sha256_bytes(str(max_bytes).encode("ascii")), "source_path": "--max-bytes %d" % max_bytes}
    reused = reuse_if_same(run, state, "digest", {k: v["sha256"] for k, v in inputs.items()}, revisions)
    if reused is not None:
        event(state, "digest", "reused", "gen-%03d" % reused["generation"])
        return reused
    references = []
    for repository in request["source"]["reference_repositories"]:
        references += ["--reference-repo", repository]
    incoming = run.incoming("digest", state)
    try:
        code, _, stderr = run_main(siblings.build_digest.main, [str(corpus), "--out", str(incoming / "extraction"),
                                                                 "--max-bytes", str(max_bytes)] + references)
        if code != 0:
            raise WorkflowError("build_digest failed: " + stderr.strip())
        code, _, stderr = run_main(siblings.build_prompts.main, ["extract", "--digest-dir", str(incoming / "extraction"), "--out", str(incoming / "extract")])
        if code != 0:
            raise WorkflowError("build_prompts extract failed: " + stderr.strip())
        prompt_files = sorted((incoming / "extract" / "prompts").glob("*.md"))
        prompts = [p.name for p in prompt_files]
        prompt_bytes = {p.name: p.stat().st_size for p in prompt_files}
        largest = max(prompt_bytes.values()) if prompt_bytes else 0
        keymap = read_json_bytes((incoming / "extraction" / "keymap.json").read_bytes(), "digest keymap")
        reasons = []
        if largest > DIGEST_PROMPT_TARGET_BYTES:
            reasons.append("largest lens prompt is %d bytes, over the %d byte target (a worker takes at most 65536)"
                           % (largest, DIGEST_PROMPT_TARGET_BYTES))
        generation = {"status": "complete", "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
                      "details": {"prompts": prompts, "reference_repositories": list(request["source"]["reference_repositories"]),
                                  "max_bytes": max_bytes, "shards": len(keymap["shards"]),
                                  "prompt_bytes": prompt_bytes, "largest_prompt_bytes": largest}}
        commit(run, state, "digest", incoming, generation, True)
        incoming = None
        event(state, "digest", "complete", "gen-%03d prompts=%d" % (generation["generation"], len(prompts)))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def _check_origin(request: dict, origin: str) -> None:
    if origin == "synthetic-fixture" and request["mode"] != "offline-fixture":
        raise WorkflowError("synthetic-fixture origin is accepted only in offline-fixture mode")


def _claimed_provenance(request: dict, args) -> dict:
    return {
        "origin": args.origin,
        "model": args.model or request["provenance"]["model"],
        "effort": args.effort or request["provenance"]["effort"],
        "claimed_by_caller": True,
        "note": "origin/model/effort are caller claims bound to input hashes; they do not prove that a model ran",
    }


def current_patterns(analysis: dict) -> List[str]:
    return [p["pattern_id"] for p in analysis.get("patterns", []) if isinstance(p, dict) and p.get("superseded_by") is None]


def _recorded_analyzer(source: str):
    """Load an earlier analyzer's validator from its own skill files (a caller-chosen trusted root).

    Its revision is computed from those files, never taken from the analysis.
    """
    directory = Path(os.path.abspath(source))
    script = directory / "scripts" / "validate_corpus.py"
    if directory.is_symlink() or script.is_symlink() or not script.is_file():
        raise WorkflowError("--analyzer-source %s has no scripts/validate_corpus.py (give the earlier analyzer's skill "
                            "directory, %s)" % (source, SIBLINGS["analyzer"][0]))
    name = "validate_corpus_recorded_" + sha256_bytes(os.path.realpath(directory).encode("utf-8"))[:16]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, script)
        if spec is None or spec.loader is None:
            raise WorkflowError("cannot load " + str(script))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    try:
        revision = module._canonical_revision()
    except OSError as error:
        raise WorkflowError("--analyzer-source revision could not be computed: %s" % error) from error
    return module, revision, os.path.realpath(directory)


def _validate_analysis(siblings: Siblings, corpus: Path, analysis: Path, references: List[str],
                       recorded=None) -> Tuple[Optional[str], str]:
    """(error, validator output). ``recorded`` is (module, revision) of the analyzer that wrote the analysis.

    Under a recorded revision both checks run unchanged: that revision's own
    validator, with its own revision check, and the current validator's rules
    with the recorded generator as the expected one. Neither is relaxed.
    """
    if recorded is None:
        code, stdout, stderr = run_main(siblings.validate_corpus.main, [str(corpus), "--analysis-output", str(analysis)] + references)
        return (None if code == 0 else stderr.strip()), stdout.strip()
    module, revision = recorded
    code, stdout, stderr = run_main(module.main, [str(corpus), "--analysis-output", str(analysis)] + references)
    if code != 0:
        return "the recorded analyzer's own validator rejected the analysis: " + stderr.strip(), ""
    current = siblings.validate_corpus
    corpus_document = json.loads(corpus.read_text(encoding="utf-8"))
    output = json.loads(analysis.read_text(encoding="utf-8"))
    repositories = current.normalize_reference_repositories(references[1::2])
    errors = current._validate_document(corpus_document, "current") + current._validate_analysis_output(
        corpus_document, output, None, revision, repositories)
    if errors:
        return "the current analyzer rules (under the recorded generator) rejected the analysis: " + "; ".join(errors), ""
    return None, stdout.strip()


def _recorded_revision(document: object) -> Optional[str]:
    generator = document.get("analysis_generated_by") if isinstance(document, dict) else None
    revision = generator.get("revision") if isinstance(generator, dict) else None
    return revision if isinstance(revision, str) else None


def cmd_adopt_analysis(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    _check_origin(request, args.origin)
    collect, reason = usable(run, state, "collect")
    if collect is None:
        raise WorkflowError("analysis needs a usable collection: " + reason)
    revisions = siblings.revisions()
    corpus = gen_dir(run, "collect", collect) / "corpus.json"
    references = []
    for repository in request["source"]["reference_repositories"]:
        references += ["--reference-repo", repository]
    incoming = run.incoming("analysis", state)
    try:
        data, analysis_sha = copy_input(args.analysis, incoming / "analysis.json", "analysis")
        inputs = {"analysis": {"sha256": analysis_sha, "source_path": os.path.abspath(args.analysis)},
                  "corpus": {"sha256": collect["outputs"]["corpus.json"], "source_path": "stages/collect/gen-%03d/corpus.json" % collect["generation"]}}
        results = []
        if args.extraction_result:
            (incoming / "extraction-results").mkdir()
            for index, path in enumerate(args.extraction_result):
                target = incoming / "extraction-results" / ("%02d-%s" % (index, Path(path).name))
                _, digest = copy_input(path, target, "extraction result")
                inputs["extraction_result_%02d" % index] = {"sha256": digest, "source_path": os.path.abspath(path)}
                results.append(str(target))
        analysis = read_json_bytes(data, "analysis")
        recorded_revision, current_revision = _recorded_revision(analysis), revisions["analyzer"]
        recorded = drift = None
        if args.analyzer_source:
            # An analysis written by an earlier analyzer revision is adopted under that revision (#266).
            if recorded_revision == current_revision:
                raise WorkflowError("the analysis names the current analyzer revision; adopt it without --analyzer-source")
            module, source_revision, source_path = _recorded_analyzer(args.analyzer_source)
            if source_revision != recorded_revision:
                raise WorkflowError("--analyzer-source is analyzer revision %s, not the recorded %s"
                                    % (source_revision, recorded_revision))
            recorded = (module, source_revision)
            drift = {"recorded": source_revision, "current": current_revision, "source": source_path}
            inputs["analyzer_source"] = {"sha256": sha256_bytes(source_revision.encode("utf-8")), "source_path": source_path}
        reused = reuse_if_same(run, state, "analysis", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None and reused["provenance"] == _claimed_provenance(request, args):
            error, _ = _validate_analysis(siblings, corpus, gen_dir(run, "analysis", reused) / "analysis.json", references, recorded)
            if error is not None:
                raise WorkflowError("reused analysis no longer validates: " + error)
            event(state, "adopt-analysis", "reused", "gen-%03d revalidated%s" % (
                reused["generation"], " under the recorded analyzer revision" if recorded else ""))
            return reused
        guard_replace(state, "analysis", args.replace)
        if results:
            code, _, stderr = run_main(siblings.check_extraction.main, [str(corpus)] + results + references)
            if code != 0:
                raise WorkflowError("check_extraction rejected the extraction results: " + stderr.strip())
        error, stdout = _validate_analysis(siblings, corpus, incoming / "analysis.json", references, recorded)
        if error is not None and recorded is None:
            if recorded_revision is not None and recorded_revision != current_revision:
                error += ("; the analysis records analyzer revision %s, not the current %s: adopt it under its recorded "
                          "revision with --analyzer-source DIR (that revision's %s skill files), or regenerate the "
                          "analysis with the current analyzer" % (recorded_revision, current_revision, SIBLINGS["analyzer"][0]))
            raise WorkflowError("analyzer output validator rejected the analysis: " + error)
        if error is not None:
            raise WorkflowError(error)
        patterns = current_patterns(analysis)
        reasons = [] if patterns else ["the validated analysis has no current PAT-* pattern"]
        if drift:
            reasons.append("analyzer revision drift: adopted under recorded %s, current %s" % (drift["recorded"], drift["current"]))
        generation = {
            "status": "complete" if patterns else "no-patterns",
            "reasons": reasons,
            "inputs": inputs, "tool_revisions": revisions, "provenance": _claimed_provenance(request, args),
            "details": {"patterns": patterns, "validator": stdout,
                        "extraction_results_checked": len(results)},
        }
        if drift:
            generation["details"]["analyzer_revision_drift"] = drift
        commit(run, state, "analysis", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-analysis", generation["status"], "gen-%03d patterns=%d" % (generation["generation"], len(patterns)))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def _verifier_ledger(request: dict, manifest: dict) -> List[dict]:
    entries = []
    if not isinstance(manifest, dict) or not isinstance(manifest.get("runs"), list):
        raise WorkflowError("verifier manifest has no runs list")
    for run_entry in manifest["runs"]:
        if not isinstance(run_entry, dict):
            raise WorkflowError("verifier manifest run is not an object")
        budget = run_entry.get("budget") if isinstance(run_entry.get("budget"), dict) else {}
        identity = {"command": run_entry.get("command"), "run_id": run_entry.get("run_id"),
                    "started_at": run_entry.get("started_at"), "completed_at": run_entry.get("completed_at"),
                    "inputs": run_entry.get("inputs")}
        entries.append(ledger_entry(request, "verifier", run_entry.get("command"), run_entry.get("run_id"),
                                    budget.get("requests_consumed"), run_entry.get("started_at"),
                                    run_entry.get("completed_at"), identity))
    return entries


def _manifest_run(manifest: dict, command: str, match) -> Optional[dict]:
    runs = [r for r in manifest.get("runs", []) if isinstance(r, dict) and r.get("command") == command and match(r)]
    return runs[-1] if runs else None


def cmd_adopt_discovery(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    target = _require_target(request, "adopt-discovery")
    analysis, reason = usable(run, state, "analysis")
    if analysis is None:
        raise WorkflowError("discovery needs a usable analysis: " + reason)
    if len(args.discovery) != len(args.manifest):
        raise WorkflowError("pair every --discovery with one --manifest (got %d --discovery, %d --manifest)"
                            % (len(args.discovery), len(args.manifest)))
    if len(args.discovery) > 1:
        return _adopt_discoveries(args, siblings, request, state, run, target, analysis)
    discovery_path, manifest_path = args.discovery[0], args.manifest[0]
    revisions = siblings.revisions()
    incoming = run.incoming("discovery", state)
    try:
        data, discovery_sha = copy_input(discovery_path, incoming / "discovery.json", "discovery")
        manifest_bytes, manifest_sha = copy_input(manifest_path, incoming / "manifest.json", "verifier manifest")
        inputs = {"discovery": {"sha256": discovery_sha, "source_path": os.path.abspath(discovery_path)},
                  "manifest": {"sha256": manifest_sha, "source_path": os.path.abspath(manifest_path)}}
        reused = reuse_if_same(run, state, "discovery", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            event(state, "adopt-discovery", "reused", "gen-%03d" % reused["generation"])
            return reused
        guard_replace(state, "discovery", args.replace)
        discovery = read_json_bytes(data, "discovery")
        manifest = read_json_bytes(manifest_bytes, "verifier manifest")
        if not isinstance(discovery, dict) or not isinstance(discovery.get("inputs"), dict):
            raise WorkflowError("discovery document has no inputs")
        declared = discovery["inputs"]
        errors = []
        if declared.get("analysis_sha256") != analysis["outputs"]["analysis.json"]:
            errors.append("discovery.inputs.analysis_sha256 is not the active analysis checkpoint")
        repositories = declared.get("repositories") if isinstance(declared.get("repositories"), list) else []
        wanted = {r.lower() for r in target["repositories"]}
        extra = sorted(r for r in repositories if r.lower() not in wanted)
        if extra:
            errors.append("discovery covers repositories outside target.repositories: " + ", ".join(extra))
        if declared.get("request_budget") != target["discover_request_budget"]:
            errors.append("discovery request_budget differs from target.discover_request_budget")
        if declared.get("allow_clone") is not target["allow_clone"]:
            errors.append("discovery allow_clone differs from target.allow_clone")
        run_entry = _manifest_run(manifest if isinstance(manifest, dict) else {}, "discover", lambda r: r.get("inputs") == declared)
        if run_entry is None:
            errors.append("verifier manifest has no discover run with these inputs")
        else:
            budget = run_entry.get("budget") or {}
            if (budget.get("per_repo_cap"), budget.get("total_cap")) != (target["max_candidates_per_repo"], target["max_candidates_total"]):
                errors.append("discover caps differ from target caps")
        patterns = declared.get("pattern_ids") if isinstance(declared.get("pattern_ids"), list) else []
        current = current_patterns(json.loads((gen_dir(run, "analysis", analysis) / "analysis.json").read_text(encoding="utf-8")))
        unknown = sorted(set(patterns) - set(current))
        if unknown:
            errors.append("discovery names patterns absent from the analysis: " + ", ".join(unknown))
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))
        problems = siblings.verifier.validate_analysis_envelope(json.loads((gen_dir(run, "analysis", analysis) / "analysis.json").read_text(encoding="utf-8")))
        if problems:
            raise WorkflowError("verifier rejects the analysis envelope: " + "; ".join(problems))
        universe = {(r.lower(), p) for r in repositories for p in patterns}
        seen: Dict[Tuple[str, str], int] = {}
        for group in ("records", "skipped_by_cap", "failed_scopes"):
            for item in discovery.get(group, []):
                key = (str(item.get("repository", "")).lower(), item.get("pattern_id"))
                seen[key] = seen.get(key, 0) + 1
        if set(seen) != universe or any(count != 1 for count in seen.values()):
            raise WorkflowError("discovery partition does not cover every repository-pattern combination exactly once")
        status = discovery.get("status")
        if status not in ("complete", "partial", "failed"):
            raise WorkflowError("unknown discovery status %r" % (status,))
        reasons = []
        missing_repositories = sorted(r for r in target["repositories"] if r.lower() not in {x.lower() for x in repositories})
        missing_patterns = sorted(set(current) - set(patterns))
        if missing_repositories:
            reasons.append("target repositories not discovered: " + ", ".join(missing_repositories))
        if missing_patterns:
            reasons.append("patterns not discovered: " + ", ".join(missing_patterns))
        if discovery.get("skipped_by_cap"):
            reasons.append("skipped by cap: %d combinations" % len(discovery["skipped_by_cap"]))
        if discovery.get("failed_scopes"):
            reasons.append("failed scopes: %d combinations" % len(discovery["failed_scopes"]))
        if status == "complete" and reasons:
            status = "partial"
        if status != "failed" and not discovery.get("records"):
            status, reasons = "no-candidates", reasons + ["discovery produced no repository-pattern record"]
        generation = {"status": status, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
                      "details": {"records": len(discovery.get("records", [])),
                                  "skipped_by_cap": len(discovery.get("skipped_by_cap", [])),
                                  "failed_scopes": len(discovery.get("failed_scopes", []))}}
        added, total = record_ledger(state, "discovery", _verifier_ledger(request, manifest))
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", reasons + violations
        commit(run, state, "discovery", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-discovery", generation["status"], "gen-%03d ledger+%d total=%d" % (generation["generation"], added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def cmd_adopt_locus(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    _require_target(request, "adopt-locus-search")
    discovery, reason = usable(run, state, "discovery")
    if discovery is None:
        raise WorkflowError("locus-search needs a usable discovery: " + reason)
    if len(args.locus_search) != len(args.manifest):
        raise WorkflowError("pair every --locus-search with one --manifest (got %d --locus-search, %d --manifest)"
                            % (len(args.locus_search), len(args.manifest)))
    if discovery["details"].get("discoveries"):
        return _adopt_locus_searches(args, siblings, request, state, run, discovery)
    if len(args.locus_search) > 1:
        raise WorkflowError("the active discovery is one document; adopt one --locus-search and --manifest")
    locus_path, manifest_path = args.locus_search[0], args.manifest[0]
    revisions = siblings.revisions()
    incoming = run.incoming("locus-search", state)
    try:
        data, locus_sha = copy_input(locus_path, incoming / "locus-search.json", "locus search")
        manifest_bytes, manifest_sha = copy_input(manifest_path, incoming / "manifest.json", "verifier manifest")
        inputs = {"locus_search": {"sha256": locus_sha, "source_path": os.path.abspath(locus_path)},
                  "manifest": {"sha256": manifest_sha, "source_path": os.path.abspath(manifest_path)}}
        reused = reuse_if_same(run, state, "locus-search", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            event(state, "adopt-locus-search", "reused", "gen-%03d" % reused["generation"])
            return reused
        guard_replace(state, "locus-search", args.replace)
        document = read_json_bytes(data, "locus search")
        manifest = read_json_bytes(manifest_bytes, "verifier manifest")
        discovery_sha = discovery["outputs"]["discovery.json"]
        if not isinstance(document, dict) or document.get("discovery_sha256") != discovery_sha:
            raise WorkflowError("scope-conflict: locus search is not bound to the active discovery checkpoint")
        run_entry = _manifest_run(manifest if isinstance(manifest, dict) else {}, "locus-search",
                                  lambda r: isinstance(r.get("inputs"), dict) and r["inputs"].get("discovery_sha256") == discovery_sha)
        if run_entry is None:
            raise WorkflowError("verifier manifest has no locus-search run for the active discovery")
        if (run_entry.get("budget") or {}).get("request_limit") != request["target"]["locus_request_budget"]:
            raise WorkflowError("scope-conflict: locus-search request budget differs from target.locus_request_budget")
        wanted = {r.lower() for r in request["target"]["repositories"]}
        outside = sorted({s.get("repository") for s in document.get("searches", []) if str(s.get("repository", "")).lower() not in wanted})
        if outside:
            raise WorkflowError("scope-conflict: locus search covers repositories outside the target: %s" % outside)
        status = document.get("status")
        if status not in ("complete", "partial"):
            raise WorkflowError("unsupported locus-search status %r" % (status,))
        incomplete = [s for s in document.get("searches", []) if not s.get("complete")]
        reasons = ["incomplete locus searches: %d" % len(incomplete)] if incomplete else []
        generation = {"status": "partial" if incomplete else status, "reasons": reasons, "inputs": inputs,
                      "tool_revisions": revisions,
                      "details": {"searches": len(document.get("searches", [])),
                                  "assessment_sha256": run_entry["inputs"].get("assessment_sha256")}}
        added, total = record_ledger(state, "locus-search", _verifier_ledger(request, manifest))
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", reasons + violations
        commit(run, state, "locus-search", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-locus-search", generation["status"], "gen-%03d ledger+%d total=%d" % (generation["generation"], added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


# ---------------------------------------------------------------------------
# Several target repositories: one candidate-reads run per repository
# ---------------------------------------------------------------------------


def _copy_bundles(incoming: Path, columns: Sequence[Tuple[str, str, Sequence[str]]]) -> Tuple[dict, List[Dict[str, bytes]]]:
    """Copy repeated inputs into ``bundles/NN/`` in argument order; their checkpoint keys are ``<key>-NN``."""
    inputs: dict = {}
    raw: List[Dict[str, bytes]] = []
    for index in range(1, len(columns[0][2]) + 1):
        folder = incoming / "bundles" / ("%02d" % index)
        folder.mkdir(parents=True)
        item = {}
        for key, filename, paths in columns:
            source = paths[index - 1]
            data, digest = copy_input(source, folder / filename, "%s %d" % (key.replace("-", " "), index))
            inputs["%s-%02d" % (key, index)] = {"sha256": digest, "source_path": os.path.abspath(source)}
            item[key] = data
        raw.append(item)
    return inputs, raw


def _int_or_none(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _discovery_bundle(siblings: Siblings, target: dict, analysis_sha: str, current: List[str], index: int,
                      sha: str, discovery: object, manifest: object) -> Tuple[List[str], dict]:
    """One repository's discovery, checked on its own; the caller checks the whole set."""
    label = "discovery %d" % index
    if not isinstance(discovery, dict) or not isinstance(discovery.get("inputs"), dict):
        return [label + ": discovery document has no inputs"], {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("runs"), list):
        return [label + ": verifier manifest has no runs list"], {}
    try:
        siblings.verifier._validate_discovery_document(discovery)
    except ValueError as error:
        return ["%s: verifier rejects the discovery document: %s" % (label, error)], {}
    declared = discovery["inputs"]
    repositories = declared["repositories"]
    if len(repositories) != 1 or not isinstance(repositories[0], str):
        return ["%s must name exactly one repository (one candidate-reads run), not %s"
                % (label, json.dumps(repositories))], {}
    repository = repositories[0]
    errors = []
    if repository.lower() not in {item.lower() for item in target["repositories"]}:
        errors.append("%s covers a repository outside target.repositories: %s" % (label, repository))
    if declared.get("analysis_sha256") != analysis_sha:
        errors.append(label + ": inputs.analysis_sha256 is not the active analysis checkpoint")
    if declared.get("request_budget") != target["discover_request_budget"]:
        errors.append("%s: request_budget %s differs from target.discover_request_budget %d"
                      % (label, json.dumps(declared.get("request_budget")), target["discover_request_budget"]))
    if declared.get("allow_clone") is not target["allow_clone"]:
        errors.append(label + ": allow_clone differs from target.allow_clone")
    patterns = declared.get("pattern_ids")
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        errors.append(label + ": inputs.pattern_ids must be a list of pattern ids")
        patterns = []
    unknown = sorted(set(patterns) - set(current))
    if unknown:
        errors.append("%s names patterns absent from the analysis: %s" % (label, ", ".join(unknown)))
    run_entry = _manifest_run(manifest, "discover", lambda r: r.get("inputs") == declared)
    if run_entry is None:
        errors.append(label + ": verifier manifest has no discover run with these inputs")
        run_entry = {}
    budget = run_entry.get("budget") if isinstance(run_entry.get("budget"), dict) else {}
    if run_entry and (budget.get("per_repo_cap"), budget.get("total_cap")) != (target["max_candidates_per_repo"], target["max_candidates_total"]):
        errors.append(label + ": discover caps differ from target caps")
    if run_entry and _int_or_none(budget.get("request_limit")) is None:
        errors.append(label + ": verifier manifest discover run has no request_limit")
    seen: Dict[Tuple[str, str], int] = {}
    for group in ("records", "skipped_by_cap", "failed_scopes"):
        for item in discovery[group]:
            key = (item["repository"].lower(), item["pattern_id"])
            seen[key] = seen.get(key, 0) + 1
    if set(seen) != {(repository.lower(), pattern) for pattern in patterns} or any(count != 1 for count in seen.values()):
        errors.append(label + ": partition does not cover every repository-pattern combination exactly once")
    return errors, {"index": index, "sha256": sha, "repository": repository, "declared": declared,
                    "patterns": list(patterns), "document": discovery, "manifest": manifest, "run": run_entry,
                    "budget": budget}


def _discoveries_cross_errors(request: dict, target: dict, bundles: List[dict]) -> List[str]:
    """What only the whole set shows: the target set, one shared scope, one verifier, the summed per-run budgets."""
    errors: List[str] = []
    seen: Dict[str, int] = {}
    for bundle in bundles:
        key = bundle["repository"].lower()
        if key in seen:
            errors.append("repository %s is in more than one discovery (%d and %d)" % (bundle["repository"], seen[key], bundle["index"]))
        seen.setdefault(key, bundle["index"])
    missing = [item for item in target["repositories"] if item.lower() not in seen]
    if missing:
        errors.append("target repositories without a discovery: " + ", ".join(missing))
    keys = sorted({key for bundle in bundles for key in bundle["declared"]} - {"repositories"})
    differing = [key for key in keys if len({json.dumps(bundle["declared"].get(key), sort_keys=True) for bundle in bundles}) > 1]
    if differing:
        errors.append("discoveries differ in inputs other than the repository: " + ", ".join(differing))
    for label, part in (("discovery", "document"), ("verifier manifest", "manifest")):
        if len({json.dumps(bundle[part].get("generated_by"), sort_keys=True) for bundle in bundles}) > 1:
            errors.append("discoveries differ in verifier revision (%s generated_by)" % label)
    limits = sum(_int_or_none(bundle["budget"].get("request_limit")) or 0 for bundle in bundles)
    if limits > request["budget"]["total_api_requests"]:
        errors.append("per-run discover request budgets sum to %d, over budget.total_api_requests %d"
                      % (limits, request["budget"]["total_api_requests"]))
    return errors


def _merge_discoveries(siblings: Siblings, target: dict, bundles: List[dict]) -> Tuple[dict, List[dict], List[dict]]:
    """One discovery document over the target, in target order, with the target caps applied once more.

    Each repository's run applied the caps to itself alone, so the set can hold
    more combinations than ``max_candidates_total``. The verifier's own cap
    function picks the combinations one discover run over the target (in target
    order) would have kept; the others move to ``skipped_by_cap``, and the
    verifier's partition check must accept the result.
    """
    position = {item.lower(): number for number, item in enumerate(target["repositories"])}
    ordered = sorted(bundles, key=lambda bundle: position[bundle["repository"].lower()])
    patterns = ordered[0]["patterns"]
    universe = [(bundle["repository"], pattern) for bundle in ordered for pattern in patterns]
    selected, skipped = siblings.verifier._cap_combinations(
        [bundle["repository"] for bundle in ordered], [{"pattern_id": pattern} for pattern in patterns],
        target["max_candidates_per_repo"], target["max_candidates_total"])
    kept = {(repository, str(pattern["pattern_id"])) for repository, pattern in selected}
    records: List[dict] = []
    failed: List[dict] = []
    capped: List[dict] = []
    for bundle in ordered:
        for section, into in (("records", records), ("failed_scopes", failed)):
            for item in bundle["document"][section]:
                if (item["repository"], item["pattern_id"]) in kept:
                    into.append(item)
                else:
                    capped.append({"discovery": bundle["index"], "repository": item["repository"],
                                   "pattern_id": item["pattern_id"], "section": section})
    try:
        siblings.verifier._validate_discover_partition(universe, selected, records, skipped, failed)
    except ValueError as error:
        raise WorkflowError("scope-conflict: the merged discovery breaks the verifier's partition under the target "
                            "caps: %s" % error) from error
    limitations: List[str] = []
    for bundle in ordered:
        for item in bundle["document"]["method_limitations"]:
            if item not in limitations:
                limitations.append(item)
    summary = [{"index": bundle["index"], "repository": bundle["repository"], "sha256": bundle["sha256"],
                "status": bundle["document"]["status"], "records": len(bundle["document"]["records"]),
                "skipped_by_cap": len(bundle["document"]["skipped_by_cap"]),
                "failed_scopes": len(bundle["document"]["failed_scopes"]), "run_id": bundle["run"].get("run_id"),
                "request_budget": bundle["budget"].get("request_limit"),
                "requests_consumed": bundle["budget"].get("requests_consumed")} for bundle in ordered]
    merged = {
        "schema_version": "1.0.0", "generated_by": ordered[0]["document"]["generated_by"],
        "inputs": {key: ([bundle["repository"] for bundle in ordered] if key == "repositories" else value)
                   for key, value in ordered[0]["declared"].items()},
        "records": records, "skipped_by_cap": skipped, "failed_scopes": failed, "method_limitations": limitations,
        "status": max((bundle["document"]["status"] for bundle in ordered), key=COLLECTION_RANK.__getitem__),
        "workflow_merge": {"merged_by": SKILL_NAME, "discoveries": summary, "capped": capped},
    }
    try:
        siblings.verifier._validate_discovery_document(merged)
    except ValueError as error:
        raise WorkflowError("verifier rejects the merged discovery document: %s" % error) from error
    return merged, summary, capped


def _adopt_discoveries(args, siblings: Siblings, request: dict, state: dict, run: Run, target: dict, analysis: dict) -> dict:
    """Adopt one discovery per target repository as one merged discovery checkpoint.

    The candidate-reads entry reads one repository per run. The originals stay
    byte for byte under ``bundles/NN/``; ``discovery.json`` is the merge over
    the target. Assessment, locus search and verifier record then run per
    repository on the originals, and ``record`` merges their candidates.
    """
    revisions = siblings.revisions()
    incoming = run.incoming("discovery", state)
    try:
        inputs, raw = _copy_bundles(incoming, [("discovery", "discovery.json", args.discovery),
                                               ("manifest", "manifest.json", args.manifest)])
        reused = reuse_if_same(run, state, "discovery", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            event(state, "adopt-discovery", "reused", "gen-%03d" % reused["generation"])
            return reused
        guard_replace(state, "discovery", args.replace)
        analysis_document = json.loads((gen_dir(run, "analysis", analysis) / "analysis.json").read_text(encoding="utf-8"))
        problems = siblings.verifier.validate_analysis_envelope(analysis_document)
        if problems:
            raise WorkflowError("verifier rejects the analysis envelope: " + "; ".join(problems))
        current = current_patterns(analysis_document)
        bundles, errors = [], []
        for index, item in enumerate(raw, 1):
            found, bundle = _discovery_bundle(
                siblings, target, analysis["outputs"]["analysis.json"], current, index,
                inputs["discovery-%02d" % index]["sha256"], read_json_bytes(item["discovery"], "discovery %d" % index),
                read_json_bytes(item["manifest"], "verifier manifest %d" % index))
            errors.extend(found)
            if bundle:
                bundles.append(bundle)
        if len(bundles) == len(raw):
            errors.extend(_discoveries_cross_errors(request, target, bundles))
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))
        merged, summary, capped = _merge_discoveries(siblings, target, bundles)
        write_json_atomic(incoming / "discovery.json", merged)
        reasons = ["discovery %d (%s): %s" % (item["index"], item["repository"], item["status"])
                   for item in summary if item["status"] != "complete"]
        missing_patterns = sorted(set(current) - set(bundles[0]["patterns"]))
        if missing_patterns:
            reasons.append("patterns not discovered: " + ", ".join(missing_patterns))
        if merged["skipped_by_cap"]:
            reasons.append("skipped by cap: %d combinations" % len(merged["skipped_by_cap"]))
        if capped:
            reasons.append("merged caps moved %d combinations to skipped_by_cap" % len(capped))
        if merged["failed_scopes"]:
            reasons.append("failed scopes: %d combinations" % len(merged["failed_scopes"]))
        status = merged["status"]
        if status == "complete" and reasons:
            status = "partial"
        if status != "failed" and not merged["records"]:
            status, reasons = "no-candidates", reasons + ["discovery produced no repository-pattern record"]
        generation = {"status": status, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
                      "details": {"records": len(merged["records"]), "skipped_by_cap": len(merged["skipped_by_cap"]),
                                  "failed_scopes": len(merged["failed_scopes"]), "discoveries": summary,
                                  "capped": capped}}
        ledger = [entry for bundle in bundles for entry in _verifier_ledger(request, bundle["manifest"])]
        added, total = record_ledger(state, "discovery", ledger)
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", reasons + violations
        commit(run, state, "discovery", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-discovery", generation["status"], "gen-%03d from %d discoveries ledger+%d total=%d"
              % (generation["generation"], len(bundles), added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def _adopt_locus_searches(args, siblings: Siblings, request: dict, state: dict, run: Run, discovery: dict) -> dict:
    """One locus search per adopted discovery, each bound to that repository's original discovery bytes."""
    target = request["target"]
    discoveries = discovery["details"]["discoveries"]
    if len(args.locus_search) == 1:
        raise WorkflowError("the active discovery merges %d discoveries; adopt one --locus-search per discovery, "
                            "repeated with its --manifest" % len(discoveries))
    revisions = siblings.revisions()
    incoming = run.incoming("locus-search", state)
    try:
        inputs, raw = _copy_bundles(incoming, [("locus-search", "locus-search.json", args.locus_search),
                                               ("manifest", "manifest.json", args.manifest)])
        reused = reuse_if_same(run, state, "locus-search", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None:
            event(state, "adopt-locus-search", "reused", "gen-%03d" % reused["generation"])
            return reused
        guard_replace(state, "locus-search", args.replace)
        by_sha = {item["sha256"]: item for item in discoveries}
        capped = {(item["repository"], item["pattern_id"]) for item in discovery["details"]["capped"]}
        errors: List[str] = []
        searched: Dict[int, int] = {}
        summary, manifests, generators, limits = [], [], set(), 0
        for index, item in enumerate(raw, 1):
            label = "locus search %d" % index
            document = read_json_bytes(item["locus-search"], label)
            manifest = read_json_bytes(item["manifest"], "verifier manifest %d" % index)
            bound = by_sha.get(document.get("discovery_sha256")) if isinstance(document, dict) else None
            if bound is None:
                errors.append(label + " is not bound to an adopted discovery")
                continue
            if bound["index"] in searched:
                errors.append("%s is bound to discovery %d, already searched by locus search %d"
                              % (label, bound["index"], searched[bound["index"]]))
                continue
            searched[bound["index"]] = index
            try:  # the verifier's own locus-search reader, as record uses it
                siblings.verifier._read_locus_search(str(incoming / "bundles" / ("%02d" % index) / "locus-search.json"),
                                                     bound["sha256"])
            except ValueError as error:
                errors.append("%s: verifier rejects the locus search: %s" % (label, error))
                continue
            if not isinstance(manifest, dict) or not isinstance(manifest.get("runs"), list):
                errors.append(label + ": verifier manifest has no runs list")
                continue
            run_entry = _manifest_run(manifest, "locus-search", lambda r: isinstance(r.get("inputs"), dict)
                                      and r["inputs"].get("discovery_sha256") == bound["sha256"])
            if run_entry is None:
                errors.append("%s: verifier manifest has no locus-search run for discovery %d" % (label, bound["index"]))
                continue
            budget = run_entry.get("budget") if isinstance(run_entry.get("budget"), dict) else {}
            if budget.get("request_limit") != target["locus_request_budget"]:
                errors.append("%s: request budget %s differs from target.locus_request_budget %d"
                              % (label, json.dumps(budget.get("request_limit")), target["locus_request_budget"]))
            else:
                limits += budget["request_limit"]
            searches = document["searches"]
            outside = sorted({s["repository"] for s in searches if s["repository"].lower() != bound["repository"].lower()})
            if outside:
                errors.append("%s covers repositories other than its discovery's %s: %s"
                              % (label, bound["repository"], ", ".join(outside)))
            over = sorted({"%s %s" % (s["repository"], s.get("pattern_id")) for s in searches
                           if (s["repository"], s.get("pattern_id")) in capped})
            if over:
                errors.append("%s searches combinations the merged caps skipped: %s" % (label, ", ".join(over)))
            if document.get("status") not in ("complete", "partial"):
                errors.append("%s: unsupported locus-search status %r" % (label, document.get("status")))
            generators.add(json.dumps(document.get("generated_by"), sort_keys=True))
            incomplete = sum(1 for s in searches if not s.get("complete"))
            manifests.append(manifest)
            summary.append({"index": index, "discovery": bound["index"], "repository": bound["repository"],
                            "discovery_sha256": bound["sha256"],
                            "locus_search_sha256": inputs["locus-search-%02d" % index]["sha256"],
                            "assessment_sha256": run_entry["inputs"].get("assessment_sha256"),
                            "searches": len(searches), "incomplete": incomplete,
                            "status": "partial" if incomplete else document.get("status"),
                            "request_budget": budget.get("request_limit"),
                            "requests_consumed": budget.get("requests_consumed")})
        errors.extend("no locus search for discovery %d (%s)" % (item["index"], item["repository"])
                      for item in discoveries if item["index"] not in searched)
        if len(generators) > 1:
            errors.append("locus searches differ in verifier revision (generated_by)")
        if limits > request["budget"]["total_api_requests"]:
            errors.append("per-run locus-search request budgets sum to %d, over budget.total_api_requests %d"
                          % (limits, request["budget"]["total_api_requests"]))
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))
        order = {item["index"]: number for number, item in enumerate(discoveries)}
        summary.sort(key=lambda item: order[item["discovery"]])
        reasons = ["locus search %d (%s): partial" % (item["index"], item["repository"])
                   for item in summary if item["status"] != "complete"]
        incomplete = sum(item["incomplete"] for item in summary)
        if incomplete:
            reasons.append("incomplete locus searches: %d" % incomplete)
        generation = {"status": "partial" if reasons else "complete", "reasons": reasons, "inputs": inputs,
                      "tool_revisions": revisions,
                      "details": {"searches": sum(item["searches"] for item in summary), "locus_searches": summary}}
        ledger = [entry for manifest in manifests for entry in _verifier_ledger(request, manifest)]
        added, total = record_ledger(state, "locus-search", ledger)
        violations = budget_violations(request, state)
        if violations:
            generation["status"], generation["reasons"] = "blocked", reasons + violations
        commit(run, state, "locus-search", incoming, generation, args.replace)
        incoming = None
        event(state, "adopt-locus-search", generation["status"], "gen-%03d from %d locus searches ledger+%d total=%d"
              % (generation["generation"], len(summary), added, total))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def _policy_content(evidence_dir: Path, content_path: str, index: int) -> Path:
    """content_path is chosen by result data, so it may only name a regular file under the evidence directory."""
    if not isinstance(content_path, str) or not content_path or re.match(r"\A[a-z]+://", content_path):
        raise WorkflowError("policy evidence observation %d needs a local content_path" % index)
    candidate = Path(content_path)
    if not candidate.is_absolute():
        candidate = evidence_dir / candidate
    root = os.path.realpath(evidence_dir)
    resolved = os.path.realpath(candidate)
    if candidate.is_symlink() or os.path.commonpath([root, resolved]) != root or resolved == root:
        raise WorkflowError("policy evidence observation %d content_path must be a real file inside %s (no .., "
                            "symlink, or outside path): %s" % (index, root, content_path))
    if not Path(resolved).is_file():
        raise WorkflowError("policy evidence observation %d content_path is not a regular file: %s" % (index, content_path))
    return Path(resolved)


def bind_policy_evidence(source: str, incoming: Path, inputs: dict) -> str:
    """Copy the evidence JSON and every referenced policy file; rebind content_path to the copies.

    The copies live in the reserved generation directory, so the stored
    policy-evidence.json stays re-verifiable without the producer directory.
    """
    evidence_dir = Path(os.path.realpath(os.path.dirname(os.path.abspath(source))))
    data, evidence_sha = copy_input(source, incoming / "policy-evidence.source.json", "policy evidence")
    inputs["policy_evidence"] = {"sha256": evidence_sha, "source_path": os.path.abspath(source)}
    document = read_json_bytes(data, "policy evidence")
    observations = document.get("observations") if isinstance(document, dict) else None
    if not isinstance(observations, list):
        raise WorkflowError("policy evidence must hold an observations list")
    (incoming / "policy-content").mkdir()
    for index, observation in enumerate(observations):
        content_path = observation.get("content_path") if isinstance(observation, dict) else None
        content = _policy_content(evidence_dir, content_path, index)
        target = incoming / "policy-content" / ("%02d.bin" % index)
        _, digest = copy_input(str(content), target, "policy content")
        inputs["policy_content_%02d" % index] = {"sha256": digest, "source_path": str(content)}
        observation["content_path"] = str(target)
    bound = incoming / "policy-evidence.json"
    write_json_atomic(bound, document)
    return str(bound)


def _refuse_http(request, timeout):
    raise WorkflowError("record is an offline step and must not send HTTP: %s" % request.full_url)


def cmd_record(args, siblings: Siblings, request: dict, state: dict, run: Run) -> dict:
    _require_target(request, "record")
    _check_origin(request, args.origin)
    discovery, reason = usable(run, state, "discovery")
    if discovery is None:
        raise WorkflowError("record needs a usable discovery: " + reason)
    locus, reason = usable(run, state, "locus-search")
    if locus is None:
        raise WorkflowError("record needs a usable locus search: " + reason)
    if discovery["details"].get("discoveries"):
        return _record_per_repository(args, siblings, request, state, run, discovery, locus)
    if len(args.assessment) > 1:
        raise WorkflowError("the active discovery is one document; adopt one --assessment")
    assessment_path = args.assessment[0]
    revisions = siblings.revisions()
    incoming = run.incoming("record", state)
    try:
        data, assessment_sha = copy_input(assessment_path, incoming / "assessment.json", "assessment")
        inputs = {"assessment": {"sha256": assessment_sha, "source_path": os.path.abspath(assessment_path)},
                  "discovery": {"sha256": discovery["outputs"]["discovery.json"], "source_path": "stages/discovery/gen-%03d/discovery.json" % discovery["generation"]},
                  "locus_search": {"sha256": locus["outputs"]["locus-search.json"], "source_path": "stages/locus-search/gen-%03d/locus-search.json" % locus["generation"]}}
        argv_extra = []
        if args.policy_evidence:
            argv_extra = ["--policy-evidence", bind_policy_evidence(args.policy_evidence, incoming, inputs)]
        reused = reuse_if_same(run, state, "record", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None and reused["provenance"] == _claimed_provenance(request, args):
            event(state, "record", "reused", "gen-%03d" % reused["generation"])
            return reused
        shutil.copyfile(gen_dir(run, "locus-search", locus) / "manifest.json", incoming / "manifest.json")
        code, stdout, stderr = run_main(siblings.verifier.main, [
            "record", "--discovery", str(gen_dir(run, "discovery", discovery) / "discovery.json"),
            "--assessment", str(incoming / "assessment.json"),
            "--output", str(incoming / "candidates.json"), "--manifest", str(incoming / "manifest.json"),
            "--markdown-output", str(incoming / "candidates.md"),
            "--locus-search", str(gen_dir(run, "locus-search", locus) / "locus-search.json"),
        ] + argv_extra, opener=_refuse_http)
        if code not in (0, 3):
            raise WorkflowError("verifier record rejected the assessment (exit %d): %s" % (code, stderr.strip()))
        check, _, check_err = run_main(siblings.verifier.main, ["validate", "--candidates", str(incoming / "candidates.json")], opener=_refuse_http)
        if check != 0:
            raise WorkflowError("verifier validate rejected the candidates: " + check_err.strip())
        assessment = read_json_bytes(data, "assessment")
        searched = {(s.get("pattern_id"), str(s.get("repository", "")).lower(), s.get("locus"))
                    for s in json.loads((gen_dir(run, "locus-search", locus) / "locus-search.json").read_text(encoding="utf-8")).get("searches", [])}
        uncovered = [a for a in assessment.get("assessments", []) if a.get("locus") and
                     (a.get("pattern_id"), str(a.get("repository", "")).lower(), a.get("locus")) not in searched]
        reasons = ["assessed loci without a locus search: %d" % len(uncovered)] if uncovered else []
        status = "partial" if code == 3 or uncovered or locus["status"] == "partial" or discovery["status"] == "partial" else "complete"
        generation = {"status": status, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
                      "provenance": _claimed_provenance(request, args), "details": {"verifier": stdout.strip()}}
        commit(run, state, "record", incoming, generation, True)
        incoming = None
        event(state, "record", status, "gen-%03d" % generation["generation"])
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


def _renumbered_candidates(document: dict, start: int, capped: set, index: int) -> Tuple[List[dict], List[dict], List[dict]]:
    """One repository's candidates renumbered from ``start``; candidates of combinations the merged caps skipped are dropped.

    ``candidate_id`` (and the ``VER-<id>-<n>`` snapshot ids and ``superseded_by``
    that follow it) is only unique inside one verifier record output, like a
    corpus ``pr_id``. ``candidate_key`` is unchanged.
    """
    kept = [c for c in document["records"] if (c["repository"], c["pattern_ids"][0]) not in capped]
    dropped = [{"assessment": index, "candidate_id": c["candidate_id"], "repository": c["repository"],
                "pattern_id": c["pattern_ids"][0]} for c in document["records"] if c not in kept]
    local = {c["candidate_id"]: "CAN-%03d" % (start + number) for number, c in enumerate(kept, 1)}
    renumbered, mapping = [], []
    for original in kept:
        candidate = json.loads(json.dumps(original))
        candidate["candidate_id"] = local[original["candidate_id"]]
        for position, snapshot in enumerate(candidate["verification_history"], 1):
            snapshot["verification_id"] = "VER-%s-%d" % (candidate["candidate_id"], position)
        if candidate.get("superseded_by") is not None:
            if candidate["superseded_by"] not in local:
                raise WorkflowError("assessment %d: candidate %s is superseded by %s, which the merge does not keep"
                                    % (index, original["candidate_id"], candidate["superseded_by"]))
            candidate["superseded_by"] = local[candidate["superseded_by"]]
        renumbered.append(candidate)
        mapping.append({"assessment": index, "repository": candidate["repository"], "from": original["candidate_id"],
                        "to": candidate["candidate_id"], "candidate_key": candidate["candidate_key"]})
    return renumbered, mapping, dropped


def _record_per_repository(args, siblings: Siblings, request: dict, state: dict, run: Run, discovery: dict, locus: dict) -> dict:
    """Run the verifier's record once per repository, on that repository's own bytes, and merge the candidates.

    Each assessment is the answer for one repository's discovery and stays byte
    for byte as given: it must be bound to that discovery and be the assessment
    the repository's locus search used. The verifier record gate runs unchanged
    per repository; only its outputs are merged.
    """
    discoveries = discovery["details"]["discoveries"]
    loci = locus["details"].get("locus_searches")
    if not loci:
        raise WorkflowError("record needs one locus search per discovery; re-adopt the locus searches")
    if len(args.assessment) == 1:
        raise WorkflowError("the active discovery merges %d discoveries; adopt one --assessment per discovery, repeated"
                            % len(discoveries))
    revisions = siblings.revisions()
    incoming = run.incoming("record", state)
    discovery_dir, locus_dir = gen_dir(run, "discovery", discovery), gen_dir(run, "locus-search", locus)
    try:
        inputs, raw = _copy_bundles(incoming, [("assessment", "assessment.json", args.assessment)])
        inputs["discovery"] = {"sha256": discovery["outputs"]["discovery.json"],
                               "source_path": "stages/discovery/gen-%03d/discovery.json" % discovery["generation"]}
        for item in loci:
            inputs["locus_search-%02d" % item["index"]] = {
                "sha256": item["locus_search_sha256"],
                "source_path": "stages/locus-search/gen-%03d/bundles/%02d/locus-search.json" % (locus["generation"], item["index"])}
        argv_extra = []
        if args.policy_evidence:
            argv_extra = ["--policy-evidence", bind_policy_evidence(args.policy_evidence, incoming, inputs)]
        reused = reuse_if_same(run, state, "record", {k: v["sha256"] for k, v in inputs.items()}, revisions)
        if reused is not None and reused["provenance"] == _claimed_provenance(request, args):
            event(state, "record", "reused", "gen-%03d" % reused["generation"])
            return reused
        by_sha = {item["sha256"]: item for item in discoveries}
        locus_for = {item["discovery"]: item for item in loci}
        capped = {(item["repository"], item["pattern_id"]) for item in discovery["details"]["capped"]}
        errors: List[str] = []
        assigned: Dict[int, int] = {}
        documents: Dict[int, dict] = {}
        for index, item in enumerate(raw, 1):
            label = "assessment %d" % index
            document = read_json_bytes(item["assessment"], label)
            bound = by_sha.get(document.get("discovery_sha256")) if isinstance(document, dict) else None
            if bound is None:
                errors.append(label + " is not bound to an adopted discovery")
                continue
            if bound["index"] in assigned:
                errors.append("%s is bound to discovery %d, already assessed by assessment %d"
                              % (label, bound["index"], assigned[bound["index"]]))
                continue
            assigned[bound["index"]] = index
            documents[index] = document
            used = locus_for[bound["index"]]
            if inputs["assessment-%02d" % index]["sha256"] != used["assessment_sha256"]:
                errors.append("%s is not the assessment that locus search %d used" % (label, used["index"]))
            entries = document.get("assessments") if isinstance(document.get("assessments"), list) else []
            over = sorted({"%s %s" % (a.get("repository"), a.get("pattern_id")) for a in entries
                           if isinstance(a, dict) and (a.get("repository"), a.get("pattern_id")) in capped})
            if over:
                errors.append("%s assesses combinations the merged caps skipped: %s" % (label, ", ".join(over)))
        errors.extend("no assessment for discovery %d (%s)" % (item["index"], item["repository"])
                      for item in discoveries if item["index"] not in assigned)
        if errors:
            raise WorkflowError("scope-conflict: " + "; ".join(errors))

        merged: List[dict] = []
        mapping: List[dict] = []
        dropped: List[dict] = []
        generators, per_repository, outputs, partial, uncovered = set(), [], [], False, 0
        for item in discoveries:  # target order
            index, used = assigned[item["index"]], locus_for[item["index"]]
            folder = incoming / "bundles" / ("%02d" % index)
            locus_path = locus_dir / "bundles" / ("%02d" % used["index"]) / "locus-search.json"
            shutil.copyfile(locus_dir / "bundles" / ("%02d" % used["index"]) / "manifest.json", folder / "manifest.json")
            code, stdout, stderr = run_main(siblings.verifier.main, [
                "record", "--discovery", str(discovery_dir / "bundles" / ("%02d" % item["index"]) / "discovery.json"),
                "--assessment", str(folder / "assessment.json"), "--output", str(folder / "candidates.json"),
                "--manifest", str(folder / "manifest.json"), "--markdown-output", str(folder / "candidates.md"),
                "--locus-search", str(locus_path)] + argv_extra, opener=_refuse_http)
            if code not in (0, 3):
                raise WorkflowError("verifier record rejected assessment %d (%s) (exit %d): %s"
                                    % (index, item["repository"], code, stderr.strip()))
            check, _, check_err = run_main(siblings.verifier.main, ["validate", "--candidates", str(folder / "candidates.json")],
                                           opener=_refuse_http)
            if check != 0:
                raise WorkflowError("verifier validate rejected the candidates of assessment %d: %s" % (index, check_err.strip()))
            document = json.loads((folder / "candidates.json").read_text(encoding="utf-8"))
            generators.add(json.dumps(document.get("generated_by"), sort_keys=True))
            renumbered, moved, gone = _renumbered_candidates(document, len(merged), capped, index)
            merged.extend(renumbered)
            mapping.extend(moved)
            dropped.extend(gone)
            searched = {(s.get("pattern_id"), str(s.get("repository", "")).lower(), s.get("locus"))
                        for s in json.loads(locus_path.read_text(encoding="utf-8")).get("searches", [])}
            missing = sum(1 for a in documents[index].get("assessments", []) if isinstance(a, dict) and a.get("locus") and
                          (a.get("pattern_id"), str(a.get("repository", "")).lower(), a.get("locus")) not in searched)
            uncovered += missing
            partial = partial or code == 3
            outputs.append(stdout.strip())
            per_repository.append({"assessment": index, "discovery": item["index"], "repository": item["repository"],
                                   "locus_search": used["index"], "candidates": len(renumbered), "verifier_exit": code})
        if len(generators) > 1:
            raise WorkflowError("the per-repository verifier records differ in verifier revision (generated_by)")
        write_json_atomic(incoming / "candidates.json", {"schema_version": "1.0.0", "generated_by": json.loads(generators.pop()),
                                                         "records": merged})
        check, _, check_err = run_main(siblings.verifier.main, ["validate", "--candidates", str(incoming / "candidates.json")],
                                       opener=_refuse_http)
        if check != 0:
            raise WorkflowError("verifier validate rejected the merged candidates: " + check_err.strip())
        code, _, stderr = run_main(siblings.verifier.main, ["render", "--candidates", str(incoming / "candidates.json"),
                                                             "--output", str(incoming / "candidates.md")], opener=_refuse_http)
        if code != 0:
            raise WorkflowError("verifier render rejected the merged candidates: " + stderr.strip())
        write_json_atomic(incoming / "workflow-merge.json", {"merged_by": SKILL_NAME, "repositories": per_repository,
                                                             "candidate_ids": mapping, "dropped_capped": dropped})
        reasons = ["assessed loci without a locus search: %d" % uncovered] if uncovered else []
        if dropped:
            reasons.append("candidates of combinations the merged caps skipped, dropped: %d" % len(dropped))
        status = ("partial" if partial or uncovered or locus["status"] == "partial" or discovery["status"] == "partial"
                  else "complete")
        generation = {"status": status, "reasons": reasons, "inputs": inputs, "tool_revisions": revisions,
                      "provenance": _claimed_provenance(request, args),
                      "details": {"verifier": outputs, "records": len(merged), "repositories": per_repository}}
        commit(run, state, "record", incoming, generation, True)
        incoming = None
        event(state, "record", status, "gen-%03d from %d repositories" % (generation["generation"], len(per_repository)))
        return generation
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming)


# ---------------------------------------------------------------------------
# Status and report
# ---------------------------------------------------------------------------


def next_steps(run: Run, request: dict, state: dict) -> List[dict]:
    """Plan data only; nothing here is executed or implies a passed launcher gate."""
    plan_note = ("plan data; executed=false. The argv shows the scope of the step, not the isolated command. "
                 "New research runs only through the research launcher's Linux VM entries, with the user's "
                 "approval and the coordinator's instruction; that launcher's gate is not evaluated by this workflow.")
    # Recent-closed collection is the only network step with an isolated entry that has made real reads.
    unverified_note = plan_note + " No isolated entry has run this step against real data yet."
    if request["source"]["kind"] not in SUPPORTED_SOURCE_KINDS:
        return [{"stage": "collect", "kind": "pending-unsupported", "executed": False,
                 "detail": "source.kind %s has no connected route" % request["source"]["kind"]}]
    source, target = request["source"], request["target"]
    blocked = integrity_failures(run, state)
    if blocked:
        return [{"stage": stage, "kind": "blocked", "executed": False,
                 "detail": "%s; re-adopt with --replace (digest and record: run again)" % ", ".join(reasons)}
                for stage, reasons in blocked.items()]
    for stage in STAGES:
        generation = active(state, stage)
        if generation is not None and generation["status"] in ("failed", "blocked"):
            return [{"stage": stage, "kind": generation["status"], "executed": False,
                     "detail": "; ".join(generation["reasons"]) or generation["status"]}]
    if active(state, "collect") is None and source["kind"] == "curated-tracker":
        argv = ["collect_curated_contribution_prs.py", "collect", "--tracker-repo", source["tracker"]["repository"],
                "--issue", str(source["tracker"]["issue_number"]), "--max-prs", str(source["max_prs"]),
                "--request-budget", str(source["request_budget"]),
                "--output", "<ISOLATED_OUTPUT>/corpus.json", "--manifest", "<ISOLATED_OUTPUT>/manifest.json"]
        return [{"stage": "collect", "kind": "external-network-step", "executed": False, "argv": argv, "note": unverified_note,
                 "then": "adopt-collection --corpus ... --manifest ... (pull requests outside source.repositories "
                         "are rejected, not added)"}]
    if active(state, "collect") is None:
        argv = ["collect_recent_closed_prs.py", "collect"]
        for repository in source["repositories"]:
            argv += ["--repo", repository]
        argv += ["--start-at", source["interval"]["start_at"], "--end-at", source["interval"]["end_at"],
                 "--outcome", source["outcome"] if isinstance(source["outcome"], str) else "<merged|closed-unmerged>",
                 "--max-per-repo", str(source["max_per_repository"]),
                 "--request-budget", str(source["request_budget"]),
                 "--output", "<ISOLATED_OUTPUT>/corpus.json", "--manifest", "<ISOLATED_OUTPUT>/manifest.json",
                 "--markdown-output", "<ISOLATED_OUTPUT>/inventory.md"]
        start, end = parse_utc(source["interval"]["start_at"]), parse_utc(source["interval"]["end_at"])
        then = "adopt-collection --corpus ... --manifest ... (or adopt an existing corpus explicitly)"
        if isinstance(source["outcome"], list) or (end - start).days > COLLECTOR_WINDOW_DAYS:
            then = ("one run per repository, outcome and window of at most %d days (the collector's limit), then "
                    "adopt-collection with --corpus ... --manifest ... repeated once per run; the runs must tile "
                    "source.interval for each repository and outcome" % COLLECTOR_WINDOW_DAYS)
        return [{"stage": "collect", "kind": "external-network-step", "executed": False, "argv": argv, "note": plan_note,
                 "then": then}]
    analysis = active(state, "analysis")
    if analysis is None and active(state, "digest") is None:
        return [{"stage": "digest", "kind": "local", "executed": False, "command": "digest"}]
    if analysis is None:
        digest = active(state, "digest")
        return [{"stage": "analysis", "kind": "awaiting-ai", "executed": False,
                 "detail": "AI extraction and synthesis (analyzer steps 3-10 on stages/digest/gen-%03d) never run in "
                           "this session. An isolated model run opens only for the research launcher's one measured "
                           "read-only launch, with the user's approval; every other model launch is closed. "
                           "Adopting an existing AI output offline is allowed. The script never writes PAT-*."
                           % digest["generation"],
                 "then": "adopt-analysis --analysis ... --origin ai"}]
    if target is None:
        return [{"stage": stage, "kind": "skipped", "executed": False, "detail": TARGET_UNSPECIFIED}
                for stage in CANDIDATE_STAGES]
    if analysis["status"] == "no-patterns":
        return []
    if active(state, "discovery") is None:
        def discover_argv(repositories):
            argv = ["verify_candidates.py", "discover", "--analysis", "stages/analysis/gen-%03d/analysis.json" % analysis["generation"]]
            for repository in repositories:
                argv += ["--repo", repository]
            return argv + ["--max-candidates-per-repo", str(target["max_candidates_per_repo"]),
                           "--max-candidates-total", str(target["max_candidates_total"]),
                           "--request-budget", str(target["discover_request_budget"]),
                           "--output", "<ISOLATED_OUTPUT>/discovery.json", "--manifest", "<ISOLATED_OUTPUT>/manifest.json"]
        if len(target["repositories"]) > 1:
            # The candidate-reads entry reads one repository per run.
            then = ("adopt-discovery with --discovery ... --manifest ... repeated once per repository (%d), in any "
                    "order; the target caps are applied again over the set" % len(target["repositories"]))
            return [{"stage": "discovery", "repository": repository, "kind": "external-network-step", "executed": False,
                     "argv": discover_argv([repository]), "note": unverified_note, "then": then}
                    for repository in target["repositories"]]
        return [{"stage": "discovery", "kind": "external-network-step", "executed": False,
                 "argv": discover_argv(target["repositories"]), "note": unverified_note,
                 "then": "adopt-discovery --discovery ... --manifest ..."}]
    discovery = active(state, "discovery")
    if discovery["status"] == "no-candidates":
        return []
    bundles = discovery["details"].get("discoveries")
    record_step = {"stage": "record", "kind": "local", "executed": False, "command": "record --assessment ... --origin ai"}
    if bundles:
        record_step["command"] = ("record --assessment ... --origin ai, with --assessment once per repository (%d), "
                                  "each the one its locus search used" % len(bundles))
    if active(state, "locus-search") is None and bundles:
        return _per_repository_steps(analysis, discovery, target, unverified_note) + [record_step]
    if active(state, "locus-search") is None:
        # assess -> locus-search -> record (#259): the assess input is the analysis bundle's (stage_inputs.py,
        # docs/research/oss-e2e-2026-09-28), answered by one isolated model call and checked strictly before adoption.
        return [{"stage": "assessment", "kind": "awaiting-ai", "executed": False,
                 "argv": ["stage_inputs.py", "assess-inputs",
                          "--analysis", "stages/analysis/gen-%03d/analysis.json" % analysis["generation"],
                          "--discovery", "stages/discovery/gen-%03d/discovery.json" % discovery["generation"],
                          "--out", "<HOST_INPUT>/assess"],
                 "detail": "the assessment of stages/discovery/gen-%03d is AI research and never runs in this "
                           "session: one isolated model call answers the assess input, through the research "
                           "launcher's one measured read-only launch with the user's approval. No isolated model has "
                           "returned an assessment yet; only an assessment that already exists can be recorded"
                           % discovery["generation"],
                 "then": "verify_e2e_result.py --answer-kind assess --discovery ... --input-scope ... --adopt-to "
                         "... (a ready status passes only with --locus-search of its loci); only the adopted "
                         "answer is the assessment"},
                {"stage": "locus-search", "kind": "external-network-step", "executed": False, "note": unverified_note,
                 "argv": ["verify_candidates.py", "locus-search", "--discovery", "stages/discovery/gen-%03d/discovery.json" % discovery["generation"],
                          "--assessment", "<assessment.json>", "--output", "<ISOLATED_OUTPUT>/locus-search.json",
                          "--manifest", "<copy of stages/discovery/gen-%03d/manifest.json>" % discovery["generation"],
                          "--request-budget", str(target["locus_request_budget"])],
                 "then": "adopt-locus-search --locus-search ... --manifest ..."},
                record_step]
    if active(state, "record") is None:
        return [record_step]
    return []


def _per_repository_steps(analysis: dict, discovery: dict, target: dict, note: str) -> List[dict]:
    """Assessment and locus search per repository: each on that repository's own discovery bytes (bundles/NN)."""
    steps, loci = [], []
    for bundle in discovery["details"]["discoveries"]:
        base = "stages/discovery/gen-%03d/bundles/%02d/" % (discovery["generation"], bundle["index"])
        left_out = ["%s %s" % (item["repository"], item["pattern_id"]) for item in discovery["details"]["capped"]
                    if item["discovery"] == bundle["index"]]
        detail = ("the assessment of %s (%s) is AI research and never runs in this session: one isolated model call "
                  "per repository answers that discovery's assess input, through the research launcher's one measured "
                  "read-only launch with the user's approval. No isolated model has returned an assessment yet; only "
                  "an assessment that already exists can be recorded" % (base + "discovery.json", bundle["repository"]))
        if left_out:
            detail += "; leave out the combinations the merged caps skipped: " + ", ".join(left_out)
        steps.append({"stage": "assessment", "repository": bundle["repository"], "kind": "awaiting-ai", "executed": False,
                      "argv": ["stage_inputs.py", "assess-inputs",
                               "--analysis", "stages/analysis/gen-%03d/analysis.json" % analysis["generation"],
                               "--discovery", base + "discovery.json", "--out", "<HOST_INPUT>/assess-%02d" % bundle["index"]],
                      "detail": detail,
                      "then": "verify_e2e_result.py --answer-kind assess --discovery %sdiscovery.json --input-scope ... "
                              "--adopt-to ... (a ready status passes only with --locus-search of its loci); only the "
                              "adopted answer is that repository's assessment" % base})
        loci.append({"stage": "locus-search", "repository": bundle["repository"], "kind": "external-network-step",
                     "executed": False, "note": note,
                     "argv": ["verify_candidates.py", "locus-search", "--discovery", base + "discovery.json",
                              "--assessment", "<assessment of %s>" % bundle["repository"],
                              "--output", "<ISOLATED_OUTPUT>/locus-search.json",
                              "--manifest", "<copy of %smanifest.json>" % base,
                              "--request-budget", str(target["locus_request_budget"])],
                     "then": "adopt-locus-search with --locus-search ... --manifest ... repeated once per repository (%d)"
                             % len(discovery["details"]["discoveries"])})
    return steps + loci


def overall(run: Run, request: dict, state: dict) -> Tuple[str, str]:
    """(outcome, completeness)."""
    if request["source"]["kind"] not in SUPPORTED_SOURCE_KINDS:
        return "pending-unsupported", "none"
    if integrity_failures(run, state):
        return "blocked", "none"
    partial = False
    for stage in STAGES:
        generation = active(state, stage)
        if generation is None:
            continue
        if generation["status"] == "failed":
            return "failed", "none"
        if generation["status"] == "blocked":
            return "blocked", "none"
        partial = partial or generation["status"] == "partial"
    completeness = "partial" if partial else "complete"
    analysis, discovery, record = active(state, "analysis"), active(state, "discovery"), active(state, "record")
    if analysis is not None and analysis["status"] == "no-patterns":
        return "insufficient", completeness
    if analysis is not None and request["target"] is None:
        # Patterns exist but nothing was checked against a target: never a candidate verdict.
        return "analysis-only", completeness
    if discovery is not None and discovery["status"] == "no-candidates":
        return "insufficient", completeness
    if record is None:
        return "awaiting-input", completeness
    candidates = json.loads((gen_dir(run, "record", record) / "candidates.json").read_text(encoding="utf-8"))["records"]
    if any(c["status"] in ACTIONABLE_STATUSES for c in candidates):
        return "candidates", completeness
    if candidates and all(c["status"] in DECISIVE_NON_ACTIONABLE for c in candidates):
        return "no-candidates", completeness
    return "insufficient", completeness


def status_document(run: Run, request: dict, state: dict) -> dict:
    outcome, completeness = overall(run, request, state)
    stages = {}
    for stage in STAGES:
        generation = active(state, stage)
        history = state["stages"][stage]["generations"]
        skipped = generation is None and stage in CANDIDATE_STAGES and request["target"] is None
        stages[stage] = {
            "active": None if generation is None else "gen-%03d" % generation["generation"],
            "status": "skipped" if skipped else "not-run" if generation is None else generation["status"],
            "reasons": [TARGET_UNSPECIFIED] if skipped else [] if generation is None else generation["reasons"],
            "generations": len(history),
            "invalidated": [g["invalidated"] for g in history if g.get("invalidated")],
        }
    collected = active(state, "collect")
    if collected is not None and collected.get("details", {}).get("collections"):
        stages["collect"]["collections"] = collected["details"]["collections"]
    analyzed = active(state, "analysis")
    if analyzed is not None and analyzed.get("details", {}).get("analyzer_revision_drift"):
        stages["analysis"]["analyzer_revision_drift"] = analyzed["details"]["analyzer_revision_drift"]
    for stage, key in (("discovery", "discoveries"), ("locus-search", "locus_searches")):
        generation = active(state, stage)
        if generation is not None and generation.get("details", {}).get(key):
            stages[stage][key] = generation["details"][key]
            if stage == "discovery":
                stages[stage]["capped"] = generation["details"]["capped"]
    ledger_total = sum(e["requests"] for e in state["ledger"])
    return {
        "run_id": state["run_id"], "mode": request["mode"], "outcome": outcome, "completeness": completeness,
        "stages": stages, "integrity_failures": integrity_failures(run, state),
        "budget": {"producer_reported_requests": ledger_total, "limit": request["budget"]["total_api_requests"],
                   "external_requests_by_this_workflow": 0,
                   "producer_requests_simulated": request["mode"] == "offline-fixture",
                   "violations": budget_violations(request, state)},
        "next": next_steps(run, request, state),
        "last_rejection": next((e for e in reversed(state["events"]) if e["result"] == "rejected"), None),
    }


def _short(value: Optional[str]) -> str:
    if not value:
        return "-"
    return value.replace("sha256:", "")[:12]


def render_report(run: Run, request: dict, state: dict) -> str:
    status = status_document(run, request, state)
    source, target = request["source"], request["target"]
    lines = ["# OSS contribution research run", ""]
    lines += ["- Outcome: `%s` (completeness: `%s`)" % (status["outcome"], status["completeness"]),
              "- Mode: `%s` — this workflow sent 0 external requests; network steps are plan data only" % request["mode"],
              "- Run: `%s`, request sha256 `%s`" % (state["run_id"], state["request_sha256"]),
              "- Workflow revision: `%s`" % state["skills"]["revisions"]["workflow"], ""]
    lines += ["## Request scope", "",
              "| Field | Value |", "| --- | --- |",
              "| Source kind | `%s` |" % source["kind"]]
    if source["kind"] == "curated-tracker":
        lines += ["| Tracker Issue | `%s#%d` |" % (source["tracker"]["repository"], source["tracker"]["issue_number"]),
                  "| Allowed pull request repositories | %s |" % ", ".join("`%s`" % r for r in source["repositories"]),
                  "| Pull request cap (max_prs) | %d |" % source["max_prs"],
                  "| Reference repositories | %s |" % (", ".join(source["reference_repositories"]) or "none")]
    else:
        lines.append("| Source repositories | %s |" % ", ".join("`%s`" % r for r in source["repositories"]))
    if source["kind"] == "recent-closed-prs":
        lines += ["| Interval (UTC) | %s → %s |" % (source["interval"]["start_at"], source["interval"]["end_at"]),
                  "| Outcome / cap per repository | %s / %d |" % (outcome_label(source["outcome"]), source["max_per_repository"]),
                  "| Reference repositories | %s |" % (", ".join(source["reference_repositories"]) or "none")]
    if target is None:
        lines += ["| Target repositories | unspecified (candidate verification skipped) |",
                  "| Candidate caps | not applicable (no target) |",
                  "| Static only / clone | %s / not applicable (no target) |" % request["static_only"]]
    else:
        lines += ["| Target repositories | %s |" % ", ".join("`%s`" % r for r in target["repositories"]),
                  "| Candidate caps | %d per repository, %d total |" % (target["max_candidates_per_repo"], target["max_candidates_total"]),
                  "| Static only / clone | %s / %s |" % (request["static_only"], target["allow_clone"])]
    lines += ["| Session model / effort | %s / %s |" % (request["provenance"]["model"], request["provenance"]["effort"]), ""]
    lines += ["## Stages", "", "| Stage | Status | Checkpoint | Inputs (sha256) | Reasons |", "| --- | --- | --- | --- | --- |"]
    for stage in STAGES:
        generation = active(state, stage)
        entry = status["stages"][stage]
        inputs = "" if generation is None else ", ".join("%s `%s`" % (k, _short(v["sha256"])) for k, v in sorted(generation["inputs"].items()))
        reasons = "; ".join(entry["reasons"] + ["invalidated: " + x for x in entry["invalidated"]]) or "-"
        lines.append("| %s | %s | %s | %s | %s |" % (stage, entry["status"], entry["active"] or "-", inputs or "-", reasons.replace("|", "/")))
    if status["integrity_failures"]:
        lines += ["", "Blocked: modified checkpoint outputs %s" % json.dumps(status["integrity_failures"], sort_keys=True)]
    collections = status["stages"]["collect"].get("collections")
    if collections:
        windowed = any("interval" in item for item in collections)
        lines += ["", "## Collections", "",
                  "The collect stage was adopted from %d collections (%d records)."
                  % (len(collections), sum(item["records"] for item in collections)), "",
                  "| # | Repositories | " + ("Interval (UTC) | Outcome | " if windowed else "")
                  + "Records | Run | Requests / budget | Status |",
                  "| --- | --- | " + ("--- | --- | " if windowed else "") + "--- | --- | --- | --- |"]
        for item in collections:
            by_repository = ", ".join("`%s` %d" % (name, count) for name, count in sorted(item["records_by_repository"].items()))
            window = ("%s → %s | %s | " % (item["interval"]["start_at"], item["interval"]["end_at"], item["outcome"])
                      if windowed else "")
            lines.append("| %d | %s | %s%s | %s | %d / %d | %s |" % (
                item["index"], ", ".join("`%s`" % r for r in item["repositories"]), window, by_repository or "0",
                ", ".join("`%s`" % r for r in item["run_ids"]), item["request_count"], item["request_budget"], item["status"]))
        lines.append("")
        lines.append("Requests across the collections: %d of a summed per-run budget of %d."
                     % (sum(item["request_count"] for item in collections), sum(item["request_budget"] for item in collections)))
    lines += _candidate_reads_section(status)
    lines += ["", "Tool revisions: " + ", ".join("%s `%s`" % (k, _short(v)) for k, v in sorted(state["skills"]["revisions"].items())), ""]
    lines += ["## AI judgment provenance", "",
              "Origin, model, and effort are caller claims bound to input hashes. They do not prove that a model ran.", ""]
    drift = status["stages"]["analysis"].get("analyzer_revision_drift")
    if drift:
        lines += ["- Analyzer revision drift: the analysis was adopted under its recorded analyzer revision `%s`; the "
                  "current analyzer is `%s`. It passed that revision's own validator and the current validator's "
                  "rules; it was not written or checked by the current analyzer's prompts." % (drift["recorded"], drift["current"])]
    for stage in ("analysis", "record"):
        generation = active(state, stage)
        if generation is not None:
            p = generation["provenance"]
            lines.append("- %s: origin `%s`, model `%s`, effort `%s`" % (stage, p["origin"], p["model"], p["effort"]))
    lines += ["", "## Budget", "",
              "- External requests sent by this workflow: 0",
              "- Producer-reported requests: %d of %d (%s)" % (
                  status["budget"]["producer_reported_requests"], status["budget"]["limit"],
                  "offline fixture responses, not real API calls" if request["mode"] == "offline-fixture"
                  else "reported by the producer manifests; not independently verified"),
              "- Violations: %s" % (", ".join(status["budget"]["violations"]) or "none"),
              "- Ad hoc lookups outside the skills are not part of this budget and are not allowed.", ""]
    lines += ["| Producer | Command | Run | Requests | Elapsed s (producer timestamps) |", "| --- | --- | --- | --- | --- |"]
    for entry in state["ledger"]:
        lines.append("| %s | %s | %s | %d | %s |" % (entry["producer"], entry["command"], entry["run_id"], entry["requests"],
                                                   "-" if entry["elapsed_seconds"] is None else entry["elapsed_seconds"]))
    if status["integrity_failures"]:
        lines += ["", "## Candidates", "", "Not rendered: blocked checkpoints are not read. Re-adopt them first.", ""]
    else:
        lines += [""] + _candidate_sections(run, state, request) + _case_section(run, state)
    lines += ["## Remaining judgment and next steps", ""]
    for step in status["next"]:
        lines.append("- `%s` (%s, executed=%s): %s" % (step["stage"], step["kind"], step["executed"],
                                                       step.get("detail") or " ".join(step.get("argv", [])) or step.get("command", "")))
    if not status["next"]:
        lines.append("- No workflow step is pending. Candidate-level judgment is listed above.")
    if status["last_rejection"]:
        lines.append("- Last rejected command: `%s` — %s" % (status["last_rejection"]["command"], status["last_rejection"]["detail"]))
    lines += ["", "## Limits", "",
              "- Offline checkpoint validation only. No OS isolation, credential, or launcher gate is proven here.",
              "- This workflow only adopts outputs that already exist and runs synthetic fixtures. New research "
              "belongs to the research launcher's Linux VM entries, with the user's approval. Candidate reads "
              "(discovery, locus-search) and assessments have no isolated path verified against real data yet.",
              "- `complete` search means every query returned; it never means no duplicate exists.",
              "- General Issue sources (`issues`) are not connected (pending-unsupported): an isolated Issue "
              "collection entry exists, but the Issue corpus is not a pull request corpus, and one run takes one "
              "source kind.", ""]
    if source["kind"] == "curated-tracker":
        lines[-1:-1] = ["- Curated tracker: submissions come from the collector's narrow label heuristic over tracker "
                        "comments; tracker association is not upstream role evidence, and unresolved pull requests keep "
                        "an `unknown` state."]
    return "\n".join(lines)


def _candidate_reads_section(status: dict) -> List[str]:
    """Discovery and locus search per target repository, when they arrived as one run per repository."""
    discoveries = status["stages"]["discovery"].get("discoveries")
    if not discoveries:
        return []
    loci = {item["discovery"]: item for item in status["stages"]["locus-search"].get("locus_searches") or []}
    lines = ["", "## Candidate reads by repository", "",
             "The discovery stage was adopted from %d discoveries, one per target repository; assessment, locus "
             "search, and verifier record run per repository and `record` merges the candidates." % len(discoveries), "",
             "| # | Repository | Discovery | Records / skipped by cap / failed (its own run) | Requests / budget | Locus search | Locus requests / budget |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in discoveries:
        locus = loci.get(item["index"])
        lines.append("| %d | `%s` | %s | %d / %d / %d | %s / %s | %s | %s |" % (
            item["index"], item["repository"], item["status"], item["records"], item["skipped_by_cap"], item["failed_scopes"],
            item["requests_consumed"], item["request_budget"],
            "-" if locus is None else "%s (%d searches)" % (locus["status"], locus["searches"]),
            "-" if locus is None else "%s / %s" % (locus["requests_consumed"], locus["request_budget"])))
    capped = status["stages"]["discovery"].get("capped") or []
    lines += ["", "Moved to skipped_by_cap by the target caps over the set: %s."
              % (", ".join("`%s` %s" % (item["repository"], item["pattern_id"]) for item in capped) or "none")]
    return lines


def _candidate_sections(run: Run, state: dict, request: dict) -> List[str]:
    record = active(state, "record")
    if record is None and request["target"] is None:
        return ["## Candidates", "", "Not verified: " + TARGET_UNSPECIFIED + ". No candidate exists; this is not a verdict.", ""]
    if record is None:
        return ["## Candidates", "", "No candidate record yet.", ""]
    synthetic = record["provenance"]["origin"] == "synthetic-fixture" or (
        active(state, "analysis") is not None and active(state, "analysis")["provenance"]["origin"] == "synthetic-fixture")
    candidates = json.loads((gen_dir(run, "record", record) / "candidates.json").read_text(encoding="utf-8"))["records"]
    title = "## Synthetic fixture candidates (not contribution candidates)" if synthetic else "## Candidates (assessment origin claimed: ai)"
    lines = [title, "", "Verifier Markdown: `stages/record/gen-%03d/candidates.md`" % record["generation"], ""]
    if not candidates:
        lines += ["No candidate records.", ""]
    for candidate in candidates:
        unconfirmed = sorted(k for k, v in candidate["readiness_checks"].items() if v["result"] == "not-confirmed")
        kind = ("actionable" if candidate["status"] in ACTIONABLE_STATUSES
                else "decisive non-actionable" if candidate["status"] in DECISIVE_NON_ACTIONABLE else "undecided")
        lines += ["### %s — %s / %s" % (candidate["candidate_id"], candidate["status"], candidate["status_reason"]), "",
                  "- Class: %s%s" % (kind, " (synthetic fixture)" if synthetic else ""),
                  "- Pattern: %s; repository `%s`; locus `%s`" % (", ".join(candidate["pattern_ids"]), candidate["repository"], candidate["locus"]),
                  "- Evidence base SHA `%s` at %s" % (candidate["verified_base_sha"], candidate["verified_at"]),
                  "- Evidence links: %s" % (", ".join(candidate["evidence_links"]) or "none"),
                  "- Blocking gaps: %s" % (", ".join(candidate["blocking_gaps"]) or "none"),
                  "- Remaining judgment (unconfirmed readiness): %s" % (", ".join(unconfirmed) or "none"), ""]
    return lines


def _case_section(run: Run, state: dict) -> List[str]:
    analysis, collect = active(state, "analysis"), active(state, "collect")
    if analysis is None or collect is None:
        return []
    document = json.loads((gen_dir(run, "analysis", analysis) / "analysis.json").read_text(encoding="utf-8"))
    urls = {r.get("pr_id"): r.get("pull_request", {}).get("url") for r in document.get("records", []) if isinstance(r, dict)}
    lines = ["## PR cases behind each pattern", ""]
    for pattern in document.get("patterns", []):
        ids = pattern.get("evidence_pr_ids") or []
        lines.append("- %s (%s): %s" % (pattern["pattern_id"], pattern.get("confidence", {}).get("level", "unknown"),
                                        ", ".join("%s %s" % (i, urls.get(i) or "?") for i in ids) or "none"))
    if not document.get("patterns"):
        lines.append("- No pattern: the analysis is an insufficient result, not an error.")
    return lines + [""]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_option(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer: %r" % value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer: %r" % value)
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--print-revision", action="store_true")
    commands = parser.add_subparsers(dest="command")

    def command(name):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--skills-root")
        return sub

    command("init").add_argument("--request", required=True)
    adopt = command("adopt-collection")
    # Repeat the three flags for each collection of one request, in the order to merge them.
    adopt.add_argument("--corpus", required=True, action="append")
    adopt.add_argument("--manifest", required=True, action="append")
    adopt.add_argument("--inventory", action="append", default=[])
    adopt.add_argument("--replace", action="store_true")
    digest = command("digest")
    digest.add_argument("--max-bytes", type=_positive_option, default=None,
                        help="largest digest shard in bytes (default %d, so one lens prompt stays under %d)"
                        % (DIGEST_MAX_BYTES, DIGEST_PROMPT_TARGET_BYTES))
    analysis = command("adopt-analysis")
    analysis.add_argument("--analysis", required=True)
    analysis.add_argument("--extraction-result", action="append", default=[])
    # An analysis of an earlier analyzer revision: that revision's analyzer skill directory (#266).
    analysis.add_argument("--analyzer-source")
    for sub in (analysis,):
        sub.add_argument("--origin", choices=ORIGINS, required=True)
        sub.add_argument("--model")
        sub.add_argument("--effort")
        sub.add_argument("--replace", action="store_true")
    # One target repository per candidate-reads run: repeat the pair once per repository.
    discovery = command("adopt-discovery")
    discovery.add_argument("--discovery", required=True, action="append")
    discovery.add_argument("--manifest", required=True, action="append")
    discovery.add_argument("--replace", action="store_true")
    locus = command("adopt-locus-search")
    locus.add_argument("--locus-search", required=True, action="append")
    locus.add_argument("--manifest", required=True, action="append")
    locus.add_argument("--replace", action="store_true")
    record = command("record")
    record.add_argument("--assessment", required=True, action="append")
    record.add_argument("--policy-evidence")
    record.add_argument("--origin", choices=ORIGINS, required=True)
    record.add_argument("--model")
    record.add_argument("--effort")
    command("status")
    command("report")
    return parser


HANDLERS = {
    "adopt-collection": cmd_adopt_collection,
    "digest": cmd_digest,
    "adopt-analysis": cmd_adopt_analysis,
    "adopt-discovery": cmd_adopt_discovery,
    "adopt-locus-search": cmd_adopt_locus,
    "record": cmd_record,
}


def main(argv: Optional[Sequence[str]] = None, *, environ=os.environ) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.print_revision:
        print(compute_revision())
        return 0
    if args.command is None:
        parser.error("a command is required")
    try:
        siblings = Siblings(resolve_siblings(args.skills_root, environ))
        if args.command == "init":
            print(json.dumps(cmd_init(args, siblings), ensure_ascii=False, sort_keys=True))
            return 0
        run = Run(args.run_dir)
        request, state = run.load()
        run.revisions = siblings.revisions()
        if args.command == "status":
            print(json.dumps(status_document(run, request, state), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "report":
            print(str(run.write_report(render_report(run, request, state))))
            return 0
        try:
            generation = HANDLERS[args.command](args, siblings, request, state, run)
        except WorkflowError as error:
            # A rejection changes nothing but the event log: reload the saved state.
            _, saved = run.load()
            event(saved, args.command, "rejected", str(error))
            run.save(saved)
            raise
        run.save(state)
        print(json.dumps({"stage": args.command, "generation": generation["generation"], "status": generation["status"],
                          "reasons": generation["reasons"]}, ensure_ascii=False, sort_keys=True))
        return EXIT.get(generation["status"], 3)
    except (WorkflowError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
