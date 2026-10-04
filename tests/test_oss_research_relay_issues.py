"""Tests for the Issue collector's relay path (executable_oss-research-relay-issues).

Every run is local: a loopback mock stands in for api.github.com (REST and the
GraphQL POST), the real relay serves it, and the adapter drives the real
collect_open_source_issues.collect. A socket guard refuses any address but
127.0.0.1. Nothing reads a token.
"""

from __future__ import annotations

import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
RELAY_SCRIPT = ROOT / "dot_local/bin/executable_oss-research-relay"
ADAPTER_SCRIPT = ROOT / "dot_local/bin/executable_oss-research-relay-issues"
SKILLS = ROOT / "dot_codex/skills"
API = "https://api.github.com"
TARGET = "octo/demo"
INTERVAL = ("2030-01-01T00:00:00Z", "2030-02-01T00:00:00Z")
CAPTURED_AT = "2030-02-02T00:00:00Z"
FIXED_TIME = 1896307200.0  # 2030-02-02T00:00:00Z

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _loopback_only(address):
    if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
        raise PermissionError("test network guard refused " + repr(address))


def setUpModule():
    socket.socket.connect = lambda self, address: (_loopback_only(address), _real_connect(self, address))[1]
    socket.socket.connect_ex = lambda self, address: (_loopback_only(address), _real_connect_ex(self, address))[1]


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


relay_module = _load("oss_research_relay_for_issues", RELAY_SCRIPT)
adapter = _load("oss_research_relay_issues_under_test", ADAPTER_SCRIPT)
relay_issues_module, relay_sources = relay_module.load_issues_module(SKILLS)
collector, collector_source = adapter.load_issue_collector(SKILLS)


# -- mock api.github.com -------------------------------------------------------


def _query_key(query):
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return tuple(sorted((name, value) for name, value in pairs if name != "per_page"))


def _connection(nodes=()):
    return {"totalCount": len(nodes), "pageInfo": {"hasNextPage": False}, "nodes": list(nodes)}


def issue_payload(number, comments, state="open"):
    closed = state == "closed"
    return {
        "number": number, "node_id": "I_node_%d" % number, "title": "Synthetic issue %d" % number,
        "state": state, "state_reason": "completed" if closed else None,
        "created_at": "2030-01-1%dT00:00:00Z" % number,
        "updated_at": "2030-01-2%dT00:00:00Z" % number,
        "closed_at": "2030-01-1%dT12:00:00Z" % number if closed else None, "labels": [],
        "url": "%s/repos/%s/issues/%d" % (API, TARGET, number),
        "html_url": "https://github.com/%s/issues/%d" % (TARGET, number),
        "repository_url": "%s/repos/%s" % (API, TARGET),
        "body": "Synthetic body %d." % number, "user": {"login": "fixture-user", "node_id": "U_fixture"},
        "author_association": "CONTRIBUTOR", "locked": False, "active_lock_reason": None,
        "closed_by": {"login": "maintainer", "node_id": "U_maint"} if closed else None,
        "type": None, "comments": len(comments)}


def world(*, graphql_status=401, issue_status=200, state="open", date_field="created"):
    """REST routes for two open Issues of TARGET, and what /graphql answers."""
    interval = collector.resolve_interval(start_at=INTERVAL[0], end_at=INTERVAL[1], start_date=None, end_date=None,
                                          recent_days=None, timezone_name="UTC", as_of=None)
    query = collector.build_issue_query(TARGET, interval, date_field, state, [])
    comments = {1: [{"id": 101, "body": "A maintainer reply.", "user": {"login": "maintainer"},
                     "author_association": "MEMBER", "created_at": "2030-01-12T00:00:00Z",
                     "updated_at": "2030-01-12T00:00:00Z",
                     "url": "%s/repos/%s/issues/comments/101" % (API, TARGET),
                     "html_url": "https://github.com/%s/issues/1#issuecomment-101" % TARGET}], 2: []}
    routes = {}

    def add(path, params, payload, status=200):
        routes[(path, _query_key(urllib.parse.urlencode(params or {})))] = {"status": status, "payload": payload}

    add("/versions", None, ["2026-03-10"])
    add("/repos/" + TARGET, None, {"full_name": TARGET, "node_id": "R_node"})
    items = [issue_payload(number, comments[number], state) for number in (2, 1)]
    add("/search/issues", {"q": query, "page": 1}, {"total_count": 2, "incomplete_results": False, "items": items})
    for number in (1, 2):
        add("/repos/%s/issues/%d" % (TARGET, number), None, issue_payload(number, comments[number], state),
            status=issue_status if number == 2 else 200)
        add("/repos/%s/issues/%d/comments" % (TARGET, number), {"page": 1}, comments[number])
        add("/repos/%s/issues/%d/timeline" % (TARGET, number), {"page": 1}, [])
    graphql = {}
    for number in (1, 2):
        if graphql_status == 200:
            graphql[number] = (200, {"data": {"repository": {"issue": {
                "id": "I_node_%d" % number, "lastEditedAt": None, "userContentEdits": _connection(),
                "duplicateOf": None, "closedByPullRequestsReferences": _connection(),
                "timelineItems": _connection(), "comments": _connection(
                    [{"databaseId": 101, "lastEditedAt": None, "userContentEdits": _connection()}]
                    if number == 1 else [])}}}})
        else:
            graphql[number] = (graphql_status, {"message": "This endpoint requires you to be authenticated."})
    return routes, graphql


class MockUpstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes, graphql):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.routes, self.graphql = routes, graphql
        self.requests = []


class MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return None

    def _reply(self, status, payload, extra=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        self.server.requests.append({"method": "GET", "path": self.path,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})
        route = self.server.routes.get((path, _query_key(query)), {"status": 404, "payload": {"message": "Not Found"}})
        self._reply(route["status"], route["payload"], route.get("headers"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"method": "POST", "path": self.path, "body": body,
                                     "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.partition("?")[0] != "/graphql":
            return self._reply(404, {"message": "Not Found"})
        status, payload = self.server.graphql.get(body.get("variables", {}).get("number"),
                                                  (200, {"data": {"repository": None}}))
        self._reply(status, payload)


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class UpstreamOpener:
    """api.github.com -> the mock, GET or POST, keeping the logical URL."""

    def __init__(self, upstream):
        self.upstream = upstream
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Refuse)

    def open(self, request, timeout):
        parts = urllib.parse.urlsplit(request.full_url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com", request.full_url
        local = "http://127.0.0.1:%d%s%s" % (self.upstream.server_address[1], parts.path,
                                             "?" + parts.query if parts.query else "")
        forwarded = urllib.request.Request(local, data=request.data, headers=dict(request.header_items()),
                                           method=request.get_method())
        try:
            response = self._opener.open(forwarded, timeout=timeout)
        except urllib.error.HTTPError as error:
            error.url = request.full_url
            raise
        response.url = request.full_url
        return response


# -- harness -------------------------------------------------------------------


class Harness(unittest.TestCase):
    budget = 100
    world_options: dict = {}
    relay_repositories = (TARGET,)

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://10.0.0.1:3128"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "HTTP_PROXY", "http_proxy"):
            os.environ.pop(name, None)
        self.root = Path(tempfile.mkdtemp(prefix="osri-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        routes, graphql = world(**self.world_options)
        self.upstream = MockUpstream(routes, graphql)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        for patcher in (mock.patch.object(relay_module.time, "sleep", lambda seconds: None),
                        mock.patch.object(collector, "_run_timestamp", lambda value: value or CAPTURED_AT),
                        mock.patch.object(collector._sibling, "_run_timestamp", lambda value: value or CAPTURED_AT),
                        # The adapter loads the collector by path; hand it the same (time-fixed) module.
                        mock.patch.object(adapter, "load_issue_collector",
                                          lambda skills_dir: (collector, collector_source))):
            patcher.start()
            self.addCleanup(patcher.stop)
        argv = []
        for repository in self.relay_repositories:
            argv += ["--repo", repository]
        args = relay_module.build_parser().parse_args(argv + [
            "--request-budget", str(self.budget), "--max-response-bytes", str(1 << 20),
            "--response-byte-budget", str(8 << 20), "--timeout-seconds", "10"])
        self.server = relay_module.build_server(args, opener=UpstreamOpener(self.upstream),
                                                issues_module=relay_issues_module)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def collector_argv(self, name, **changes):
        options = {"--repo": TARGET, "--date-field": "created", "--state": "open", "--start-at": INTERVAL[0],
                   "--end-at": INTERVAL[1], "--max-per-repo": "5", "--request-budget": "60",
                   "--output": str(self.root / (name + "-corpus.json")),
                   "--manifest": str(self.root / (name + "-manifest.json"))}
        options.update(changes)
        argv = []
        for key, value in options.items():
            argv += [key, value]
        return argv

    def direct(self):
        budget = collector._sibling.RequestBudget(60)
        common = dict(api_version=collector._sibling.API_VERSION, budget=budget, opener=UpstreamOpener(self.upstream),
                      token_provider=lambda: None, sleeper=lambda seconds: None, clock=lambda: FIXED_TIME)
        rest = collector._sibling.GhApiClient(**common)
        graphql = collector.GraphqlQueryClient(**common)
        graphql.request_events = rest.request_events
        interval = collector.resolve_interval(start_at=INTERVAL[0], end_at=INTERVAL[1], start_date=None,
                                              end_date=None, recent_days=None, timezone_name="UTC", as_of=None)
        run = collector.collect(rest, graphql, repositories=[TARGET], date_field="created", state="open", labels=[],
                                interval=interval, max_per_repository=5, captured_at=CAPTURED_AT)
        return run

    def relayed(self, name="relayed", environ=None, extra=(), **changes):
        argv = ["--relay-url", "http://127.0.0.1:%d" % self.port, "--skills-dir", str(SKILLS),
                "--transport-record", str(self.root / (name + "-transport.json")), "--timeout-seconds", "30",
                "--max-response-bytes", str(1 << 20), *extra] + self.collector_argv(name, **changes)
        code = adapter.run(argv, environ if environ is not None else {}, captured_at=CAPTURED_AT,
                           sleeper=lambda seconds: None, clock=lambda: FIXED_TIME)
        return code

    def load(self, name, kind):
        return json.loads((self.root / ("%s-%s.json" % (name, kind))).read_text(encoding="utf-8"))


def _differences(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        found = []
        for key in sorted(set(left) | set(right)):
            found += _differences(left.get(key, "<missing>"), right.get(key, "<missing>"), path + "." + str(key))
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        found = []
        for index, (a, b) in enumerate(zip(left, right)):
            found += _differences(a, b, path + "[%d]" % index)
        return found
    return [] if left == right else [path]


# The one allowed difference: the relayed REST client's recorded version names the relay transport.
def _unallowed(differences):
    return [d for d in differences if not re.search(r"\.client_version\Z", d)]


# -- tests ---------------------------------------------------------------------


class IdentityTests(Harness):
    def assert_same(self, direct, name="relayed"):
        corpus, manifest = self.load(name, "corpus"), self.load(name, "manifest")
        self.assertEqual(_differences(direct.corpus, corpus), [])
        self.assertEqual(_unallowed(_differences(direct.manifest, manifest)), [])
        collector.validate_issue_corpus(corpus, "relayed corpus")
        collector.validate_issue_manifest(manifest, "relayed manifest")

    def test_anonymous_graphql_401_is_the_same_honest_partial_either_way(self):
        direct = self.direct()
        self.upstream.requests.clear()
        code = self.relayed()
        self.assertEqual((direct.exit_code, code), (3, 3))
        self.assert_same(direct)
        transport = self.load("relayed", "transport")
        graphql = [entry for entry in transport["requests"] if entry.get("method") == "POST"]
        self.assertEqual({(entry["outcome"], entry.get("upstream_status")) for entry in graphql}, {("upstream-error", 401)})
        self.assertIn("not TLS evidence", transport["note"])
        self.assertEqual(transport["collector"]["issue_evidence_query_sha256"], collector_source["issue_evidence_query_sha256"])
        # Pickup contract (agreed with A): transport.collector.issues and .sibling are the files the
        # adapter really loaded, in the relay ready line's own {path, sha256} shape, so each equals
        # the relay's source of the same role (and the host's expected hash of that file).
        ready = {entry["role"]: {"path": entry["path"], "sha256": entry["sha256"]} for entry in relay_sources}
        self.assertEqual(transport["collector"]["issues"], ready["issues"])
        self.assertEqual(transport["collector"]["sibling"], ready["sibling"])
        self.assertNotIn("sibling", transport)  # no duplicate top-level field
        # The relay sent the fixed operation's own query and no credential upstream.
        posts = [r for r in self.upstream.requests if r["method"] == "POST"]
        self.assertTrue(posts)
        for request in self.upstream.requests:
            self.assertNotIn("authorization", request["headers"])
        for post in posts:
            self.assertEqual(post["body"]["query"], relay_issues_module.ISSUE_EVIDENCE_QUERY)
            self.assertEqual(set(post["body"]["variables"]), {"owner", "name", "number"})
        # One budget for REST and GraphQL, every attempt counted: the manifest's request_count is
        # exactly the adapter's relay exchanges, GraphQL included.
        manifest = self.load("relayed", "manifest")
        self.assertEqual(manifest["records"][-1]["request_count"], len(transport["requests"]))
        self.assertEqual(direct.manifest["records"][-1]["request_count"], len(transport["requests"]))

    def test_rest_and_graphql_share_one_budget_object(self):
        opener = adapter.issue_relay_opener_class(adapter.load_relay_collect())(
            collector, "http://127.0.0.1:%d" % self.port, timeout_seconds=5, max_response_bytes=1 << 20)
        rest, graphql = adapter.build_clients(collector, adapter.load_relay_collect(), opener,
                                              api_version=collector._sibling.API_VERSION, request_budget=7)
        self.assertIs(rest.budget, graphql.budget)
        self.assertIs(rest.request_events, graphql.request_events)
        self.assertEqual(rest.budget.limit, 7)


class CompleteTests(Harness):
    world_options = {"graphql_status": 200}

    def test_a_complete_collection_is_identical_except_client_version(self):
        direct = self.direct()
        code = self.relayed()
        self.assertEqual((direct.exit_code, code), (0, 0))
        IdentityTests.assert_same(self, direct)
        manifest = self.load("relayed", "manifest")
        versions = [d for d in _differences(direct.manifest, manifest)]
        self.assertTrue(versions and all(d.endswith(".client_version") for d in versions))


class QuotaRecordTests(Harness):
    """The transport record keeps each answer's quota facts (#192 follow-up)."""

    world_options = {"graphql_status": 200}
    QUOTA = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990", "x-ratelimit-reset": "1790000000",
             "x-ratelimit-used": "10", "x-ratelimit-resource": "core"}

    def setUp(self):
        super().setUp()
        for route in self.upstream.routes.values():
            route["headers"] = dict(self.QUOTA)

    def test_rest_requests_record_limit_remaining_used_and_resource(self):
        self.assertEqual(self.relayed(), 0)
        requests = self.load("relayed", "transport")["requests"]
        rest = [entry for entry in requests if entry.get("method") != "POST"]
        self.assertTrue(rest)
        for entry in rest:
            self.assertEqual(entry["rate_limit"], {"limit": 5000, "remaining": 4990, "resource": "core", "used": 10})
        self.assertNotIn("1790000000", json.dumps(requests))


class ClosedIssuesTests(Harness):
    """--state closed and --date-field closed take the same contract as open Issues (#192 follow-up)."""

    world_options = {"graphql_status": 200, "state": "closed", "date_field": "closed"}

    def test_closed_issues_are_collected_through_the_relay_with_the_state_recorded(self):
        code = self.relayed(**{"--state": "closed", "--date-field": "closed"})
        self.assertEqual(code, 0)
        manifest = self.load("relayed", "manifest")
        run = manifest["records"][-1]
        self.assertEqual((run["filters"]["state"], run["filters"]["date_field"]), ("closed", "closed"))
        self.assertEqual(run["collection_status"], "complete")
        corpus = self.load("relayed", "corpus")
        collector.validate_issue_corpus(corpus, "closed corpus")
        collector.validate_issue_manifest(manifest, "closed manifest")
        self.assertEqual({record["issue"]["state"] for record in corpus["records"]}, {"closed"})
        self.assertTrue(all(record["issue"]["closed_at"] for record in corpus["records"]))
        searched = [r["path"] for r in self.upstream.requests if r["path"].startswith("/search/issues")]
        self.assertEqual(len(searched), 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(searched[0]).query)["q"][0]
        self.assertIn("is:closed", query)
        self.assertIn("closed:", query)
        self.assertFalse([token for token in query.split() if token.startswith("-")])

    def test_the_closed_date_of_open_issues_is_still_refused(self):
        with mock.patch.object(sys, "stderr"):
            code = self.relayed(**{"--state": "open", "--date-field": "closed"})
        self.assertNotEqual(code, 0)
        self.assertEqual(self.upstream.requests, [])


class CredentialScopeTests(Harness):
    """The transport record's credential field speaks for the child only, and says so (#192 follow-up)."""

    world_options = {"graphql_status": 200}

    def test_the_credential_field_says_it_speaks_for_the_child_only(self):
        self.assertEqual(self.relayed(), 0)
        record = self.load("relayed", "transport")
        self.assertEqual(record["credential_scope"], "child")
        self.assertIn("github_token.authenticated", record["credential_scope_note"])
        self.assertTrue(record["credential"].startswith("none: token_provider returned None"))
        self.assertEqual(self.load("relayed", "manifest")["records"][-1]["preflight"]["login"], "anonymous")


class Forbidden403Tests(Harness):
    world_options = {"issue_status": 403}

    def test_an_issue_403_stays_a_partial_record(self):
        direct = self.direct()
        code = self.relayed()
        self.assertEqual((direct.exit_code, code), (3, 3))
        IdentityTests.assert_same(self, direct)
        transport = self.load("relayed", "transport")
        self.assertIn(("upstream-error", 403), [(e["outcome"], e.get("upstream_status")) for e in transport["requests"]])


class BudgetTests(Harness):
    budget = 4

    def test_the_relay_budget_stop_is_the_collectors_budget_stop(self):
        code = self.relayed()
        self.assertNotEqual(code, 0)
        transport = self.load("relayed", "transport")
        self.assertIn("relay-budget-exhausted", [entry.get("outcome") for entry in transport["requests"]])
        self.assertLessEqual(len(self.upstream.requests), 4)


class GraphqlRateLimitTests(Harness):
    """A rate-limit GraphQL answer whose retry wait passes the relay deadline (the
    live 2026-09-28 signature was a status-less relay-timeout, 4 attempts per Issue).
    The relay passes the received status and its safe rate-limit headers on, and the
    collector's own rule waits for them: never sooner than Retry-After or the reset."""

    # Far in real time (the relay's clock) and 600 s after the collector's fixed clock.
    RESET = str(int(FIXED_TIME) + 600)

    def rate_limited(self, status, payload, headers):
        upstream = self.upstream

        class Handler(MockHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                upstream.requests.append({"method": "POST", "path": self.path,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}})
                self._reply(status, payload, headers)
        self.upstream.RequestHandlerClass = Handler
        waits = []
        argv = ["--relay-url", "http://127.0.0.1:%d" % self.port, "--skills-dir", str(SKILLS),
                "--transport-record", str(self.root / "relayed-transport.json"), "--timeout-seconds", "30",
                "--max-response-bytes", str(1 << 20)] + self.collector_argv("relayed")
        code = adapter.run(argv, {}, captured_at=CAPTURED_AT, sleeper=waits.append, clock=lambda: FIXED_TIME)
        transport = self.load("relayed", "transport")
        manifest = self.load("relayed", "manifest")["records"][-1]
        graphql = [entry for entry in transport["requests"] if entry.get("method") == "POST"]
        return code, transport, manifest, graphql, waits

    def assert_waited(self, manifest, graphql, status, delay):
        self.assertEqual({(e["outcome"], e.get("upstream_status")) for e in graphql}, {("upstream-error", status)})
        self.assertTrue(all(e.get("upstream_rate_limit") for e in graphql), graphql)
        events = [e for e in manifest["request_events"] if e["endpoint"] == "/graphql"]
        self.assertEqual([e["status"] for e in events], [status] * 8)
        # Each retry waits the answer's own delay: the relay no longer hides it as a timeout.
        self.assertEqual([e.get("retry_delay") for e in events], [delay, delay, delay, None] * 2)
        self.assertEqual(len(graphql), 8)
        self.assertEqual(sum(r["method"] == "POST" for r in self.upstream.requests), 8)
        self.assertEqual(manifest["request_count"], len(graphql) + 3 + 2 * 3)

    def test_a_403_rate_limit_keeps_its_status_headers_and_reset_wait(self):
        code, transport, manifest, graphql, waits = self.rate_limited(
            403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": self.RESET})
        self.assertEqual(code, 3)
        self.assertEqual(graphql[0]["upstream_rate_limit"], {"x-ratelimit-remaining": "0", "x-ratelimit-reset": self.RESET})
        # The reset is 600 s away: the collector waits exactly that, never sooner.
        self.assert_waited(manifest, graphql, 403, 600.0)
        self.assertEqual(waits, [600.0] * 6)
        self.assertEqual(manifest["request_count"], len(transport["requests"]))

    def test_a_429_keeps_its_status_and_waits_its_retry_after(self):
        code, transport, manifest, graphql, waits = self.rate_limited(
            429, {"message": "Too Many Requests"}, {"Retry-After": "60"})
        self.assertEqual(code, 3)
        self.assertEqual(graphql[0]["upstream_rate_limit"], {"retry-after": "60"})
        self.assert_waited(manifest, graphql, 429, 60.0)
        self.assertEqual(waits, [60.0] * 6)

    def test_a_graphql_403_without_any_quota_is_one_request_and_keeps_the_rest_evidence(self):
        # The measured anonymous answer (2026-09-28): 403, resource graphql, limit 0, remaining 0.
        quota = {"x-ratelimit-resource": "graphql", "x-ratelimit-limit": "0", "x-ratelimit-remaining": "0",
                 "x-ratelimit-used": "0", "x-ratelimit-reset": self.RESET}
        code, transport, manifest, graphql, waits = self.rate_limited(
            403, {"message": "API rate limit exceeded"}, quota)
        self.assertEqual(code, 3)
        self.assertEqual({(e["outcome"], e.get("upstream_status")) for e in graphql}, {("upstream-error", 403)})
        self.assertEqual(graphql[0]["upstream_rate_limit"], quota)
        # One GraphQL request per Issue, end to end: no wait, no retry at any layer.
        self.assertEqual((len(graphql), sum(r["method"] == "POST" for r in self.upstream.requests), waits), (2, 2, []))
        events = [e for e in manifest["request_events"] if e["endpoint"] == "/graphql"]
        self.assertEqual([(e["status"], e.get("retry_delay")) for e in events], [(403, None), (403, None)])
        self.assertEqual(manifest["request_count"], len(transport["requests"]))
        corpus = self.load("relayed", "corpus")
        self.assertEqual(len(corpus["records"]), 2)
        for record in corpus["records"]:
            # The REST evidence is kept; only the GraphQL category is partial.
            self.assertEqual(record["evidence_snapshot"]["partial_categories"], ["graphql"])
            self.assertTrue(record["evidence_snapshot"]["completeness"]["comments"]["pages_complete"])
            self.assertTrue(record["evidence_snapshot"]["completeness"]["timeline"]["pages_complete"])
        self.assertEqual(manifest["collection_status"], "partial")

    def test_an_anonymous_401_is_unchanged(self):
        code, transport, manifest, graphql, waits = self.rate_limited(
            401, {"message": "This endpoint requires you to be authenticated."}, {})
        self.assertEqual({(e["outcome"], e.get("upstream_status")) for e in graphql}, {("upstream-error", 401)})
        self.assertNotIn("upstream_rate_limit", graphql[0])
        self.assertEqual((len(graphql), waits), (2, []))


class AllowlistTests(Harness):
    relay_repositories = ("octo/other",)

    def test_a_repository_outside_the_relay_allowlist_is_a_relay_refusal_not_an_absent_issue(self):
        code = self.relayed()
        self.assertEqual(code, 4)
        transport = self.load("relayed", "transport")
        self.assertIn("relay-refused", [entry.get("outcome") for entry in transport["requests"]])
        self.assertEqual([r["path"].partition("?")[0] for r in self.upstream.requests], ["/versions"])


class RefusalTests(Harness):
    def request(self, url, method="GET", data=None, **headers):
        base = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": collector._sibling.API_VERSION,
                "User-Agent": "dotfiles-oss-research"}
        if method == "POST":
            base["Content-Type"] = "application/json"
        base.update(headers)
        return urllib.request.Request(url, data=data, headers=base, method=method)

    def opener(self):
        return adapter.issue_relay_opener_class(adapter.load_relay_collect())(
            collector, "http://127.0.0.1:%d" % self.port, timeout_seconds=5, max_response_bytes=1 << 20)

    def graphql_body(self, **changes):
        document = {"query": collector.ISSUE_EVIDENCE_QUERY, "variables": {"owner": "octo", "name": "demo", "number": 1}}
        document.update(changes)
        return json.dumps(document).encode("utf-8")

    def test_requests_outside_the_contract_are_refused_before_any_relay_call(self):
        mutation = 'mutation { addComment(input: {subjectId: "x", body: "y"}) { clientMutationId } }'
        cases = {
            "other query": self.request(API + "/graphql", "POST", self.graphql_body(query="query { viewer { login } }")),
            "mutation": self.request(API + "/graphql", "POST", self.graphql_body(query=mutation)),
            "extra variable": self.request(API + "/graphql", "POST", self.graphql_body(
                variables={"owner": "octo", "name": "demo", "number": 1, "first": 100})),
            "extra key": self.request(API + "/graphql", "POST", json.dumps({
                "query": collector.ISSUE_EVIDENCE_QUERY, "variables": {"owner": "octo", "name": "demo", "number": 1},
                "operationName": "IssueEvidence"}).encode()),
            "bool number": self.request(API + "/graphql", "POST", self.graphql_body(
                variables={"owner": "octo", "name": "demo", "number": True})),
            "other post url": self.request(API + "/repos/octo/demo/issues", "POST", self.graphql_body()),
            "authorization on graphql": self.request(API + "/graphql", "POST", self.graphql_body(), Authorization="Bearer x"),
            "authorization on rest": self.request(API + "/versions?per_page=100", Authorization="Bearer x"),
            "put": self.request(API + "/repos/octo/demo/issues/1?per_page=100", "PUT", b"{}"),
            "patch": self.request(API + "/repos/octo/demo/issues/1?per_page=100", "PATCH", b"{}"),
            "other host": self.request("https://api.github.com.evil.test/versions?per_page=100"),
            "api version": self.request(API + "/versions?per_page=100", **{"X-GitHub-Api-Version": "2022-11-28"}),
        }
        for label, request in cases.items():
            with self.subTest(label):
                opener = self.opener()
                with self.assertRaises(ValueError):
                    opener.open(request, 5)
                self.assertEqual(opener.records, [])
        self.assertEqual(self.upstream.requests, [])

    def test_the_step_refuses_to_start_with_tokens_labels_or_existing_outputs(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            with self.subTest(name):
                self.assertEqual(self.relayed(name="tok-" + name, environ={name: "gho_sentinel"}), 2)
                self.assertFalse((self.root / ("tok-%s-corpus.json" % name)).exists())
        self.assertEqual(self.relayed(name="label", **{"--label": "bug"}), 2)
        (self.root / "old-corpus.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.relayed(name="old"), 2)
        self.assertEqual(self.relayed(name="noproxy", extra=("--require-proxy",)), 2)
        # The transport record may not alias the corpus or manifest (absolute or relative spelling).
        corpus = self.root / "alias-corpus.json"
        relative = os.path.relpath(corpus)
        for label, transport in (("same as output", str(corpus)), ("relative alias of output", relative),
                                 ("same as manifest", str(self.root / "alias-manifest.json")),
                                 ("missing directory", str(self.root / "nowhere" / "t.json"))):
            with self.subTest(label):
                argv = ["--relay-url", "http://127.0.0.1:%d" % self.port, "--skills-dir", str(SKILLS),
                        "--transport-record", transport, "--timeout-seconds", "30",
                        "--max-response-bytes", str(1 << 20)] + self.collector_argv("alias")
                self.assertEqual(adapter.run(argv, {}, captured_at=CAPTURED_AT), 2)
                self.assertFalse(corpus.exists())
        self.assertEqual(self.upstream.requests, [])


class RedirectTests(Harness):
    def test_an_upstream_redirect_is_not_followed(self):
        key = ("/repos/%s/issues/2" % TARGET, ())
        self.upstream.routes[key] = {"status": 302, "payload": {"message": "Moved"},
                                     "headers": {"Location": "http://127.0.0.1:9/elsewhere"}}
        code = self.relayed()
        self.assertNotEqual(code, 0)
        self.assertNotIn("/elsewhere", [r["path"] for r in self.upstream.requests])


if __name__ == "__main__":
    unittest.main()
