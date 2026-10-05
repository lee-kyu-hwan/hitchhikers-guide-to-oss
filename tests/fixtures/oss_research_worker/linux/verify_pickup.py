#!/usr/bin/env python3
"""Check a result tar the host took from the guest (host side, Python 3.12+).

    python3 verify_pickup.py RESULT.tar CAP_BYTES DEST_DIR [--auto-observation]
    python3 verify_pickup.py RESULT.tar CAP_BYTES DEST_DIR --collect \
        --collector-sha256 HEX --wrapper-record RECORD.json \
        --upstream github-rest-via-relay|loopback-mock
    python3 verify_pickup.py RESULT.tar CAP_BYTES DEST_DIR --issues \
        --issue-collector-sha256 HEX --sibling-sha256 HEX --wrapper-record RECORD.json \
        --upstream github-rest-via-relay|loopback-mock
    python3 verify_pickup.py RESULT.tar CAP_BYTES DEST_DIR --candidate-reads \
        --stage discover|locus-search --verifier-sha256 HEX --relay-issues-sha256 HEX \
        --relay-sibling-sha256 HEX --wrapper-record RECORD.json \
        --upstream github-rest-via-relay|loopback-mock

--collect, --issues and --candidate-reads also take a last option
`--forbid-value-file FILE`: a file holding a secret value (the GitHub token file of the
run) that no member may contain. See "GitHub token marker" below.

A test fixture for the Linux evidence run, not product protection. The tar is
data only and never executed. Before anything is unpacked:

- a file of CAP_BYTES or more is taken as cut off and not parsed at all;
- it is opened as an uncompressed tar only ("r:"), so no decompression runs;
- every member is a regular file named once, from the result set, each at
  most MAX_MEMBER_BYTES, at most MAX_MEMBERS in all and their sizes together
  under CAP_BYTES; manifest.json and report.md are required.

Only then is it unpacked with tarfile's "data" filter into a new DEST_DIR, and
manifest.json must say succeeded and report.md must match report_sha256.
Prints one JSON line; exit 0 only when all hold.

--auto-observation is a separate, explicit contract for the model-auto run,
whose stdout is a stream (so the product records it as failed). The same
structure checks apply, with manifest.json and stdout.json required and no
report.md. The manifest must be exactly that failure: status failed, the
child's exit code 0, no supervisor failures, the one parser problem the
stream causes, permission mode auto and a clean credential check. Anything
else fails. The default contract is unchanged.

--collect is the non-AI collection step's contract (produced by the
collection entry dot_local/bin/executable_oss-research-collect). Fixed names, all required: corpus.json, collection-manifest.json,
inventory.md, transport.json, relay.log, run-manifest.json and
collection-envelope.json. The same structure checks apply first. Then every
JSON file must be one object (no duplicate keys, no NaN/Infinity) and all of:
- the envelope lists every other member with its exact size and sha256;
- run-manifest: succeeded, exit code 0, no failures or problems, the same
  relay sources and relay sha256 as the envelope; the adapter exit is 0;
  (a partial collection, below, leaves the entry's run record failed with the
  collector's exit 3 and the one problem "collection exit code 3");
- transport: srt-proxy, exit code 0, every request relayed, its count equal
  to the collection manifest's request_count, the collector path and sha256
  equal to the relay's ready "sibling" source, the relay port the envelope's;
- collection-manifest: one record, collection_status complete, the requested
  repositories, interval, outcome, cap and budget equal to the envelope's request, and
  request_count within the request budget;
- corpus: 1 to max_per_repo x repositories records, each from a requested
  repository;
- host inputs: the collector sha256 the host reviewed (from its own tree)
  equals the transport's and the relay's; the wrapper's own record (the root
  unit wrapper's JSON line) has exit code 0, no failures, the envelope's unit
  and an inner summary "collect" that succeeded;
- the envelope states its upstream ("github-rest-via-relay" from the
  production entry, "loopback-mock" when a test put a mock beside it) and it
  must equal the --upstream the host asks for: a live adoption asks for
  github-rest-via-relay, so a mock result is never taken as live. It is a
  statement, printed, never evidence of a real GitHub request.
Exit codes and counts must be JSON integers (not booleans or floats).
A collection the collector stopped partial (collection_status partial) is
accepted as a bound partial one only when every member says so: the adapter
exit 3, the run record failed with exit 3 and the single problem "collection
exit code 3", the transport exit 3, the unit wrapper exit 4 with no failures
and an inner summary that is not succeeded and carries that same run record.
Exit 0 only for a bound complete collection, 3 for a bound partial one, 1 for
anything else (an adapter or run failure included). Exit 0 or 3 says the
pickup checks held; it is not evidence that every matching PR was collected
(the cap, the budget and search limits stay).

--issues is the non-AI Issue collection step's contract (produced by
dot_local/bin/executable_oss-research-collect-issues and its adapter
executable_oss-research-relay-issues). Fixed names, all required:
corpus.json, collection-manifest.json, transport.json, relay.log,
run-manifest.json and collection-envelope.json. The same structure checks
apply first, then every JSON file must be one object, and all of:
- the envelope is the Issue entry's schema and lists every other member with
  its exact size and sha256; its request is inside the entry's own limits
  again (unique OWNER/NAME repositories, date field created|updated|closed,
  state open|closed|all but not closed+open, a forward whole-second UTC
  interval of at most 184 days, positive cap and budget, repositories x cap
  at most 20 Issues, budget at most 90);
- the run's status is the collector's: succeeded (adapter exit 0, collection
  complete, wrapper exit 0) or partial (adapter exit 3, collection partial,
  wrapper exit 5), no failures or problems, the envelope's relay;
- the relay's ready sources are exactly the roles issues and sibling; the
  transport's collector.issues and collector.sibling are those same
  {path, sha256}, and each sha256 is the one the host reviewed;
- transport: srt-proxy, the adapter's exit code, the envelope's relay port,
  as many requests as the manifest's request_count, each one delivered:
  "relayed", or "upstream-error" with an HTTP 4xx/5xx status (GitHub
  answered, as the anonymous GraphQL 401 does; counted per status in the
  output). A refused, timed-out, malformed or failed exchange fails;
- the host tree's own Issue collector (and its sibling) must be the one the
  host reviewed; it is loaded (host code, never the tar's) and its
  validate_issue_corpus and validate_issue_manifest must accept the data,
  and its IssueEvidence query sha256 must be the transport's;
- collection-manifest: one run whose filters (repositories, date field,
  state, no labels), interval, cap and budget are the request's,
  request_count within budget; the official validators check only the outer
  shape, these checks bind the content;
- corpus: an Issue corpus of well-formed records, each from a requested
  repository, keys unique, at most max_per_repo per repository, as many as
  the manifest selected;
- the wrapper record and the upstream as in --collect (inner mode
  collect-issues, its status the run's).
A value of the wrong JSON type fails the pickup (no traceback). Exit 0 only
for a bound complete collection, 3 for a bound partial one, 1 for anything
else. Exit 0 or 3 says the pickup checks held; it is not evidence that every
matching Issue was collected (the cap, the budget and search limits stay).

--candidate-reads is the non-AI candidate-verifier read step's contract (produced
by dot_local/bin/executable_oss-research-candidate-reads and its adapter
executable_oss-research-relay-verify). The host names the stage. Fixed names, all
required: discovery.json (discover) or locus-search.json (locus-search),
verification-manifest.json, transport.json, relay.log, run-manifest.json and
read-envelope.json. The same structure checks apply first, then every JSON file
must be one object, and all of:
- the envelope is the entry's schema for that stage and lists every other member
  with its exact size and sha256, and the input files (analysis.json, or
  discovery.json and assessment.json) with a size and a sha256; its request is
  inside the entry's own limits again (one OWNER/NAME repository and its owner's
  .github as the relay's repositories, 1..5 PAT-<n> patterns, candidate and clue
  caps 1..5, at most 5 clues of 200 characters, a request budget of 1..60);
- the run's status is the verifier's: succeeded (adapter exit 0, output status
  complete, wrapper exit 0) or partial (adapter exit 3, output status partial,
  wrapper exit 5), no failures or problems, the envelope's request, inputs and relay;
- the relay's ready sources are exactly the roles issues and sibling, each the
  sha256 the host reviewed;
- transport: srt-proxy, this stage, the adapter's exit code, the envelope's relay
  port, no credential, the verifier's {path, sha256} equal to the sha256 the host
  reviewed (the host tree's own verifier must be that file), and every request
  delivered: "relayed", or "upstream-error" with an HTTP 4xx/5xx status (the
  anonymous code search's 401 is one). A refused, timed-out, malformed or failed
  exchange fails; the number of requests must equal what the verifier consumed
  and stay within the request budget;
- verification-manifest: one run of this stage whose budget is the request's and
  whose status is the output's; the output's revision is the host verifier's
  compute_revision(); discover: the analysis sha256 is the envelope's input, the
  repository, patterns and budget are the request's, no clone, every record is for
  the repository and a requested pattern within the caps; locus-search: the
  discovery sha256 is the envelope's input, every search is for the repository
  and uses at most the clue cap;
- REST static reads (#256), only when the discover request carries static_reads
  "rest" and static_dirs (1..3 plain content-path directories, at most 3 deep, a
  budget of at least 26 + 1 + directories + 30): the output inputs name the same
  mode and directories; every record's static_search is rest-contents at the
  record's head sha; its files, hits, listings and absent paths are README.md, a
  requested directory or a .md file directly inside one; excerpts are one line of
  at most 200 characters, files at most 256 KiB, link lists at most 200, and no
  "content" key is kept; every contents request in the transport is a policy
  path, README.md, a requested directory or a .md file directly inside one. A
  default request must have no static fields in the output inputs;
- the wrapper record and the upstream as in --collect (inner mode candidate-reads,
  its stage and status the run's).
A value of the wrong JSON type fails the pickup (no traceback). Exit 0 only for a
bound complete step, 3 for a bound partial one, 1 for anything else. Exit 0 or 3
says the pickup checks held; it is not evidence that GitHub was read: the upstream
is a statement, and a mock result is refused when the host asks for the live one.

GitHub token marker (the three contracts above). The entry hands an optional read-only
GitHub token FILE to the relay only; its envelope and run manifest then both carry
"github_token": {"authenticated": bool, "token_file_sha256": hex64 or null} and nothing else.
The pickup requires exactly that object, identical in both files (authenticated true needs the
sha256 of the file, false needs null: no value, prefix or length), prints it as "authenticated"
and "token_file_sha256", and fails the pickup when it is missing or malformed. It also fails when
the relay log, the transport record, the run manifest or the envelope holds a credential-shaped
value (a GitHub token shape or a bearer value; corpus and discovery members hold remote text and
are not shape-scanned), and, with --forbid-value-file, when ANY member holds that file's exact
value. The file must be a regular file of at most 4096 bytes holding at least 8 characters; one
that cannot be used fails the pickup (it is never skipped). Messages never include the value.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
from urllib.parse import unquote, urlsplit

RESULT_FILES = {"manifest.json", "report.md", "stdout.json", "stderr.log", "relay.log"}
REQUIRED = {"manifest.json", "report.md"}
MAX_MEMBERS = 16
MAX_MEMBER_BYTES = 16 * 1024 * 1024
AUTO_REQUIRED = {"manifest.json", "stdout.json"}
AUTO_PROBLEMS = ["stdout is not a JSON result"]
COLLECT_FILES = {"corpus.json", "collection-manifest.json", "inventory.md", "transport.json",
                 "relay.log", "run-manifest.json", "collection-envelope.json"}
COLLECT_REQUIRED = COLLECT_FILES


def _unique(pairs):
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("duplicate key")
    return document


def _refuse_constant(name):
    raise ValueError("non-standard JSON constant " + name)


def _zero(value) -> bool:
    return type(value) is int and value == 0


def _exit(value, expected: int) -> bool:
    return type(value) is int and value == expected


def _count(value) -> bool:
    return type(value) is int and value > 0


# The upstream the envelope states: the production entry's relay to GitHub, or a
# test's loopback mock. A statement only: printed, never evidence of a request.
UPSTREAMS = ("github-rest-via-relay", "loopback-mock")

# A collection that stopped partial (the collector's exit 3) leaves the entry's own run
# record "failed" with that exit and this one problem, and the unit wrapper exits 4.
PARTIAL_COLLECT_PROBLEM = "collection exit code 3"
PARTIAL_COLLECT_WRAPPER_EXIT = 4


def collect_problems(dest: Path, result: dict, host: dict) -> list:
    """The --collect cross-checks over an unpacked, structure-checked tar."""
    problems, docs = [], {}
    for name in sorted(n for n in COLLECT_FILES if n.endswith(".json")):
        try:
            doc = json.loads((dest / name).read_text(encoding="utf-8"), object_pairs_hook=_unique,
                             parse_constant=_refuse_constant)
        except (ValueError, UnicodeError) as error:
            problems.append(f"{name} is not valid JSON: {type(error).__name__}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{name} is not one JSON object")
            continue
        docs[name] = doc
    if problems:
        return problems
    envelope, run, transport = (docs["collection-envelope.json"], docs["run-manifest.json"],
                                docs["transport.json"])
    collection, corpus = docs["collection-manifest.json"], docs["corpus.json"]
    runs = collection.get("records")
    partial = (isinstance(runs, list) and len(runs) == 1 and isinstance(runs[0], dict)
               and runs[0].get("collection_status") == "partial")
    result["collection_status"] = "partial" if partial else "complete"
    status_exit = 3 if partial else 0

    def check(ok, message):
        if not ok:
            problems.append(message)
        return ok

    files = envelope.get("files")
    others = COLLECT_FILES - {"collection-envelope.json"}
    if check(isinstance(files, dict) and set(files) == others, "envelope does not list exactly the members"):
        for name in sorted(others):
            data = (dest / name).read_bytes()
            entry = files[name] if isinstance(files[name], dict) else {}
            check(entry.get("bytes") == len(data) and entry.get("sha256") == hashlib.sha256(data).hexdigest(),
                  f"{name} does not match the envelope")
    request = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    relay = envelope.get("relay") if isinstance(envelope.get("relay"), dict) else {}
    repositories = request.get("repositories")
    check(isinstance(repositories, list) and repositories and all(isinstance(r, str) for r in repositories),
          "envelope request has no repositories")
    check(_exit(envelope.get("adapter_exit"), status_exit),
          "adapter exit is not the integer %d" % status_exit)
    upstream = envelope.get("upstream")
    check(upstream in UPSTREAMS, "envelope does not state a known upstream")
    check(upstream == host["upstream"], "envelope upstream is not the one the host asked for")
    result["upstream"] = upstream if isinstance(upstream, str) else None
    run_relay = run.get("relay") if isinstance(run.get("relay"), dict) else {}
    if partial:
        check(run.get("status") == "failed" and _exit(run.get("exit_code"), 3) and run.get("failures") == []
              and run.get("problems") == [PARTIAL_COLLECT_PROBLEM],
              "run manifest is not the partial collection's (failed, exit 3, one problem)")
    else:
        check(run.get("status") == "succeeded" and _zero(run.get("exit_code")) and run.get("failures") == []
              and run.get("problems") == [], "run manifest did not succeed")
    check(run_relay.get("sources") == relay.get("sources") and run_relay.get("relay_sha256")
          == relay.get("relay_sha256") and run_relay.get("port") == relay.get("port"),
          "run manifest relay differs from the envelope")
    sibling = [s for s in relay.get("sources") or [] if isinstance(s, dict) and s.get("role") == "sibling"]
    collector = transport.get("collector") if isinstance(transport.get("collector"), dict) else {}
    check(len(sibling) == 1 and collector.get("sha256") == sibling[0].get("sha256")
          and collector.get("path") == sibling[0].get("path"),
          "transport collector is not the relay's ready sibling source")
    requests = transport.get("requests")
    check(transport.get("transport") == "srt-proxy", "transport record does not say srt-proxy")
    check(_exit(transport.get("exit_code"), status_exit),
          "transport exit code is not the integer %d" % status_exit)
    check(collector.get("sha256") == host["collector_sha256"],
          "transport collector is not the collector the host reviewed")
    try:
        wrapper = json.loads(host["wrapper_record"].read_text(encoding="utf-8").strip().splitlines()[-1],
                             object_pairs_hook=_unique, parse_constant=_refuse_constant)
        inner = json.loads(str(wrapper.get("stdout", "")).strip().splitlines()[-1],
                           object_pairs_hook=_unique, parse_constant=_refuse_constant)
    except (OSError, ValueError, UnicodeError, IndexError, AttributeError):
        wrapper, inner = None, None
    check(isinstance(wrapper, dict) and isinstance(inner, dict), "wrapper record is unreadable")
    wrapper, inner = wrapper or {}, inner or {}
    wrapper_exit = PARTIAL_COLLECT_WRAPPER_EXIT if partial else 0
    check(_exit(wrapper.get("exit_code"), wrapper_exit) and wrapper.get("failures") == [],
          "the unit wrapper did not end with exit code %d and no failures" % wrapper_exit)
    check(isinstance(wrapper.get("unit"), str) and wrapper.get("unit") == envelope.get("unit"),
          "wrapper unit is not the envelope's unit")
    if partial:
        check(inner.get("mode") == "collect" and inner.get("succeeded") is False
              and inner.get("manifest") == {"status": "failed", "exit_code": 3, "failures": [],
                                            "problems": [PARTIAL_COLLECT_PROBLEM]},
              "wrapper inner summary is not a partial collect")
    else:
        check(inner.get("mode") == "collect" and inner.get("succeeded") is True,
              "wrapper inner summary is not a succeeded collect")
    check(transport.get("relay_base_url") == f"http://127.0.0.1:{relay.get('port')}",
          "transport relay URL is not the envelope's relay")
    check(isinstance(requests, list) and requests
          and all(isinstance(r, dict) and r.get("outcome") == "relayed" for r in requests),
          "not every transport request was relayed")
    records = collection.get("records")
    record = {}
    if check(isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict),
             "collection manifest does not have exactly one record"):
        record = records[0]
        interval = record.get("interval") if isinstance(record.get("interval"), dict) else {}
        count = record.get("request_count")
        check(record.get("collection_status") in ("complete", "partial"), "collection is not complete")
        check(record.get("repositories_requested") == repositories, "requested repositories differ")
        check(interval.get("start_at") == request.get("start_at") and interval.get("end_at")
              == request.get("end_at"), "collection interval differs from the request")
        check(record.get("outcome") == request.get("outcome")
              and _count(record.get("max_per_repository")) and _count(record.get("request_budget"))
              and record.get("max_per_repository") == request.get("max_per_repo")
              and record.get("request_budget") == request.get("request_budget"),
              "collection outcome, cap or budget differs from the request")
        check(_count(count) and _count(request.get("request_budget"))
              and count <= request["request_budget"], "request count is outside the budget")
        check(isinstance(requests, list) and count == len(requests),
              "transport requests differ from the collection request count")
    corpus_records = corpus.get("records")
    limit = request.get("max_per_repo")
    docs_only = request.get("docs_only")
    check(docs_only is None or docs_only in DOCS_ONLY_MODES, "envelope request names an unknown docs filter")
    check(record.get("docs_only") == docs_only,
          "collection manifest docs filter differs from the request")
    docs_ok = _check_docs_filter(record, repositories, docs_only, check)
    empty = isinstance(corpus_records, list) and not corpus_records
    if empty and not (docs_ok and _empty_collection_is_explained(record, repositories, docs_only)):
        problems.append("corpus is empty but the manifest does not say that nothing was matched or selected")
    if check((empty or (isinstance(corpus_records, list) and corpus_records)) and _count(limit)
             and isinstance(repositories, list) and len(corpus_records) <= limit * len(repositories),
             "corpus record count is outside 0..max_per_repo x repositories" if empty
             else "corpus record count is outside 1..max_per_repo x repositories"):
        wanted = {r.lower() for r in repositories}
        for item in corpus_records:
            repository = item.get("repository") if isinstance(item, dict) else None
            name = repository.get("full_name") if isinstance(repository, dict) else None
            check(isinstance(name, str) and name.lower() in wanted, "a corpus record is from another repository")
    result["records"] = len(corpus_records) if isinstance(corpus_records, list) else None
    return problems


DOCS_ONLY_MODES = ("title", "label")
REPOSITORY_STATUSES = ("collected", "no-results")


def _plain_count(value) -> bool:
    return type(value) is int and value >= 0


def _check_docs_filter(record: dict, repositories, docs_only, check) -> bool:
    """The docs filter's counts, when the request names one: per repository mode, rule and arithmetic.

    Without a filter in the request no repository may carry one. Returns whether every check held."""
    entries = record.get("repositories") if isinstance(record, dict) else None
    ok = True
    for entry in entries if isinstance(entries, list) else []:
        entry = entry if isinstance(entry, dict) else {}
        docs = entry.get("docs_filter")
        if docs_only is None:
            ok &= bool(check(docs is None, "a repository states a docs filter the request did not ask for"))
            continue
        good = isinstance(docs, dict) and docs.get("mode") == docs_only and isinstance(docs.get("rule"), str)
        counts = [docs.get(name) for name in ("search_matched", "hydrated", "passed", "dropped")] \
            if isinstance(docs, dict) else []
        good = good and len(counts) == 4 and all(_plain_count(value) for value in counts)
        if good:
            matched, hydrated, passed, dropped = counts
            good = hydrated == passed + dropped and hydrated <= matched and entry.get("selected_count") == passed
            good = good and entry.get("matched_count") == matched
        ok &= bool(check(good, "a repository's docs filter counts or mode do not fit the request and the manifest"))
    if docs_only is not None and isinstance(repositories, list):
        found = [entry.get("repository") for entry in entries if isinstance(entry, dict)] \
            if isinstance(entries, list) else []
        ok &= bool(check(sorted(found) == sorted(repositories),
                         "the docs filter is not recorded for exactly the requested repositories"))
    return ok


def _empty_collection_is_explained(record: dict, repositories, docs_only) -> bool:
    """True when an empty corpus is a normal complete result: every requested repository's entry says
    nothing was selected, and why (no search match, or the docs filter passed none)."""
    entries = record.get("repositories") if isinstance(record, dict) else None
    if not isinstance(entries, list) or not isinstance(repositories, list) or len(entries) != len(repositories):
        return False
    seen = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("collection_status") not in REPOSITORY_STATUSES:
            return False
        seen.append(entry.get("repository"))
        if not (_plain_count(entry.get("matched_count")) and _plain_count(entry.get("selected_count"))
                and entry["selected_count"] == 0):
            return False
        if entry["matched_count"] == 0 and entry["collection_status"] == "no-results":
            continue
        docs = entry.get("docs_filter")
        if not (docs_only is not None and isinstance(docs, dict) and docs.get("passed") == 0
                and entry["collection_status"] == "collected"):
            return False
    return sorted(seen) == sorted(repositories)


ISSUE_FILES = {"corpus.json", "collection-manifest.json", "transport.json", "relay.log",
               "run-manifest.json", "collection-envelope.json"}
ISSUE_ENVELOPE = "oss-research-issue-collection-envelope/1"
ISSUE_ENTRY = "dot_local/bin/executable_oss-research-collect-issues"
ISSUE_SKILL = "collecting-open-source-issues"
ISSUE_LIMITS = {"issues": 20, "request_budget": 90, "interval_days": 184}
ISSUE_DATE_FIELDS = ("created", "updated", "closed")
ISSUE_STATES = ("open", "closed", "all")
ISSUE_REQUEST_FIELDS = {"repositories", "date_field", "state", "start_at", "end_at", "max_per_repo",
                        "request_budget"}
# The host's own reviewed tree: this file is tests/fixtures/oss_research_worker/linux/.
HOST_SKILLS = Path(__file__).resolve().parents[4] / "dot_codex/skills"
HOST_ISSUE_COLLECTOR = HOST_SKILLS / "collecting-open-source-issues/scripts/collect_open_source_issues.py"
HOST_SIBLING = HOST_SKILLS / "collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py"
# run status -> (adapter exit, collection_status, wrapper exit)
ISSUE_STATUSES = {"succeeded": (0, "complete", 0), "partial": (3, "partial", 5)}
ISSUE_EXIT_PARTIAL = 3


def json_documents(dest: Path, names) -> tuple[dict, list]:
    docs, problems = {}, []
    for name in sorted(n for n in names if n.endswith(".json")):
        try:
            doc = json.loads((dest / name).read_text(encoding="utf-8"), object_pairs_hook=_unique,
                             parse_constant=_refuse_constant)
        except (ValueError, UnicodeError) as error:
            problems.append(f"{name} is not valid JSON: {type(error).__name__}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{name} is not one JSON object")
            continue
        docs[name] = doc
    return docs, problems


def wrapper_documents(path: Path) -> tuple[dict | None, dict | None]:
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1],
                             object_pairs_hook=_unique, parse_constant=_refuse_constant)
        inner = json.loads(str(wrapper.get("stdout", "")).strip().splitlines()[-1],
                           object_pairs_hook=_unique, parse_constant=_refuse_constant)
    except (OSError, ValueError, UnicodeError, IndexError, AttributeError):
        return None, None
    return (wrapper, inner) if isinstance(wrapper, dict) and isinstance(inner, dict) else (None, None)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def host_issue_collector(host: dict):
    """The host tree's Issue collector, loaded only when it is the reviewed one.

    Its own validate_issue_corpus and validate_issue_manifest then check the
    unpacked data (the official shape checks), and its fixed IssueEvidence
    query is the one the transport must name. Returns (module, problem)."""
    path = HOST_ISSUE_COLLECTOR
    sibling = HOST_SIBLING
    try:
        seen = {"issues": hashlib.sha256(path.read_bytes()).hexdigest(),
                "sibling": hashlib.sha256(sibling.read_bytes()).hexdigest()}
    except OSError as error:
        return None, f"the host tree's Issue collector is unreadable: {type(error).__name__}"
    if seen != {"issues": host["issue_collector_sha256"], "sibling": host["sibling_sha256"]}:
        return None, "the host tree's Issue collector or sibling is not the one the host reviewed"
    spec = importlib.util.spec_from_file_location("_verify_pickup_issue_collector", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if Path(module._sibling.__file__).resolve() != sibling.resolve():
        return None, "the host tree's Issue collector loaded another sibling"
    return module, None


def _instant(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) \
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value) else None
    except ValueError:
        return None


def request_problems(request: dict) -> list:
    """The Issue entry's own limits, checked again on the envelope's request."""
    problems = []
    repositories = request.get("repositories")
    good = (isinstance(repositories, list) and repositories
            and all(isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", r)
                    for r in repositories)
            and len({r.lower() for r in repositories}) == len(repositories))
    if not good:
        problems.append("envelope request has no unique OWNER/NAME repositories")
    if set(request) != ISSUE_REQUEST_FIELDS:
        problems.append("envelope request does not hold exactly the request fields")
    cap, budget = request.get("max_per_repo"), request.get("request_budget")
    if not (good and _count(cap) and _count(budget) and cap * len(repositories) <= ISSUE_LIMITS["issues"]
            and budget <= ISSUE_LIMITS["request_budget"]):
        problems.append("envelope request cap or budget is outside the entry's limits")
    date_field, state = request.get("date_field"), request.get("state")
    if not (isinstance(date_field, str) and date_field in ISSUE_DATE_FIELDS and isinstance(state, str)
            and state in ISSUE_STATES and (date_field, state) != ("closed", "open")):
        problems.append("envelope request date field or state is not one the entry accepts")
    start, end = _instant(request.get("start_at")), _instant(request.get("end_at"))
    if start is None or end is None or not start < end \
            or end - start > timedelta(days=ISSUE_LIMITS["interval_days"]):
        problems.append("envelope request interval is not a forward UTC interval within the entry's limit")
    return problems


def issue_problems(dest: Path, result: dict, host: dict) -> list:
    """The --issues cross-checks over an unpacked, structure-checked tar."""
    docs, problems = json_documents(dest, ISSUE_FILES)
    if problems:
        return problems
    try:
        return issue_bindings(dest, docs, result, host)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as error:
        # A value of the wrong JSON type (a list where a string belongs, say)
        # is a failed pickup, never a traceback.
        result["collection_status"] = None
        return [f"a member holds a malformed value: {type(error).__name__}"]


def issue_bindings(dest: Path, docs: dict, result: dict, host: dict) -> list:
    problems = []
    envelope, run, transport = (docs["collection-envelope.json"], docs["run-manifest.json"],
                                docs["transport.json"])
    manifest, corpus = docs["collection-manifest.json"], docs["corpus.json"]

    def check(ok, message):
        if not ok:
            problems.append(message)
        return ok

    check(envelope.get("schema") == ISSUE_ENVELOPE and envelope.get("entry") == ISSUE_ENTRY,
          "envelope is not the Issue entry's")
    files = envelope.get("files")
    others = ISSUE_FILES - {"collection-envelope.json"}
    if check(isinstance(files, dict) and set(files) == others, "envelope does not list exactly the members"):
        for name in sorted(others):
            data = (dest / name).read_bytes()
            entry = _dict(files[name])
            check(entry.get("bytes") == len(data) and entry.get("sha256") == hashlib.sha256(data).hexdigest(),
                  f"{name} does not match the envelope")
    request, relay = _dict(envelope.get("request")), _dict(envelope.get("relay"))
    problems += request_problems(request)
    repositories = request.get("repositories") if not problems else None
    cap, budget = request.get("max_per_repo"), request.get("request_budget")
    upstream = envelope.get("upstream")
    check(upstream in UPSTREAMS, "envelope does not state a known upstream")
    check(upstream == host["upstream"], "envelope upstream is not the one the host asked for")
    result["upstream"] = upstream if isinstance(upstream, str) else None
    status = run.get("status")
    expected = ISSUE_STATUSES.get(status) if isinstance(status, str) else None
    check(expected is not None and run.get("kind") == "issue-collection",
          "run manifest is not a succeeded or partial Issue collection")
    adapter_exit, collection_status, wrapper_exit = expected or (None, None, None)
    check(type(envelope.get("adapter_exit")) is int and envelope.get("adapter_exit") == adapter_exit
          and type(run.get("exit_code")) is int and run.get("exit_code") == adapter_exit,
          "adapter exit is not the run status's")
    check(run.get("failures") == [] and run.get("problems") == [], "run manifest has failures or problems")
    run_relay = _dict(run.get("relay"))
    check(all(run_relay.get(k) == relay.get(k) for k in ("sources", "relay_sha256", "port")),
          "run manifest relay differs from the envelope")
    # The relay's ready sources and the adapter's loaded sources, each {path, sha256}.
    sources = relay.get("sources") if isinstance(relay.get("sources"), list) else []
    ready = [s for s in sources if isinstance(s, dict) and set(s) == {"role", "path", "sha256"}]
    roles = {s["role"]: {"path": s["path"], "sha256": s["sha256"]} for s in ready if isinstance(s["role"], str)}
    check(len(sources) == 2 and len(ready) == 2 and set(roles) == {"issues", "sibling"},
          "relay sources are not exactly the Issue collector and its sibling")
    loaded = _dict(transport.get("collector"))
    check(set(loaded) == {"issues", "sibling", "issue_evidence_query_sha256"},
          "transport collector does not hold exactly the Issue collector's sources")
    for role, sha in (("issues", host["issue_collector_sha256"]), ("sibling", host["sibling_sha256"])):
        check(_dict(loaded.get(role)) == roles.get(role), f"transport collector {role} is not the relay's ready {role}")
        check(_dict(loaded.get(role)).get("sha256") == sha, f"transport collector {role} is not the one the host reviewed")
    check(transport.get("transport") == "srt-proxy", "transport record does not say srt-proxy")
    check(type(transport.get("exit_code")) is int and transport.get("exit_code") == adapter_exit,
          "transport exit code is not the adapter's")
    check(transport.get("relay_base_url") == f"http://127.0.0.1:{relay.get('port')}",
          "transport relay URL is not the envelope's relay")
    requests = transport.get("requests")
    # Delivered exchanges only: the relay carried the request and GitHub answered
    # (an HTTP error such as the anonymous GraphQL 401 included). A refusal,
    # timeout, broken envelope or transport failure is not a collection.
    delivered = isinstance(requests, list) and requests and all(
        isinstance(r, dict) and (r.get("outcome") == "relayed" or (
            r.get("outcome") == "upstream-error" and type(r.get("upstream_status")) is int
            and 400 <= r["upstream_status"] <= 599)) for r in requests)
    check(delivered, "a transport request was not delivered (refused, timed out, malformed or failed)")
    if delivered:
        errors = {}
        for r in requests:
            if r["outcome"] == "upstream-error":
                errors[str(r["upstream_status"])] = errors.get(str(r["upstream_status"]), 0) + 1
        result["upstream_errors"] = errors
    wrapper, inner = wrapper_documents(host["wrapper_record"])
    check(wrapper is not None, "wrapper record is unreadable")
    wrapper, inner = wrapper or {}, inner or {}
    check(type(wrapper.get("exit_code")) is int and wrapper.get("exit_code") == wrapper_exit
          and wrapper.get("failures") == [], "the unit wrapper's exit is not the run status's")
    check(isinstance(wrapper.get("unit"), str) and wrapper.get("unit") == envelope.get("unit"),
          "wrapper unit is not the envelope's unit")
    check(inner.get("mode") == "collect-issues" and inner.get("status") == status,
          "wrapper inner summary is not this Issue collection")
    collector, why = host_issue_collector(host)
    if check(collector is not None, why or ""):
        check(loaded.get("issue_evidence_query_sha256")
              == hashlib.sha256(collector.ISSUE_EVIDENCE_QUERY.encode("utf-8")).hexdigest(),
              "transport IssueEvidence query is not the reviewed collector's")
        for validate, document, label in ((collector.validate_issue_corpus, corpus, "corpus"),
                                          (collector.validate_issue_manifest, manifest, "collection manifest")):
            try:
                validate(document, label)
            except ValueError as error:
                problems.append(f"the reviewed collector rejects the {label}: {error}")
    records = manifest.get("records")
    selected = None
    if check(isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict),
             "collection manifest does not have exactly one run"):
        record = records[0]
        filters, interval = _dict(record.get("filters")), _dict(record.get("interval"))
        count = record.get("request_count")
        check(record.get("collection_status") == collection_status,
              "collection status is not the run status's")
        check(repositories is not None and filters.get("repositories") == repositories
              and filters.get("date_field") == request.get("date_field")
              and filters.get("state") == request.get("state") and filters.get("labels") == [],
              "collection filters differ from the request")
        check(interval.get("start_at") == request.get("start_at")
              and interval.get("end_at") == request.get("end_at"), "collection interval differs from the request")
        check(type(record.get("max_per_repository")) is int and record.get("max_per_repository") == cap
              and type(record.get("request_budget")) is int and record.get("request_budget") == budget,
              "collection cap or budget differs from the request")
        check(_count(count) and _count(budget) and count <= budget, "request count is outside the budget")
        check(isinstance(requests, list) and count == len(requests),
              "transport requests differ from the collection request count")
        repos = record.get("repositories")
        counts = [_dict(r).get("selected_count") for r in repos] if isinstance(repos, list) and repos else [None]
        if check(all(type(c) is int and c >= 0 for c in counts), "collection repositories have no selected counts"):
            selected = sum(counts)
    check(corpus.get("schema_version") == "1.0.0" and corpus.get("corpus_kind") == "github-issue",
          "corpus is not an Issue corpus")
    items = corpus.get("records")
    if check(isinstance(items, list), "corpus has no record list"):
        wanted = {r.lower() for r in repositories} if repositories else set()
        keys, per_repository = set(), {}
        for item in items:
            item = _dict(item)
            key = item.get("record_key")
            name = _dict(item.get("repository")).get("full_name")
            shaped = (isinstance(key, str) and key.startswith("github-issue:")
                      and all(isinstance(item.get(f), dict) for f in ("repository", "issue", "evidence_snapshot"))
                      and all(isinstance(item.get(f), list) for f in ("sources", "state_history")))
            check(shaped, "a corpus record does not have the Issue record shape")
            check(isinstance(name, str) and name.lower() in wanted, "a corpus record is from another repository")
            check(not isinstance(key, str) or key not in keys, "a corpus record key repeats")
            if isinstance(key, str):
                keys.add(key)
            if isinstance(name, str):
                per_repository[name.lower()] = per_repository.get(name.lower(), 0) + 1
        check(_count(cap) and all(n <= cap for n in per_repository.values()),
              "a repository has more records than max_per_repo")
        check(selected is None or len(items) == selected, "corpus records differ from the selected count")
    result["records"] = len(items) if isinstance(items, list) else None
    result["collection_status"] = collection_status if not problems else None
    return problems


CR_ENVELOPE = "oss-research-candidate-reads-envelope/1"
CR_ENTRY = "dot_local/bin/executable_oss-research-candidate-reads"
CR_STAGES = ("discover", "locus-search")
CR_INPUTS = {"discover": ("analysis.json",), "locus-search": ("discovery.json", "assessment.json")}
CR_DOCUMENT = {"discover": "discovery.json", "locus-search": "locus-search.json"}
CR_COMMON = ("verification-manifest.json", "transport.json", "relay.log", "run-manifest.json",
             "read-envelope.json")
CR_LIMITS = {"patterns": 5, "candidates": 5, "clues": 5, "clue_chars": 200, "request_budget": 60}
CR_REQUEST_FIELDS = {"stage", "repository", "relay_repositories", "patterns", "clues",
                     "max_candidates_per_repo", "max_candidates_total", "max_clues_per_pattern",
                     "max_clues_per_locus", "request_budget"}
# run status -> (adapter exit, output document status, wrapper exit)
CR_STATUSES = {"succeeded": (0, "complete", 0), "partial": (3, "partial", 5)}
CR_VERIFIER_TAIL = "verifying-open-source-contribution-candidates/scripts/verify_candidates.py"
HOST_VERIFIER = HOST_SKILLS / CR_VERIFIER_TAIL
CR_REPOSITORY = re.compile(r"\A([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})\Z")
CR_PATTERN = re.compile(r"\APAT-\d+\Z")
# REST static reads (#256), the verifier's constants of the same names (a test keeps them equal).
CR_STATIC_FIELDS = {"static_reads", "static_dirs"}
CR_STATIC_MAX_DIRS, CR_STATIC_MAX_DEPTH, CR_STATIC_MAX_FILES = 3, 3, 30
CR_STATIC_MAX_FILE_BYTES, CR_STATIC_EXCERPT_CHARS, CR_STATIC_MAX_LINKS = 256 * 1024, 200, 200
CR_REPOSITORY_FACT_REQUESTS = 26
CR_CONTENT_SEGMENT = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}|\.[A-Za-z0-9_-][A-Za-z0-9_.-]{0,253}"
CR_SEGMENT = re.compile(r"\A(?:" + CR_CONTENT_SEGMENT + r")\Z")
CR_POLICY_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "SECURITY.md",
                   ".github/SECURITY.md", "docs/SECURITY.md", ".github/PULL_REQUEST_TEMPLATE.md",
                   ".github/ISSUE_TEMPLATE", "CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md")


def cr_files(stage: str) -> set:
    return {CR_DOCUMENT[stage], *CR_COMMON}


def _bounded(value, limit) -> bool:
    return type(value) is int and 1 <= value <= limit


def cr_clue(clue) -> bool:
    return (isinstance(clue, str) and 1 <= len(clue) <= CR_LIMITS["clue_chars"] and clue == clue.strip()
            and '"' not in clue and "\\" not in clue
            and all(ord(character) >= 32 and ord(character) != 127 for character in clue))


def cr_static_request(request: dict) -> bool:
    """A rest request: 1..3 different directories of plain segments and a budget over the static reserve."""
    directories = request.get("static_dirs")
    return (request.get("static_reads") == "rest" and isinstance(directories, list)
            and 1 <= len(directories) <= CR_STATIC_MAX_DIRS and len(set(map(str, directories))) == len(directories)
            and all(isinstance(d, str) and len(d.split("/")) <= CR_STATIC_MAX_DEPTH
                    and all(CR_SEGMENT.fullmatch(s) for s in d.split("/")) for d in directories)
            and type(request.get("request_budget")) is int
            and request["request_budget"] >= CR_REPOSITORY_FACT_REQUESTS + 1 + len(directories) + CR_STATIC_MAX_FILES)


def cr_static_path(path, directories) -> bool:
    """README.md, or a Markdown file directly inside a requested directory."""
    if not isinstance(path, str):
        return False
    parent, _, name = path.rpartition("/")
    return path == "README.md" or (parent in directories and name.endswith(".md")
                                   and CR_SEGMENT.fullmatch(name) is not None)


def _keys(value) -> set:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def cr_static_record_problems(record: dict, directories: list) -> list:
    """One discover record's REST static reads: the fixed paths, the caps, no file body."""
    problems = []
    static = _dict(record.get("static_search"))
    head = record.get("head_sha")
    if not (static.get("mode") == "rest-contents" and static.get("clone_sha") is None
            and (static.get("ref") == head if isinstance(head, str)
                 else static.get("ref") is None and static.get("error") == "head-unavailable")):
        return ["a record's static reads are not the REST contents reads"]
    files, hits = static.get("files"), static.get("hits")
    files, hits = (files if isinstance(files, list) else [None]), (hits if isinstance(hits, list) else [None])
    listings = static.get("listings") if isinstance(static.get("listings"), list) else [None]
    absent = static.get("absent") if isinstance(static.get("absent"), list) else [None]
    if not (len(files) <= 1 + CR_STATIC_MAX_FILES
            and all(cr_static_path(_dict(f).get("path"), directories) for f in files + hits)
            and all(_dict(item).get("path") in directories for item in listings)
            and all(item in directories or cr_static_path(item, directories) for item in absent)):
        problems.append("a record's static reads name a path outside the fixed rule")
    excerpts = [_dict(h).get("excerpt") for h in hits]
    links = [static.get("external_links"), static.get("unverified_links")]
    if not (all(isinstance(e, dict) and isinstance(e.get("text"), str) and len(e["text"]) <= CR_STATIC_EXCERPT_CHARS
                and "\n" not in e["text"] for e in excerpts)
            and all(type(_dict(f).get("size")) is int and _dict(f)["size"] <= CR_STATIC_MAX_FILE_BYTES for f in files)
            and all(isinstance(item, list) and len(item) <= CR_STATIC_MAX_LINKS for item in links)
            and "content" not in _keys(static)):
        problems.append("a record's static evidence holds more than a one-line excerpt")
    return problems


def cr_contents_problems(requests: list, repository: str, directories: list) -> list:
    """Every contents request is a policy path, README.md, a requested directory or a file directly in it."""
    organisation = repository.split("/", 1)[0] + "/.github"
    allowed = {f"/repos/{source}/contents/{path}" for source in (repository, organisation)
               for path in CR_POLICY_PATHS}
    allowed |= {f"/repos/{repository}/contents/{d}" for d in directories}
    prefix = f"/repos/{repository}/contents/"
    for entry in requests:
        url = _dict(entry).get("logical_url")
        path = unquote(urlsplit(url).path) if isinstance(url, str) else ""
        if "/contents/" not in path or path in allowed:
            continue
        if not (path.startswith(prefix) and cr_static_path(path[len(prefix):], directories)):
            return ["a contents request is outside the fixed static paths"]
    return []


def cr_request_problems(request: dict, stage: str) -> list:
    """The candidate-reads entry's own limits, checked again on the envelope's request."""
    problems = []
    static = stage == "discover" and set(request) == CR_REQUEST_FIELDS | CR_STATIC_FIELDS
    if set(request) != CR_REQUEST_FIELDS and not static:
        return ["envelope request does not hold exactly the request fields"]
    repository = request.get("repository")
    if request.get("stage") != stage:
        problems.append("envelope request stage is not the host's stage")
    if not (isinstance(repository, str) and CR_REPOSITORY.fullmatch(repository)):
        return problems + ["envelope request has no OWNER/NAME repository"]
    organisation = repository.split("/", 1)[0] + "/.github"
    expected = [repository] if repository.lower() == organisation.lower() else [repository, organisation]
    if request.get("relay_repositories") != expected:
        problems.append("envelope relay repositories are not the repository and its owner's .github")
    if not _bounded(request.get("request_budget"), CR_LIMITS["request_budget"]):
        problems.append("envelope request budget is outside the entry's limits")
    patterns, clues = request.get("patterns"), request.get("clues")
    if stage == "discover":
        good = (isinstance(patterns, list) and 1 <= len(patterns) <= CR_LIMITS["patterns"]
                and len(set(patterns)) == len(patterns)
                and all(isinstance(p, str) and CR_PATTERN.fullmatch(p) for p in patterns)
                and clues == [] and request.get("max_clues_per_locus") is None
                and all(_bounded(request.get(name), limit) for name, limit in (
                    ("max_candidates_per_repo", CR_LIMITS["candidates"]),
                    ("max_candidates_total", CR_LIMITS["candidates"]),
                    ("max_clues_per_pattern", CR_LIMITS["clues"])))
                and (not static or cr_static_request(request)))
    else:
        good = (patterns == [] and isinstance(clues, list) and len(clues) <= CR_LIMITS["clues"]
                and len(set(clues)) == len(clues) and all(cr_clue(c) for c in clues)
                and _bounded(request.get("max_clues_per_locus"), CR_LIMITS["clues"])
                and all(request.get(name) is None for name in (
                    "max_candidates_per_repo", "max_candidates_total", "max_clues_per_pattern")))
    if not good:
        problems.append("envelope request patterns, clues or caps are outside the entry's limits")
    return problems


def host_verifier(host: dict):
    """The host tree's verifier, loaded only when it is the reviewed one: (module, problem).

    Its compute_revision() is the revision the output must carry."""
    try:
        seen = hashlib.sha256(HOST_VERIFIER.read_bytes()).hexdigest()
    except OSError as error:
        return None, f"the host tree's verifier is unreadable: {type(error).__name__}"
    if seen != host["verifier_sha256"]:
        return None, "the host tree's verifier is not the one the host reviewed"
    spec = importlib.util.spec_from_file_location("_verify_pickup_verifier", HOST_VERIFIER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, None


def candidate_problems(dest: Path, result: dict, host: dict) -> list:
    """The --candidate-reads cross-checks over an unpacked, structure-checked tar."""
    docs, problems = json_documents(dest, cr_files(host["stage"]))
    if problems:
        return problems
    try:
        return candidate_bindings(dest, docs, result, host)
    except (TypeError, AttributeError, KeyError, ValueError, IndexError) as error:
        result["step_status"] = None
        return [f"a member holds a malformed value: {type(error).__name__}"]


def candidate_bindings(dest: Path, docs: dict, result: dict, host: dict) -> list:
    problems = []
    stage = host["stage"]
    members = cr_files(stage)
    envelope, run, transport = docs["read-envelope.json"], docs["run-manifest.json"], docs["transport.json"]
    manifest, document = docs["verification-manifest.json"], docs[CR_DOCUMENT[stage]]

    def check(ok, message):
        if not ok:
            problems.append(message)
        return ok

    check(envelope.get("schema") == CR_ENVELOPE and envelope.get("entry") == CR_ENTRY
          and envelope.get("stage") == stage, "envelope is not the candidate reads entry's for this stage")
    files = envelope.get("files")
    others = members - {"read-envelope.json"}
    if check(isinstance(files, dict) and set(files) == others, "envelope does not list exactly the members"):
        for name in sorted(others):
            data = (dest / name).read_bytes()
            entry = _dict(files[name])
            check(entry.get("bytes") == len(data) and entry.get("sha256") == hashlib.sha256(data).hexdigest(),
                  f"{name} does not match the envelope")
    inputs = _dict(envelope.get("inputs"))
    if check(set(inputs) == set(CR_INPUTS[stage]), "envelope does not list exactly the stage's inputs"):
        for name in CR_INPUTS[stage]:
            entry = _dict(inputs[name])
            check(set(entry) == {"bytes", "sha256"} and _count(entry.get("bytes"))
                  and isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]),
                  f"envelope input {name} has no size and sha256")
    request, relay = _dict(envelope.get("request")), _dict(envelope.get("relay"))
    request_found = cr_request_problems(request, stage)
    problems += request_found
    usable = not request_found
    upstream = envelope.get("upstream")
    check(upstream in UPSTREAMS, "envelope does not state a known upstream")
    check(upstream == host["upstream"], "envelope upstream is not the one the host asked for")
    result["upstream"] = upstream if isinstance(upstream, str) else None
    status = run.get("status")
    expected = CR_STATUSES.get(status) if isinstance(status, str) else None
    check(expected is not None and run.get("kind") == "candidate-reads" and run.get("stage") == stage,
          "run manifest is not a succeeded or partial candidate read of this stage")
    adapter_exit, output_status, wrapper_exit = expected or (None, None, None)
    check(type(envelope.get("adapter_exit")) is int and envelope.get("adapter_exit") == adapter_exit
          and type(run.get("exit_code")) is int and run.get("exit_code") == adapter_exit,
          "adapter exit is not the run status's")
    check(run.get("failures") == [] and run.get("problems") == [], "run manifest has failures or problems")
    run_relay = _dict(run.get("relay"))
    check(all(run_relay.get(k) == relay.get(k) for k in ("sources", "relay_sha256", "port")),
          "run manifest relay differs from the envelope")
    check(run.get("request") == envelope.get("request") and run.get("inputs") == envelope.get("inputs"),
          "run manifest request or inputs differ from the envelope")
    sources = relay.get("sources") if isinstance(relay.get("sources"), list) else []
    ready = [s for s in sources if isinstance(s, dict) and set(s) == {"role", "path", "sha256"}]
    roles = {s["role"]: s["sha256"] for s in ready if isinstance(s["role"], str)}
    check(len(sources) == 2 and len(ready) == 2 and roles == {"issues": host["relay_issues_sha256"],
                                                            "sibling": host["relay_sibling_sha256"]},
          "relay ready sources are not exactly the issues and sibling files the host reviewed")
    check(transport.get("schema_version") == "1.0.0" and transport.get("stage") == stage,
          "transport record is not this stage's")
    verifier = _dict(transport.get("verifier"))
    check(set(verifier) == {"path", "sha256"} and verifier.get("sha256") == host["verifier_sha256"]
          and isinstance(verifier.get("path"), str) and verifier["path"].endswith(CR_VERIFIER_TAIL),
          "transport verifier is not the one the host reviewed")
    check(transport.get("transport") == "srt-proxy", "transport record does not say srt-proxy")
    check(isinstance(transport.get("credential"), str) and transport["credential"].startswith("none:"),
          "transport does not state that no credential was used")
    check(type(transport.get("exit_code")) is int and transport.get("exit_code") == adapter_exit,
          "transport exit code is not the adapter's")
    check(transport.get("relay_base_url") == f"http://127.0.0.1:{relay.get('port')}",
          "transport relay URL is not the envelope's relay")
    requests = transport.get("requests")
    # Delivered exchanges only: the relay carried the request and GitHub answered (an HTTP
    # error such as the anonymous code search's 401 included). A refusal, timeout, broken
    # envelope or transport failure is not a read.
    delivered = isinstance(requests, list) and requests and all(
        isinstance(r, dict) and (r.get("outcome") == "relayed" or (
            r.get("outcome") == "upstream-error" and type(r.get("upstream_status")) is int
            and 400 <= r["upstream_status"] <= 599)) for r in requests)
    check(delivered, "a transport request was not delivered (refused, timed out, malformed or failed)")
    if delivered:
        errors = {}
        for r in requests:
            if r["outcome"] == "upstream-error":
                errors[str(r["upstream_status"])] = errors.get(str(r["upstream_status"]), 0) + 1
        result["upstream_errors"] = errors
        result["requests"] = len(requests)
    wrapper, inner = wrapper_documents(host["wrapper_record"])
    check(wrapper is not None, "wrapper record is unreadable")
    wrapper, inner = wrapper or {}, inner or {}
    check(type(wrapper.get("exit_code")) is int and wrapper.get("exit_code") == wrapper_exit
          and wrapper.get("failures") == [], "the unit wrapper's exit is not the run status's")
    check(isinstance(wrapper.get("unit"), str) and wrapper.get("unit") == envelope.get("unit"),
          "wrapper unit is not the envelope's unit")
    check(inner.get("mode") == "candidate-reads" and inner.get("stage") == stage
          and inner.get("status") == status, "wrapper inner summary is not this candidate read")
    verifier_module, why = host_verifier(host)
    revision = verifier_module.compute_revision() if check(verifier_module is not None, why or "") else None
    budget = request.get("request_budget") if usable else None
    one = {}
    runs = manifest.get("runs")
    if check(manifest.get("schema_version") == "1.0.0" and isinstance(runs, list) and len(runs) == 1
             and isinstance(runs[0], dict), "verification manifest does not have exactly one run"):
        one = runs[0]
    run_budget = _dict(one.get("budget"))
    check(one.get("command") == stage and one.get("status") == output_status,
          "verification manifest run is not this stage's or its status differs")
    check(usable and run_budget.get("request_limit") == budget, "verification budget is not the request's")
    consumed = run_budget.get("requests_consumed")
    check(_count(consumed) and isinstance(requests, list) and consumed == len(requests)
          and (budget is None or consumed <= budget),
          "the verifier's consumed requests differ from the transport's or pass the budget")
    generated = _dict(document.get("generated_by"))
    check(generated.get("name") == "verifying-open-source-contribution-candidates"
          and revision is not None and generated.get("revision") == revision
          and _dict(manifest.get("generated_by")).get("revision") == revision,
          "output revision is not the host verifier's")
    check(document.get("schema_version") == "1.0.0" and document.get("status") == output_status,
          "output document is not this status's")
    repository = request.get("repository") if usable else None
    if stage == "discover":
        found = _dict(document.get("inputs"))
        check(found.get("analysis_sha256") == _dict(inputs.get("analysis.json")).get("sha256"),
              "output analysis is not the envelope's input")
        check(usable and found.get("repositories") == [repository]
              and found.get("pattern_ids") == request.get("patterns")
              and found.get("request_budget") == budget and found.get("allow_clone") is False,
              "output inputs differ from the request (repository, patterns, budget, clone)")
        directories = request.get("static_dirs") if usable and request.get("static_reads") == "rest" else None
        check(found.get("static_reads") == "rest" and found.get("static_dirs") == directories
              if directories is not None else not {"static_reads", "static_dirs"} & set(found),
              "output static reads differ from the request")
        if directories is not None and isinstance(requests, list):
            problems += cr_contents_problems(requests, repository, directories)
        records = document.get("records")
        if check(isinstance(records, list), "output has no record list") and usable:
            for item in records:
                item = _dict(item)
                check(item.get("repository") == repository and item.get("pattern_id") in request["patterns"],
                      "an output record is for another repository or pattern")
                if directories is not None:
                    problems += [p for p in cr_static_record_problems(item, directories) if p not in problems]
            check(len(records) <= request["max_candidates_per_repo"]
                  and len(records) <= request["max_candidates_total"],
                  "the output has more candidates than the caps")
            result["records"] = len(records)
    else:
        check(document.get("discovery_sha256") == _dict(inputs.get("discovery.json")).get("sha256"),
              "output discovery is not the envelope's input")
        searches = document.get("searches")
        if check(isinstance(searches, list), "output has no search list") and usable:
            for item in searches:
                item = _dict(item)
                check(item.get("repository") == repository and isinstance(item.get("clues"), list)
                      and len(item["clues"]) <= request["max_clues_per_locus"],
                      "an output search is for another repository or uses more clues than the cap")
            result["records"] = len(searches)
    result["step_status"] = output_status if not problems else None
    return problems


HEX64 = re.compile(r"[0-9a-f]{64}")
TOKEN_SHAPES = re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                          rb"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# Members that hold the run's own records, not remote text: scanned for credential shapes.
TOKEN_SCAN_MEMBERS = ("relay.log", "transport.json", "run-manifest.json", "collection-envelope.json",
                      "read-envelope.json", "verification-manifest.json")
FORBID_MAX_BYTES = 4096


def marker_problems(marker) -> list:
    """The github_token object: exactly authenticated (a bool) and token_file_sha256 (hex64, or null)."""
    if not isinstance(marker, dict) or set(marker) != {"authenticated", "token_file_sha256"}:
        return ["github token marker is not exactly {authenticated, token_file_sha256}"]
    authenticated, digest = marker["authenticated"], marker["token_file_sha256"]
    if type(authenticated) is not bool:
        return ["github token marker: authenticated is not a boolean"]
    if authenticated and not (isinstance(digest, str) and HEX64.fullmatch(digest)):
        return ["github token marker: an authenticated run needs the sha256 of the token file"]
    if not authenticated and digest is not None:
        return ["github token marker: an anonymous run has no token file sha256"]
    return []


def forbidden_value(path: Path | None):
    """(value bytes or None, problems) of the file named by --forbid-value-file."""
    if path is None:
        return None, []
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, ["the --forbid-value-file cannot be opened"]
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > FORBID_MAX_BYTES:
            return None, ["the --forbid-value-file is not a small regular file"]
        value = os.read(descriptor, FORBID_MAX_BYTES + 1).strip()
    finally:
        os.close(descriptor)
    if len(value) < 8 or any(character in value for character in b"\r\n\0"):
        return None, ["the --forbid-value-file does not hold one value of at least 8 characters"]
    return value, []


def token_problems(dest: Path, result: dict, envelope_name: str, forbid: Path | None) -> list:
    """The GitHub token marker and the exposure checks over an unpacked tar (never printing a value)."""
    problems = []
    docs, found = json_documents(dest, {envelope_name, "run-manifest.json"})
    envelope, run = docs.get(envelope_name), docs.get("run-manifest.json")
    result["authenticated"] = result["token_file_sha256"] = None
    if envelope is None or run is None:
        problems.append("github token marker: the envelope or the run manifest cannot be read")
    else:
        marker = envelope.get("github_token")
        bad = marker_problems(marker)
        problems += bad
        if not bad and run.get("github_token") != marker:
            problems.append("github token marker differs between the envelope and the run manifest")
        elif not bad:
            result["authenticated"], result["token_file_sha256"] = marker["authenticated"], marker["token_file_sha256"]
    value, forbid_problems = forbidden_value(forbid)
    problems += forbid_problems
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.name in TOKEN_SCAN_MEMBERS and TOKEN_SHAPES.search(data):
            problems.append(f"{path.name} holds a credential-shaped value")
        if value is not None and value in data:
            problems.append(f"{path.name} holds the forbidden value")
    return problems


def member_problems(bundle: tarfile.TarFile, cap: int, required=REQUIRED,
                    forbidden=frozenset(), allowed=RESULT_FILES) -> list[str]:
    problems, seen, total, count = [], set(), 0, 0
    for member in bundle:  # streamed; stops reading at the first excess
        count += 1
        if count > MAX_MEMBERS:
            return problems + [f"more than {MAX_MEMBERS} members"]
        name = member.name.removeprefix("./")
        if member.isdir() and name in ("", "."):
            continue
        if not member.isfile() or member.issparse() or name not in allowed:
            problems.append(f"unexpected entry: {member.name} (type {member.type!r})")
            continue
        if name in seen:
            problems.append(f"duplicate entry: {name}")
        if name in forbidden:
            problems.append(f"unexpected entry for this contract: {name}")
        seen.add(name)
        if member.size > MAX_MEMBER_BYTES:
            problems.append(f"{name} is larger than {MAX_MEMBER_BYTES} bytes")
        total += member.size
        if total >= cap:
            return problems + ["member sizes reach the cap"]
    missing = required - seen
    if missing:
        problems.append("missing: " + ", ".join(sorted(missing)))
    return problems


def main() -> int:
    tail = sys.argv[4:]
    forbid = None
    if "--forbid-value-file" in tail:
        if tail[-2:-1] != ["--forbid-value-file"] or tail[:1] not in (["--collect"], ["--issues"],
                                                                      ["--candidate-reads"]):
            raise SystemExit(__doc__)
        forbid = Path(tail[-1])
        tail = tail[:-2]
    collect = tail[:1] == ["--collect"]
    issues = tail[:1] == ["--issues"]
    candidate = tail[:1] == ["--candidate-reads"]
    if issues:
        if len(tail) != 9 or tail[1] != "--issue-collector-sha256" or tail[3] != "--sibling-sha256" \
                or tail[5] != "--wrapper-record" or tail[7] != "--upstream" or tail[8] not in UPSTREAMS \
                or any(re.fullmatch(r"[0-9a-f]{64}", tail[i]) is None for i in (2, 4)):
            raise SystemExit(__doc__)
        host = {"issue_collector_sha256": tail[2], "sibling_sha256": tail[4],
                "wrapper_record": Path(tail[6]), "upstream": tail[8]}
    elif candidate:
        if len(tail) != 13 or tail[1] != "--stage" or tail[2] not in CR_STAGES \
                or tail[3] != "--verifier-sha256" or tail[5] != "--relay-issues-sha256" \
                or tail[7] != "--relay-sibling-sha256" or tail[9] != "--wrapper-record" \
                or tail[11] != "--upstream" or tail[12] not in UPSTREAMS \
                or any(re.fullmatch(r"[0-9a-f]{64}", tail[i]) is None for i in (4, 6, 8)):
            raise SystemExit(__doc__)
        host = {"stage": tail[2], "verifier_sha256": tail[4], "relay_issues_sha256": tail[6],
                "relay_sibling_sha256": tail[8], "wrapper_record": Path(tail[10]), "upstream": tail[12]}
    elif collect:
        if len(tail) != 7 or tail[1] != "--collector-sha256" or tail[3] != "--wrapper-record" \
                or tail[5] != "--upstream" or tail[6] not in UPSTREAMS \
                or re.fullmatch(r"[0-9a-f]{64}", tail[2]) is None:
            raise SystemExit(__doc__)
        host = {"collector_sha256": tail[2], "wrapper_record": Path(tail[4]), "upstream": tail[6]}
    elif tail not in ([], ["--auto-observation"]):
        raise SystemExit(__doc__)
    auto = tail == ["--auto-observation"]
    archive, cap, dest = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    size = archive.stat().st_size
    result = {"bytes": size, "files": [], "status": None, "policy_sha256": None}
    if size >= cap:
        result["problems"] = [f"tar reached the cap ({size} bytes): taken as cut off"]
        print(json.dumps(result))
        return 1
    try:
        with tarfile.open(archive, "r:") as bundle:
            if issues:
                problems = member_problems(bundle, cap, ISSUE_FILES, allowed=ISSUE_FILES)
            elif candidate:
                problems = member_problems(bundle, cap, cr_files(host["stage"]),
                                           allowed=cr_files(host["stage"]))
            elif collect:
                problems = member_problems(bundle, cap, COLLECT_REQUIRED, allowed=COLLECT_FILES)
            elif auto:
                problems = member_problems(bundle, cap, AUTO_REQUIRED, {"report.md"})
            else:
                problems = member_problems(bundle, cap)
            if not problems:
                dest.mkdir(parents=True, exist_ok=False)
                bundle.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as error:
        problems = [f"tar cannot be read: {type(error).__name__}: {error}"]
    if not problems and issues:
        result["contract"] = "issues"
        result["files"] = sorted(p.name for p in dest.iterdir())
        problems = issue_problems(dest, result, host)
        problems += token_problems(dest, result, "collection-envelope.json", forbid)
        result["problems"] = problems
        print(json.dumps(result))
        if problems:
            return 1
        return 0 if result["collection_status"] == "complete" else ISSUE_EXIT_PARTIAL
    if not problems and candidate:
        result["contract"] = "candidate-reads"
        result["stage"] = host["stage"]
        result["files"] = sorted(p.name for p in dest.iterdir())
        problems = candidate_problems(dest, result, host)
        problems += token_problems(dest, result, "read-envelope.json", forbid)
        result["problems"] = problems
        print(json.dumps(result))
        if problems:
            return 1
        return 0 if result["step_status"] == "complete" else ISSUE_EXIT_PARTIAL
    if not problems and collect:
        result["contract"] = "collect"
        result["files"] = sorted(p.name for p in dest.iterdir())
        problems = collect_problems(dest, result, host)
        problems += token_problems(dest, result, "collection-envelope.json", forbid)
        result["problems"] = problems
        print(json.dumps(result))
        if problems:
            return 1
        return 0 if result["collection_status"] == "complete" else ISSUE_EXIT_PARTIAL
    if not problems:
        result["files"] = sorted(p.name for p in dest.iterdir())
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        result["status"] = manifest.get("status")
        result["policy_sha256"] = manifest.get("policy_sha256")
        if auto:
            expected = {"status": "failed", "exit_code": 0, "failures": [],
                        "problems": AUTO_PROBLEMS, "permission_mode": "auto"}
            for key, value in expected.items():
                if manifest.get(key) != value:
                    problems.append(f"manifest {key} is {manifest.get(key)!r}, not {value!r}")
            if (manifest.get("credential") or {}).get("exact_value_check") != "clean":
                problems.append("manifest credential check is not clean")
            result["contract"] = "auto-observation"
        elif manifest.get("status") != "succeeded":
            problems.append(f"manifest status is {manifest.get('status')!r}")
        if not auto:
            report = hashlib.sha256((dest / "report.md").read_bytes()).hexdigest()
            if report != manifest.get("report_sha256"):
                problems.append("report.md does not match report_sha256")
    result["problems"] = problems
    print(json.dumps(result))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
