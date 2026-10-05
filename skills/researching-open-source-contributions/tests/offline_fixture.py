"""Test-only support: offline guards and one synthetic GitHub world.

Importing this module first installs guards that make socket, HTTP
connection, urllib opener, and process-spawn entry points raise
``OfflineViolation``. It also removes GitHub token variables from the
environment. This is an in-process test guard, not proof of OS isolation.

Every GitHub response comes from an exact route table served to the
production clients through their injected ``opener``. A request without a
route raises ``MissingFixture`` instead of becoming a default 404. The fake
response objects come from the sibling skills' own tests, so no transport is
reimplemented here.

Dependencies: python3 standard library, and the sibling skills
collecting-recent-closed-prs, collecting-curated-contribution-prs,
analyzing-open-source-pr-patterns, and
verifying-open-source-contribution-candidates (scripts and tests). They are
found through ``OSS_WORKFLOW_SKILLS_ROOT`` or the chezmoi source checkout
(``dot_codex/skills`` next to ``dot_agents/skills``).

PAT-1, its quotes, and every assessment below are hand-written synthetic
stand-ins. They are never a real pattern or contribution candidate.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import base64  # noqa: E402
from copy import deepcopy  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
import hashlib  # noqa: E402
import http.client  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402


class OfflineViolation(BaseException):
    """A network or process entry point was reached inside the tests."""


class MissingFixture(BaseException):
    """A request reached a fake opener without an explicit route."""


GUARDED_ENTRY_POINTS = (
    (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
    (socket, "getaddrinfo"), (socket, "gethostbyname"), (socket, "gethostbyname_ex"),
    (http.client.HTTPConnection, "connect"), (urllib.request.OpenerDirector, "open"),
    (urllib.request, "urlopen"), (subprocess.Popen, "_execute_child"),
    (os, "system"), (os, "popen"), (os, "fork"), (os, "forkpty"), (os, "posix_spawn"),
    (os, "posix_spawnp"), (os, "execv"), (os, "execve"), (os, "execvp"), (os, "execvpe"),
    (os, "spawnv"), (os, "spawnve"),
)
TOKEN_ENVIRONMENT = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def _refusal(label):
    def refuse(*args, **kwargs):
        raise OfflineViolation(label + " is blocked by the offline test guard")

    return refuse


def _install_guards():
    installed = []
    for owner, name in GUARDED_ENTRY_POINTS:
        if hasattr(owner, name):
            label = getattr(owner, "__name__", type(owner).__name__) + "." + name
            setattr(owner, name, _refusal(label))
            installed.append(label)
    for name in TOKEN_ENVIRONMENT:
        os.environ.pop(name, None)
    return tuple(installed)


INSTALLED_GUARDS = _install_guards()

# ---------------------------------------------------------------------------
# Products, loaded only after the guards
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parents[1]


def skills_root() -> Path:
    if os.environ.get("OSS_WORKFLOW_SKILLS_ROOT"):
        return Path(os.environ["OSS_WORKFLOW_SKILLS_ROOT"])
    if SKILL_DIR.parent.name == "skills" and SKILL_DIR.parent.parent.name == "dot_agents":
        return SKILL_DIR.parent.parent.parent / "dot_codex" / "skills"
    return SKILL_DIR.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SKILLS_ROOT = skills_root()
workflow = _load("oss_workflow", SKILL_DIR / "scripts" / "oss_workflow.py")
siblings = workflow.Siblings(workflow.resolve_siblings(str(SKILLS_ROOT)))
collector = siblings.collector
validate_corpus = siblings.validate_corpus
vc = siblings.verifier
# Only module objects are bound, so unittest never collects the sibling TestCases.
collector_tests = _load("oss_workflow_collector_tests",
                        SKILLS_ROOT / "collecting-recent-closed-prs" / "tests" / "test_collect_recent_closed_prs.py")
verifier_tests = _load("oss_workflow_verifier_tests",
                       SKILLS_ROOT / "verifying-open-source-contribution-candidates" / "tests" / "test_verify_candidates.py")
# The collector tests re-execute the collector under its plain name; keep the workflow's copy current.
collector = sys.modules["collect_recent_closed_prs"]
siblings.collector = collector

# ---------------------------------------------------------------------------
# Synthetic GitHub world
# ---------------------------------------------------------------------------

SYNTHETIC = "SYNTHETIC FIXTURE"
API_VERSION = "2026-03-10"
OWNER = "synth-fixture-org"
REPO_NAME = "synth-retry-lib"
REPO = OWNER + "/" + REPO_NAME
OTHER_TARGET = OWNER + "/synth-other-lib"
REPO_NODE = "R_SYNTH_FIXTURE_REPO"
REPO_URL = "https://github.com/" + REPO
INTERVAL = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
CAPTURED_AT = "2026-09-10T00:00:00Z"
EPOCH = datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()
HEAD_SHA = "4" * 40
PATTERN_ID = "PAT-1"
PATTERN_CLUE = "retry_with_jitter"
LOCUS = "src/retry.py retry_with_jitter"
CODE_HIT_URL = REPO_URL + "/blob/" + HEAD_SHA + "/src/retry.py"
CONTRIBUTING_TEXT = SYNTHETIC + " contributing guide. Open an issue before large changes.\n"
SYNTHETIC_LIMITATION = SYNTHETIC + ": hand-written test data, not a model or human judgment."

PULLS = {
    101: {"node_id": "PR_SYNTH_FIXTURE_101", "title": "Synthetic fixture: jittered retry backoff",
          "body": SYNTHETIC + ". Replace the fixed sleep with jittered exponential backoff between retries.",
          "quote": "jittered exponential backoff between retries",
          "created_at": "2026-09-01T09:00:00Z", "closed_at": "2026-09-03T10:00:00Z", "updated_at": "2026-09-03T10:05:00Z",
          "path": "src/retry.py", "patch": "@@ -1,2 +1,2 @@\n-    time.sleep(1)\n+    time.sleep(retry_with_jitter(attempt))",
          "base_sha": "1" * 40, "head_sha": "2" * 40, "merge_sha": "3" * 40,
          "commit_message": "Use jittered backoff in the retry loop"},
    102: {"node_id": "PR_SYNTH_FIXTURE_102", "title": "Synthetic fixture: cap the retry delay",
          "body": SYNTHETIC + ". Cap the retry delay while keeping jittered exponential backoff.",
          "quote": "keeping jittered exponential backoff",
          "created_at": "2026-09-04T09:00:00Z", "closed_at": "2026-09-05T12:00:00Z", "updated_at": "2026-09-05T12:05:00Z",
          "path": "src/retry.py", "patch": "@@ -3,1 +3,1 @@\n-    delay = base * 2 ** attempt\n+    delay = min(cap, retry_with_jitter(attempt))",
          "base_sha": "5" * 40, "head_sha": "6" * 40, "merge_sha": "7" * 40,
          "commit_message": "Cap the jittered retry delay"},
}


def pull_url(number):
    return "{0}/pull/{1}".format(REPO_URL, number)


def base_request(**changes):
    request = {
        "schema_version": "1.0.0",
        "mode": "offline-fixture",
        "source": {"kind": "recent-closed-prs", "repositories": [REPO],
                   "interval": {"start_at": INTERVAL[0], "end_at": INTERVAL[1]}, "outcome": "all",
                   "max_per_repository": 5, "request_budget": 200, "reference_repositories": []},
        "target": {"repositories": [REPO], "max_candidates_per_repo": 5, "max_candidates_total": 20,
                   "discover_request_budget": 60, "locus_request_budget": 40, "allow_clone": False},
        "budget": {"total_api_requests": 400, "max_elapsed_seconds": None},
        "static_only": True,
        "provenance": {"model": "unknown", "effort": "unknown"},
    }
    for dotted, value in changes.items():
        node = request
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return request


# -- collector ---------------------------------------------------------------


def _query_key(params):
    return tuple(sorted((str(name), str(value)) for name, value in (params or {}).items()))


class CollectorOpener:
    """Exact (path, query) routes for the collector's injected urllib opener."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.unmatched = []

    def open(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        self.requests.append({"method": request.get_method(), "url": request.full_url, "data": request.data,
                              "headers": {k.lower(): v for k, v in request.header_items()}})
        label = request.get_method() + " " + request.full_url
        if parts.scheme != "https" or parts.netloc != "api.github.com" or request.get_method() != "GET" or request.data is not None:
            self.unmatched.append(label)
            raise MissingFixture("collector fixture serves only payload-free GitHub GET: " + label)
        key = (parts.path, tuple(sorted((n, v) for n, v in pairs if n != "per_page")))
        if key not in self.routes:
            self.unmatched.append(label)
            raise MissingFixture("no collector fixture for " + label)
        status, payload = self.routes[key]
        headers = {"Content-Type": "application/json", "ETag": '"synthetic-%d"' % len(self.requests)}
        body = json.dumps(payload)
        if status >= 300:
            raise collector_tests.http_error(status, headers, body)(request)
        return collector_tests.FakeHttpResponse(status, headers, body, url=request.full_url)


def repository_payload():
    return {"full_name": REPO, "node_id": REPO_NODE, "default_branch": "main", "archived": False, "disabled": False,
            "fork": False, "has_issues": True, "visibility": "public", "pushed_at": "2026-09-06T00:00:00Z",
            "license": {"spdx_id": "MIT"}, "description": SYNTHETIC + " repository"}


def _pull_payload(number):
    spec = PULLS[number]
    return {"node_id": spec["node_id"], "number": number, "html_url": pull_url(number), "title": spec["title"],
            "state": "closed", "merged_at": spec["closed_at"], "created_at": spec["created_at"],
            "closed_at": spec["closed_at"], "updated_at": spec["updated_at"], "body": spec["body"],
            "base": {"sha": spec["base_sha"], "repo": {"node_id": REPO_NODE}}, "head": {"sha": spec["head_sha"]},
            "merge_commit_sha": spec["merge_sha"], "labels": [{"name": "synthetic-fixture"}], "changed_files": 1,
            "commits": 1, "user": {"login": "synthetic-contributor", "node_id": "U_SYNTH_CONTRIBUTOR"},
            "author_association": "CONTRIBUTOR"}


def collector_routes(*, failing_files=(), search_status=200):
    routes = {}

    def add(path, params, payload, status=200):
        routes[(path, _query_key(params))] = (status, payload)

    root = "/repos/" + REPO
    add("/versions", None, [API_VERSION])
    add(root, None, repository_payload())
    add(root + "/license", None, {"html_url": REPO_URL + "/blob/main/LICENSE", "license": {"spdx_id": "MIT"}})
    for number, spec in sorted(PULLS.items()):
        pull = root + "/pulls/%d" % number
        first = {"page": 1}
        add(pull, None, _pull_payload(number))
        if number in failing_files:
            add(pull + "/files", first, {"message": "Server Error"}, status=500)
        else:
            add(pull + "/files", first, [{"filename": spec["path"], "status": "modified", "additions": 1,
                                          "deletions": 1, "patch": spec["patch"]}])
        add(pull + "/commits", first, [{"sha": spec["head_sha"], "parents": [{"sha": spec["base_sha"]}],
                                        "commit": {"message": spec["commit_message"],
                                                   "author": {"name": "Synthetic Contributor",
                                                              "email": "synthetic@example.invalid",
                                                              "date": spec["created_at"]}},
                                        "author": {"login": "synthetic-contributor", "node_id": "U_SYNTH_CONTRIBUTOR"}}])
        add(root + "/issues/%d/comments" % number, first, [])
        add(pull + "/reviews", first, [{"id": number * 10, "body": SYNTHETIC + " review: looks fine.",
                                        "state": "APPROVED", "user": {"login": "synthetic-maintainer"},
                                        "html_url": pull_url(number) + "#pullrequestreview-%d" % (number * 10),
                                        "submitted_at": spec["closed_at"]}])
        add(pull + "/comments", first, [])
        add(root + "/issues/%d/timeline" % number, first, [{"event": "merged", "actor": {"login": "synthetic-maintainer"},
                                                             "created_at": spec["closed_at"]}])
    interval = collector.resolve_interval(start_at=INTERVAL[0], end_at=INTERVAL[1], start_date=None, end_date=None,
                                          recent_days=None, timezone_name="UTC", as_of=None)
    query = collector.build_closed_query(REPO, interval, "all")
    items = [{"node_id": PULLS[n]["node_id"], "number": n, "title": PULLS[n]["title"], "closed_at": PULLS[n]["closed_at"],
              "html_url": pull_url(n), "pull_request": {"html_url": pull_url(n)}} for n in sorted(PULLS)]
    if search_status == 200:
        add("/search/issues", {"q": query, "page": 1}, {"total_count": len(items), "incomplete_results": False, "items": items})
    else:
        add("/search/issues", {"q": query, "page": 1}, {"message": "Validation Failed"}, status=search_status)
    return routes, interval


def run_collector(directory, *, failing_files=(), search_status=200, request_budget=200, captured_at=CAPTURED_AT):
    """The production collector ``collect`` over a fake opener; returns (run, opener, paths)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    routes, interval = collector_routes(failing_files=failing_files, search_status=search_status)
    opener = CollectorOpener(routes)
    client = collector.GhApiClient(api_version=API_VERSION, budget=collector.RequestBudget(request_budget),
                                   sleeper=collector_tests.FakeSleep(), clock=lambda: EPOCH, opener=opener,
                                   token_provider=lambda: None)
    paths = {"corpus": directory / "corpus.json", "manifest": directory / "manifest.json",
             "inventory": directory / "inventory.md"}
    run = collector.collect(client, [REPO], interval, outcome="all", max_per_repository=5, request_budget=request_budget,
                            output=paths["corpus"], manifest_output=paths["manifest"],
                            markdown_output=paths["inventory"], captured_at=captured_at, api_version=API_VERSION)
    return run, opener, paths


# -- curated tracker collector ----------------------------------------------

curated = siblings.curated
TRACKER = OWNER + "/synth-tracker"
TRACKER_ISSUE = 7
EXTERNAL = OWNER + "/synth-external-lib"


def _tracker_comment(identifier, body, day):
    return {"id": identifier, "node_id": "IC_SYNTH_FIXTURE_%d" % identifier,
            "url": "https://api.github.com/repos/%s/issues/comments/%d" % (TRACKER, identifier),
            "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (TRACKER, TRACKER_ISSUE, identifier),
            "user": {"login": "synthetic-submitter-%d" % identifier}, "author_association": "NONE",
            "created_at": "2026-09-0%dT00:00:00Z" % day, "updated_at": "2026-09-0%dT00:00:00Z" % day,
            "body": body}


def curated_routes(*, failing_files=(), external=None):
    """The recent-closed world's pull request routes plus one synthetic tracker Issue.

    ``external`` adds a submission in a repository outside the request allow list:
    ``"hydrated"`` serves it fully, ``"missing"`` answers its repository with 404.
    """
    routes, _ = collector_routes(failing_files=failing_files)
    routes = {key: value for key, value in routes.items() if key[0] != "/search/issues"}
    issue_path = "/repos/%s/issues/%d" % (TRACKER, TRACKER_ISSUE)
    comments = [_tracker_comment(9001, SYNTHETIC + " submission: " + pull_url(101), 1),
                _tracker_comment(9002, SYNTHETIC + " submission: " + pull_url(102), 2),
                _tracker_comment(9003, "Example: " + REPO_URL + "/pull/999", 3)]
    if external is not None:
        comments.append(_tracker_comment(9004, SYNTHETIC + " submission: https://github.com/%s/pull/5" % EXTERNAL, 4))
        if external == "missing":
            routes[("/repos/" + EXTERNAL, ())] = (404, {"message": "Not Found"})
        else:
            for (path, query), value in list(routes.items()):
                if path.startswith("/repos/%s/" % REPO) and "/101" in path or path in ("/repos/" + REPO, "/repos/%s/license" % REPO):
                    routes[(path.replace(REPO, EXTERNAL).replace("/101", "/5"), query)] = value
            routes[("/repos/" + EXTERNAL, ())] = (200, dict(repository_payload(), full_name=EXTERNAL,
                                                            node_id="R_SYNTH_EXTERNAL_REPO"))
            pull = dict(_pull_payload(101), number=5, node_id="PR_SYNTH_EXTERNAL_5",
                        html_url="https://github.com/%s/pull/5" % EXTERNAL)
            pull["base"] = {"sha": pull["base"]["sha"], "repo": {"node_id": "R_SYNTH_EXTERNAL_REPO"}}
            routes[("/repos/%s/pulls/5" % EXTERNAL, ())] = (200, pull)
    routes[(issue_path, ())] = (200, {"id": 7000, "node_id": "I_SYNTH_TRACKER_7", "number": TRACKER_ISSUE,
                                      "url": "https://api.github.com" + issue_path,
                                      "html_url": "https://github.com/%s/issues/%d" % (TRACKER, TRACKER_ISSUE),
                                      "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-04T00:00:00Z",
                                      "body": SYNTHETIC + " tracker: post one pull request link per comment."})
    routes[(issue_path + "/comments", (("page", "1"),))] = (200, comments)
    return routes


def curated_request(**changes):
    request = base_request()
    request["source"] = {"kind": "curated-tracker", "tracker": {"repository": TRACKER, "issue_number": TRACKER_ISSUE},
                         "repositories": [REPO], "max_prs": 5, "request_budget": 200, "reference_repositories": []}
    for dotted, value in changes.items():
        node = request
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return request


def run_curated(directory, *, issue=TRACKER_ISSUE, max_prs=5, request_budget=200, failing_files=(), external=None,
                resume_from=None, captured_at=CAPTURED_AT):
    """The production curated ``collect`` over a fake opener; returns (run, opener, paths).

    ``resume_from`` (paths of an earlier run) resumes that run's last record with
    the same request, the way the collector's ``--resume-run-id`` does.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    opener = CollectorOpener(curated_routes(failing_files=failing_files, external=external))
    transport = curated.load_sibling()
    client = transport.GhApiClient(api_version=API_VERSION, budget=transport.RequestBudget(request_budget),
                                   sleeper=collector_tests.FakeSleep(), clock=lambda: EPOCH, opener=opener,
                                   token_provider=lambda: None)
    paths = {"corpus": directory / "corpus.json", "manifest": directory / "manifest.json"}
    existing = {}
    if resume_from is not None:
        manifest = read_json(resume_from["manifest"])
        existing = {"existing_corpus": read_json(resume_from["corpus"]), "existing_manifest": manifest,
                    "resume_run_id": manifest["records"][-1]["run_id"]}
    run = curated.collect(client, TRACKER, issue, max_prs=max_prs, request_budget=request_budget,
                          output=paths["corpus"], manifest_output=paths["manifest"], timestamp=captured_at, **existing)
    return run, opener, paths


# -- analysis (synthetic stand-in for model extraction) ------------------------


def synthetic_analysis(corpus, *, with_pattern=True, quote_override=None):
    revision = validate_corpus._canonical_revision()
    by_number = {record["pull_request"]["number"]: record for record in corpus["records"]}
    records = []
    for record in corpus["records"]:
        number = record["pull_request"]["number"]
        url = record["pull_request"]["url"]
        analysis = {
            "change_summary": {"value": SYNTHETIC + ": replaces a fixed retry delay.", "basis": "fact", "evidence_links": [url]},
            "motivation": {"value": None, "basis": "unknown", "evidence_links": []},
            "review_judgment": {"value": SYNTHETIC + ": approved.", "basis": "fact", "evidence_links": [url]},
            "closure_reason": {"value": "merged", "basis": "fact", "evidence_links": [url]},
            "files_changed": [PULLS[number]["path"]],
            "test_evidence": [],
            "pattern_ids": [PATTERN_ID] if with_pattern else [],
            "evidence_links": [url],
            "evidence_manifest": {"source": "oss-workflow-synthetic-fixture", "record_key": record["record_key"]},
            "license_spdx": record["license"]["spdx_id"],
            "provenance_mode": "independent-reimplementation",
            "confidence": {"level": "low", "evidence": ['%s "%s"' % (record["pr_id"], PULLS[number]["quote"])],
                           "limitations": [SYNTHETIC_LIMITATION]},
            "superseded_by": None,
        }
        enriched = deepcopy(record)
        enriched["analysis"] = analysis
        enriched["analysis_history"] = list(record.get("analysis_history") or []) + [{
            "revision": revision, "generated_at": CAPTURED_AT,
            "evidence_manifest": deepcopy(analysis["evidence_manifest"]), "conclusion": deepcopy(analysis)}]
        records.append(enriched)
    patterns = []
    if with_pattern:
        quotes = ['%s "%s"' % (by_number[n]["pr_id"], quote_override or PULLS[n]["quote"]) for n in sorted(PULLS)]
        pattern = {
            "pattern_id": PATTERN_ID,
            "description": SYNTHETIC + " pattern: retry loops use jittered exponential backoff.",
            "generated_by": {"name": validate_corpus.ANALYZER_NAME, "revision": revision},
            "evidence_pr_ids": [by_number[n]["pr_id"] for n in sorted(PULLS)],
            "applicability": [SYNTHETIC + ": retry loops with a fixed sleep."],
            "counterconditions": [SYNTHETIC + ": callers that need deterministic timing."],
            "search_clues": [PATTERN_CLUE],
            "expected_tests": [SYNTHETIC + ": the delay stays within its cap."],
            "maintainer_judgment_required": [SYNTHETIC + ": whether jitter is acceptable."],
            "source_licenses": [{"pr_id": by_number[n]["pr_id"], "spdx_id": by_number[n]["license"]["spdx_id"]} for n in sorted(PULLS)],
            "provenance_mode": "independent-reimplementation",
            "confidence": {"level": "low", "evidence": quotes, "limitations": [SYNTHETIC_LIMITATION]},
            "superseded_by": None,
        }
        pattern["pattern_history"] = [{"revision": revision, "generated_at": CAPTURED_AT,
                                       "conclusion": {k: deepcopy(pattern[k]) for k in validate_corpus.PATTERN_KEYS}}]
        patterns.append(pattern)
    ledger = validate_corpus.EXTRACTION_LEDGER_PREFIX + SYNTHETIC + " written by the test fixture; no model extraction ran."
    return {"schema_version": "1.0.0", "generated_by": deepcopy(corpus["generated_by"]),
            "analysis_generated_by": {"name": validate_corpus.ANALYZER_NAME, "revision": revision},
            "records": records, "patterns": patterns, "limitations": [ledger, SYNTHETIC_LIMITATION]}


# -- verifier ----------------------------------------------------------------


class StrictReplies(dict):
    """A path mapping for the verifier tests' fake_opener without its default 404."""

    def __init__(self, replies):
        super().__init__(replies)
        self.unmatched = []

    def get(self, key, default=None):
        if key not in self:
            self.unmatched.append(key)
            raise MissingFixture("no verifier fixture for GET " + str(key))
        return dict.__getitem__(self, key)


def verifier_replies():
    reply = verifier_tests.fixture_response
    path = vc._request_path
    root = "/repos/" + REPO
    not_found = {"message": "Not Found"}
    content = CONTRIBUTING_TEXT.encode("utf-8")
    replies = {
        path(root, None): reply(repository_payload()),
        path(root + "/commits/main", None): reply({"sha": HEAD_SHA}),
        path(root + "/community/profile", None): reply({"files": {
            "contributing": {"html_url": REPO_URL + "/blob/main/CONTRIBUTING.md"}, "license": {"spdx_id": "MIT"},
            "readme": None, "code_of_conduct": None, "issue_template": None, "pull_request_template": None}}),
        path(root + "/private-vulnerability-reporting", None): reply({"enabled": False}),
        path("/repos/" + OWNER + "/.github", None): reply(not_found, status=404),
        path("/search/code", {"q": 'repo:%s "%s"' % (REPO, PATTERN_CLUE)}): reply({
            "total_count": 1, "incomplete_results": False, "items": [{"path": "src/retry.py", "html_url": CODE_HIT_URL}]}),
    }
    for policy_path in vc.POLICY_PATHS:
        key = path(root + "/contents/" + policy_path, {"ref": "main"})
        if policy_path == "CONTRIBUTING.md":
            replies[key] = reply({"encoding": "base64", "content": base64.b64encode(content).decode("ascii"),
                                  "size": len(content), "html_url": REPO_URL + "/blob/main/CONTRIBUTING.md"})
        else:
            replies[key] = reply(not_found, status=404)
    for clue in dict.fromkeys([PATTERN_CLUE] + vc._locus_clues(LOCUS)):
        for kind in ("issue", "pr"):
            for state in ("open", "closed"):
                query = 'repo:%s "%s" is:%s is:%s' % (REPO, clue, kind, state)
                replies[path("/search/issues", {"q": query})] = reply({"total_count": 0, "incomplete_results": False, "items": []})
    return replies


class VerifierWorld:
    def __init__(self):
        self.calls = []
        self.replies = StrictReplies(verifier_replies())
        self.opener = verifier_tests.fake_opener(self.calls, self.replies)

    def main(self, argv):
        return workflow.run_main(vc.main, argv, sleeper=collector_tests.FakeSleep(), clock=lambda: EPOCH, opener=self.opener)


def synthetic_assessment(discovery_record, pr_urls, status="unverified", status_reason="insufficient-evidence",
                         static_reproduction=False):
    """``static_reproduction`` adds the evidence a ``reproduced`` status needs; it is still synthetic."""
    hits = [hit["html_url"] for hit in discovery_record["code_search"]["hits"]]
    assessment = {
        "pattern_id": discovery_record["pattern_id"], "repository": discovery_record["repository"], "locus": LOCUS,
        "status": status, "status_reason": status_reason, "sensitivity": "normal",
        "summary": SYNTHETIC + ": test-written assessment, not a verified contribution candidate.",
        "impact": SYNTHETIC + ": no impact was assessed.",
        "readiness_checks": {key: {"result": "not-confirmed", "evidence_links": [], "note": SYNTHETIC + ": not assessed"}
                             for key in vc.READINESS_KEYS},
        "duplicate_verdict": {"matched_items": [], "judgment": SYNTHETIC + ": no duplicate judgment was made."},
        "ai_policy_status": "unknown", "disclosure_required": None, "private_evidence_reference": None,
        "evidence_links": hits + list(pr_urls),
        "execution_evidence": None, "reproduction": {"method": "none", "evidence_links": [], "public_steps": None},
        "policy_checks": {key: {"found": None, "source": None, "sha256": None, "assessment": SYNTHETIC + ": not reviewed",
                                "evidence_links": []} for key in vc.POLICY_CHECK_KEYS},
        "blocking_gaps": ["synthetic-fixture-not-a-real-candidate", "model-pattern-extraction-not-run"],
        "superseded_by": None,
    }
    if static_reproduction:
        # The fake code-search hit stands in for reading the locus; nothing was reproduced for real.
        assessment["reproduction"] = {"method": "static", "evidence_links": hits,
                                      "public_steps": [SYNTHETIC + ": read the fixture locus."]}
        assessment["readiness_checks"]["reproduction_evidence"] = {
            "result": "confirmed", "evidence_links": hits, "note": SYNTHETIC + ": fixture code-search hit"}
    return assessment


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# -- several collections of one request --------------------------------------

REPO_B = OWNER + "/synth-docs-lib"
REPO_C = OWNER + "/synth-third-lib"


def derive_bundle(source_paths, directory, repository, tag):
    """A second collection by re-labelling a collector run for another repository.

    Every repository name, node id, run id and request fingerprint changes, so
    the bundle is a distinct synthetic collection that the analyzer's own
    corpus validation still accepts. Nothing here talks to a collector.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = read_json(source_paths["manifest"])
    swaps = []
    for record in manifest["records"]:
        swaps.append((record["run_id"], "run-" + hashlib.sha256((record["run_id"] + tag).encode()).hexdigest()[:20]))
        swaps.append((record["request_fingerprint"],
                      "sha256:" + hashlib.sha256((record["request_fingerprint"] + tag).encode()).hexdigest()))
    paths = {}
    for name, filename in (("corpus", "corpus.json"), ("manifest", "manifest.json"), ("inventory", "inventory.md")):
        text = Path(source_paths[name]).read_text(encoding="utf-8")
        text = text.replace(REPO, repository).replace(REPO_NAME, repository.split("/")[1])
        text = text.replace("SYNTH_FIXTURE", "SYNTH_FIXTURE_" + tag.upper())
        for old, new in swaps:
            text = text.replace(old, new)
        paths[name] = directory / filename
        paths[name].write_text(text, encoding="utf-8")
    return paths


def derive_window(source_paths, directory, tag, *, repository=REPO, interval=None, outcome=None, keep=None, shift=0):
    """A collection of one window and outcome, re-labelled from a collector run.

    ``keep`` lists the corpus record positions this window holds (the fixture run
    holds two pull requests), ``interval`` is (start_at, end_at), and a
    closed-unmerged outcome turns the kept records into closed-unmerged pull
    requests. Node ids, run ids and fingerprints are new, so windows of one
    repository never share a pull request or a run. ``shift`` adds to every pull request
    number (the digest keys a pull request by repository and number).
    """
    paths = derive_bundle(source_paths, directory, repository, tag)
    if shift:
        for name in ("corpus", "inventory"):
            text = paths[name].read_text(encoding="utf-8")
            for number in sorted(PULLS, reverse=True):
                text = text.replace("/pull/%d" % number, "/pull/%d" % (number + shift))
            paths[name].write_text(text, encoding="utf-8")
    corpus, manifest = read_json(paths["corpus"]), read_json(paths["manifest"])
    if shift:
        for record in corpus["records"]:
            record["pull_request"]["number"] += shift
    if keep is not None:
        corpus["records"] = [corpus["records"][index] for index in keep]
    for record in manifest["records"]:
        if interval is not None:
            record["interval"]["start_at"], record["interval"]["end_at"] = interval
        if outcome is not None:
            record["outcome"] = outcome
    if outcome == "closed-unmerged":
        for record in corpus["records"]:
            record["pull_request"].update(normalized_state="closed-unmerged", merged_at=None, closure_reason="unknown")
    write_json(paths["corpus"], corpus)
    write_json(paths["manifest"], manifest)
    return paths
