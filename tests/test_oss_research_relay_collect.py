"""Tests for oss-research-relay-collect: the collector over the loopback relay.

Every run here is local: a loopback mock upstream stands in for
api.github.com, the real relay serves it, and the adapter drives the real
collector. A socket guard refuses any address but 127.0.0.1.
"""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
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
ADAPTER_SCRIPT = ROOT / "dot_local/bin/executable_oss-research-relay-collect"
SKILLS = ROOT / "dot_codex/skills"
API = "https://api.github.com"
REPO = "octo/demo"
INTERVAL = ("2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
CAPTURED_AT = "2026-09-10T00:00:00Z"
FIXED_NOW = "2026-09-10T00:05:00Z"
EPOCH = 1789000000.0

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _loopback_only(address):
    if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
        raise PermissionError("test network guard refused " + repr(address))


def _guarded_connect(self, address):
    _loopback_only(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _loopback_only(address)
    return _real_connect_ex(self, address)


def setUpModule():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


def tearDownModule():
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex


def _load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


relay_module = _load_script("oss_research_relay_for_collect", RELAY_SCRIPT)
adapter = _load_script("oss_research_relay_collect_under_test", ADAPTER_SCRIPT)
issues_module, _ = relay_module.load_issues_module(SKILLS)
collector, collector_source = adapter.load_collector(SKILLS)

_pause = threading.Event().wait


# -- mock upstream -----------------------------------------------------------


def _query_key(query):
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return tuple(sorted((name, value) for name, value in pairs if name != "per_page"))


class MockUpstream(http.server.ThreadingHTTPServer):
    """A loopback api.github.com stand-in with exact (path, query) routes."""

    daemon_threads = True

    def __init__(self, routes):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.routes = routes
        self.requests = []
        self.unmatched = []


class MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return None

    def do_GET(self):
        path, _, query = self.path.partition("?")
        self.server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        route = self.server.routes.get((path, _query_key(query)))
        if route is None:
            self.server.unmatched.append(self.path)
            route = {"status": 404, "payload": {"message": "Not Found"}}
        if route.get("delay"):
            _pause(route["delay"])
        body = json.dumps(route["payload"]).encode("utf-8")
        self.send_response(route.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"etag-{0}"'.format(path.replace("/", "-")))
        for name, value in route.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class UpstreamOpener:
    """Send an api.github.com request to the mock upstream, keeping the logical URL."""

    def __init__(self, upstream):
        self.upstream = upstream
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Refuse)

    def open(self, request, timeout):
        parts = urllib.parse.urlsplit(request.full_url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com", request.full_url
        local = "http://127.0.0.1:{0}{1}?{2}".format(self.upstream.server_address[1], parts.path, parts.query)
        forwarded = urllib.request.Request(local, headers=dict(request.header_items()), method="GET")
        try:
            response = self._opener.open(forwarded, timeout=timeout)
        except urllib.error.HTTPError as error:
            error.url = request.full_url
            raise
        response.url = request.full_url
        return response


def _pull(number):
    return {
        "url": "{0}/repos/{1}/pulls/{2}".format(API, REPO, number), "node_id": "PR_node_%d" % number,
        "number": number, "html_url": "https://github.com/{0}/pull/{1}".format(REPO, number),
        "title": "Relay fixture PR %d" % number, "state": "closed", "merged_at": "2026-09-0%dT10:00:00Z" % (number + 1),
        "created_at": "2026-09-0%dT09:00:00Z" % number, "closed_at": "2026-09-0%dT10:00:00Z" % (number + 1),
        "updated_at": "2026-09-0%dT10:05:00Z" % (number + 1), "body": "Fixture body for PR %d." % number,
        "base": {"sha": "%d" % number * 40, "repo": {"node_id": "R_node"}}, "head": {"sha": "a%d" % number + "0" * 38},
        "merge_commit_sha": "b%d" % number + "0" * 38, "labels": [], "changed_files": 2, "commits": 1,
        "user": {"login": "fixture-author", "node_id": "U_node"}, "author_association": "CONTRIBUTOR",
    }


def world(*, pulls=(1, 2, 3, 4)):
    """Routes for one repository. PR 4's files span two pages to exercise pagination."""
    routes = {}

    def add(path, query, payload, **options):
        routes[(path, _query_key(query))] = dict(payload=payload, **options)

    root = "/repos/" + REPO
    add("/versions", "", [collector.API_VERSION])
    add(root, "", {"full_name": REPO, "node_id": "R_node", "default_branch": "main", "archived": False,
                   "disabled": False, "fork": False, "has_issues": True, "visibility": "public",
                   "pushed_at": "2026-09-06T00:00:00Z", "license": {"spdx_id": "MIT"}, "description": "fixture"})
    add(root + "/license", "", {"url": API + root + "/license", "html_url": "https://github.com/" + REPO + "/blob/main/LICENSE",
                                "license": {"spdx_id": "MIT"}})
    for number in pulls:
        pull = root + "/pulls/%d" % number
        spec = _pull(number)
        add(pull, "", spec)
        files = [{"filename": "src/a%d.py" % number, "status": "modified", "additions": 1, "deletions": 1,
                  "patch": "@@ -1 +1 @@\n-a\n+b"}]
        if number == 4:
            link = '<{0}{1}/files?per_page=100&page=2>; rel="next"'.format(API, pull)
            add(pull + "/files", "page=1", files, headers={"Link": link})
            add(pull + "/files", "page=2", [{"filename": "src/b4.py", "status": "added", "additions": 2,
                                             "deletions": 0, "patch": "@@ -0,0 +1 @@\n+c"}])
        else:
            add(pull + "/files", "page=1", files + [{"filename": "src/c%d.py" % number, "status": "added",
                                                     "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1 @@\n+d"}])
        add(pull + "/commits", "page=1", [{"sha": spec["head"]["sha"], "parents": [{"sha": spec["base"]["sha"]}],
                                           "commit": {"message": "Commit %d" % number,
                                                      "author": {"name": "Fixture", "email": "f@example.invalid",
                                                                 "date": spec["created_at"]}},
                                           "author": {"login": "fixture-author", "node_id": "U_node"}}])
        add(root + "/issues/%d/comments" % number, "page=1", [])
        add(pull + "/reviews", "page=1", [])
        add(pull + "/comments", "page=1", [])
        add(root + "/issues/%d/timeline" % number, "page=1", [{"event": "merged", "created_at": spec["closed_at"]}])
    interval = interval_for()
    query = collector.build_closed_query(REPO, interval, "all")
    items = [{"node_id": "PR_node_%d" % n, "number": n, "title": "Relay fixture PR %d" % n,
              "closed_at": _pull(n)["closed_at"], "html_url": _pull(n)["html_url"],
              "repository_url": API + root, "pull_request": {"html_url": _pull(n)["html_url"]}} for n in pulls]
    add("/search/issues", urllib.parse.urlencode({"q": query, "page": 1}),
        {"total_count": len(items), "incomplete_results": False, "items": items})
    return routes


def interval_for():
    return collector.resolve_interval(start_at=INTERVAL[0], end_at=INTERVAL[1], start_date=None, end_date=None,
                                      recent_days=None, timezone_name="UTC", as_of=None)


# -- harness -----------------------------------------------------------------


class RelayCollectHarness(unittest.TestCase):
    relay_budget = 200
    response_byte_budget = 1024 * 1024
    max_response_bytes = 64 * 1024
    relay_timeout = 5.0
    relay_repos = (REPO,)

    def setUp(self):
        environment = mock.patch.dict(os.environ, {
            "GH_TOKEN": "gho_parentsecret", "GITHUB_TOKEN": "ghp_parentsecret",
            "HTTPS_PROXY": "http://10.0.0.1:3128", "HTTP_PROXY": "http://10.0.0.1:3128",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for patcher in (
            mock.patch.object(collector, "_github_token", side_effect=AssertionError("token lookup")),
            mock.patch.object(collector, "_run_timestamp", lambda value: value or FIXED_NOW),
            mock.patch.object(relay_module.time, "sleep", lambda seconds: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.start(world())

    def start(self, routes):
        self.upstream = MockUpstream(routes)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        argv = []
        for repository in self.relay_repos:
            argv += ["--repo", repository]
        args = relay_module.build_parser().parse_args(argv + [
            "--request-budget", str(self.relay_budget),
            "--max-response-bytes", str(self.max_response_bytes),
            "--response-byte-budget", str(self.response_byte_budget),
            "--timeout-seconds", str(self.relay_timeout),
        ])
        self.server = relay_module.build_server(args, opener=UpstreamOpener(self.upstream), issues_module=issues_module)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.relay_url = "http://127.0.0.1:{0}".format(self.server.server_address[1])

    def collect(self, client, name, *, repositories=(REPO,), max_per_repository=3):
        paths = {key: self.directory / "{0}-{1}".format(name, key) for key in ("corpus.json", "manifest.json", "inventory.md")}
        run = collector.collect(
            client, list(repositories), interval_for(), outcome="all", max_per_repository=max_per_repository,
            request_budget=client.budget.limit, output=paths["corpus.json"], manifest_output=paths["manifest.json"],
            markdown_output=paths["inventory.md"], captured_at=CAPTURED_AT, api_version=collector.API_VERSION,
        )
        return run, paths

    def relayed(self, *, budget=200, repositories=(REPO,), max_per_repository=3):
        opener = adapter.RelayOpener(collector, self.relay_url, timeout_seconds=10,
                                     max_response_bytes=self.max_response_bytes)
        client = adapter.build_client(collector, opener, api_version=collector.API_VERSION, request_budget=budget,
                                      sleeper=lambda seconds: None, clock=lambda: EPOCH)
        run, paths = self.collect(client, "relayed", repositories=repositories, max_per_repository=max_per_repository)
        return run, paths, opener, client

    def direct(self, *, budget=200):
        client = collector.GhApiClient(api_version=collector.API_VERSION, budget=collector.RequestBudget(budget),
                                       sleeper=lambda seconds: None, clock=lambda: EPOCH,
                                       opener=UpstreamOpener(self.upstream), token_provider=lambda: None)
        run, paths = self.collect(client, "direct")
        return run, paths, client


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _without_relay_client_version(value, direct_version):
    """Map the one allowed difference, the relayed client_version, back to the direct one."""
    if isinstance(value, dict):
        return {key: (direct_version if key == "client_version" and item == direct_version + adapter.CLIENT_VERSION_SUFFIX
                      else _without_relay_client_version(item, direct_version)) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_relay_client_version(item, direct_version) for item in value]
    return value


# -- tests -------------------------------------------------------------------


class OutputIdentityTests(RelayCollectHarness):
    def test_relayed_collection_matches_direct_except_client_version(self):
        direct_run, direct_paths, direct_client = self.direct()
        direct_requests = len(self.upstream.requests)
        self.upstream.requests.clear()
        relayed_run, relayed_paths, opener, relayed_client = self.relayed()

        self.assertEqual(self.upstream.unmatched, [])
        self.assertEqual((direct_run.exit_code, relayed_run.exit_code), (0, 0))
        direct_manifest = _load(direct_paths["manifest.json"])
        relayed_manifest = _load(relayed_paths["manifest.json"])
        self.assertEqual(direct_manifest["generated_by"]["client_version"], "python-urllib")
        self.assertEqual(relayed_manifest["generated_by"]["client_version"], "python-urllib+oss-research-relay")
        self.assertEqual(_without_relay_client_version(relayed_manifest, "python-urllib"), direct_manifest)
        self.assertEqual(_load(relayed_paths["corpus.json"]), _load(direct_paths["corpus.json"]))
        self.assertEqual(relayed_paths["inventory.md"].read_text(encoding="utf-8").replace("+oss-research-relay", ""),
                         direct_paths["inventory.md"].read_text(encoding="utf-8"))

        # The newest three PRs of the four hits, and PR 4's second files page came through the relay.
        corpus = _load(relayed_paths["corpus.json"])
        self.assertEqual(len(corpus["records"]), 3)
        paths = [request["path"] for request in self.upstream.requests]
        self.assertIn("/repos/octo/demo/pulls/4/files?per_page=100&page=2", paths)
        # The relay made exactly the collector's requests, all anonymous, all with per_page=100.
        self.assertEqual(len(self.upstream.requests), direct_requests)
        self.assertEqual(relayed_client.budget.consumed, direct_client.budget.consumed)
        self.assertEqual(self.server.relay.budget.consumed, relayed_client.budget.consumed)
        for request in self.upstream.requests:
            self.assertNotIn("authorization", request["headers"])
            self.assertIn("per_page=100", request["path"])
        # The transport record names the logical URL and the relay target, never a TLS connection.
        record = adapter.transport_record(opener, collector_source, relayed_run)
        self.assertIn("not TLS evidence", record["note"])
        self.assertEqual(len(record["requests"]), relayed_client.budget.consumed)
        for entry in record["requests"]:
            self.assertTrue(entry["logical_url"].startswith(API + "/"))
            self.assertTrue(entry["relay_target"].startswith("/v1/rest/"))
            self.assertNotIn("per_page", entry["relay_target"])
            self.assertEqual(entry["outcome"], "relayed")
        collector._github_token.assert_not_called()

    def test_upstream_not_found_keeps_its_status_and_output(self):
        del self.upstream.routes[("/repos/octo/demo/pulls/2/reviews", (("page", "1"),))]
        direct_run, direct_paths, _ = self.direct()
        self.upstream.unmatched.clear()
        relayed_run, relayed_paths, opener, _ = self.relayed()
        self.assertEqual((direct_run.exit_code, relayed_run.exit_code), (3, 3))
        self.assertEqual(_load(relayed_paths["corpus.json"]), _load(direct_paths["corpus.json"]))
        self.assertEqual(_without_relay_client_version(_load(relayed_paths["manifest.json"]), "python-urllib"),
                         _load(direct_paths["manifest.json"]))
        failed = [entry for entry in opener.records if entry["outcome"] == "upstream-error"]
        self.assertEqual([(entry["relay_status"], entry["upstream_status"]) for entry in failed], [(502, 404)])

    def test_upstream_server_error_retries_multiply_but_stay_in_the_relay_budget(self):
        key = ("/repos/octo/demo/pulls/2/reviews", (("page", "1"),))
        self.upstream.routes[key] = {"status": 500, "payload": {"message": "Server Error"}}
        _, direct_paths, _ = self.direct()
        direct_hits = sum(1 for request in self.upstream.requests if request["path"].startswith("/repos/octo/demo/pulls/2/reviews"))
        self.upstream.requests.clear()
        relayed_run, relayed_paths, opener, client = self.relayed()
        relayed_hits = sum(1 for request in self.upstream.requests if request["path"].startswith("/repos/octo/demo/pulls/2/reviews"))
        # The collector retries 4 times either way; each relayed attempt is itself up to 4 upstream attempts.
        self.assertEqual((direct_hits, relayed_hits), (4, 16))
        self.assertEqual(self.server.relay.budget.consumed, len(self.upstream.requests))
        self.assertEqual(_load(relayed_paths["corpus.json"]), _load(direct_paths["corpus.json"]))


class RateLimitPassThroughTests(RelayCollectHarness):
    """A rate-limited answer whose retry wait passes the relay deadline reaches the
    collector as that answer (status and safe rate-limit headers), so the collector's
    own rule decides the wait, exactly as on a direct connection."""

    def rate_limit_reviews(self, status, headers):
        key = ("/repos/octo/demo/pulls/2/reviews", (("page", "1"),))
        self.upstream.routes[key] = {"status": status, "payload": {"message": "API rate limit exceeded"},
                                     "headers": headers}

    def assert_same_as_direct(self):
        _, direct_paths, direct_client = self.direct()
        direct_hits = sum(r["path"].startswith("/repos/octo/demo/pulls/2/reviews") for r in self.upstream.requests)
        self.upstream.requests.clear()
        relayed_run, relayed_paths, opener, client = self.relayed()
        relayed_hits = sum(r["path"].startswith("/repos/octo/demo/pulls/2/reviews") for r in self.upstream.requests)
        # One upstream request per collector attempt: the relay does not retry past its deadline.
        self.assertEqual((direct_hits, relayed_hits), (4, 4))
        self.assertEqual(_load(relayed_paths["corpus.json"]), _load(direct_paths["corpus.json"]))
        self.assertEqual(_without_relay_client_version(_load(relayed_paths["manifest.json"]), "python-urllib"),
                         _load(direct_paths["manifest.json"]))
        waits = [e.get("retry_delay") for e in client.request_events if e["endpoint"].endswith("/pulls/2/reviews")]
        self.assertEqual(waits, [e.get("retry_delay") for e in direct_client.request_events
                                 if e["endpoint"].endswith("/pulls/2/reviews")])
        return [entry for entry in opener.records if entry["outcome"] == "upstream-error"], waits

    def test_a_403_rate_limit_keeps_its_reset_and_waits_like_a_direct_run(self):
        # Both clocks fixed at EPOCH (the relay's inner clients too), so the reset is exactly 600 s away.
        for client in (self.server.relay.rest, self.server.relay.graphql):
            client._clock = lambda: EPOCH
        reset = str(int(EPOCH) + 600)
        self.rate_limit_reviews(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})
        failed, waits = self.assert_same_as_direct()
        self.assertEqual({(e["upstream_status"], e["upstream_rate_limit"]["x-ratelimit-reset"]) for e in failed},
                         {(403, reset)})
        self.assertEqual(waits, [600.0, 600.0, 600.0, None])  # the server's reset, never sooner

    def test_a_429_keeps_its_retry_after_and_waits_like_a_direct_run(self):
        self.rate_limit_reviews(429, {"Retry-After": "60"})
        failed, waits = self.assert_same_as_direct()
        self.assertEqual({(e["upstream_status"], e["upstream_rate_limit"]["retry-after"]) for e in failed}, {(429, "60")})
        self.assertEqual(waits, [60.0, 60.0, 60.0, None])

    def test_a_malformed_kept_rate_limit_is_a_broken_envelope(self):
        opener = adapter.RelayOpener(collector, self.relay_url, timeout_seconds=5, max_response_bytes=1024)
        request = urllib.request.Request(API + "/versions?per_page=100", headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": collector.API_VERSION})
        for bad in ({"set-cookie": "a=b"}, {"retry-after": "60; x"}, {"retry-after": 60}, ["retry-after"],
                    {"x-ratelimit-resource": "Core!"}):
            with self.subTest(bad=bad):
                envelope = {"ok": False, "error": "upstream_failed", "message": "upstream answered HTTP 429",
                            "upstream_status": 429, "upstream_rate_limit": bad}
                with mock.patch.object(opener, "_exchange", return_value=(502, envelope)):
                    with self.assertRaises(ValueError):
                        opener.open(request)
                self.assertEqual(opener.records[-1]["outcome"], "malformed_envelope")


class BudgetAndLimitTests(RelayCollectHarness):
    relay_budget = 6

    def test_relay_budget_exhaustion_stops_retries_and_keeps_the_429(self):
        run, paths, opener, client = self.relayed()
        self.assertEqual(len(self.upstream.requests), 6)
        self.assertEqual(self.server.relay.budget.consumed, 6)
        exhausted = [entry for entry in opener.records if entry["outcome"] == "relay-budget-exhausted"]
        self.assertTrue(exhausted)
        for entry in exhausted:
            self.assertEqual((entry["relay_status"], entry["relay_error"]), (429, "budget_exhausted"))
        # BudgetExhausted is not retried: no logical request is sent twice.
        urls = [entry["logical_url"] for entry in opener.records]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertNotEqual(run.exit_code, 0)
        manifest_text = paths["manifest.json"].read_text(encoding="utf-8")
        self.assertIn("relay-budget-exhausted: relay answered 429 budget_exhausted", manifest_text)

    def test_collector_budget_still_applies(self):
        run, _, opener, client = self.relayed(budget=4)
        self.assertEqual(client.budget.consumed, 4)
        self.assertEqual(len(opener.records), 4)
        self.assertEqual(len(self.upstream.requests), 4)
        self.assertNotEqual(run.exit_code, 0)


class ByteBudgetTests(RelayCollectHarness):
    response_byte_budget = 1500

    def test_byte_budget_exhaustion_is_a_budget_stop(self):
        run, _, opener, _ = self.relayed()
        exhausted = [entry for entry in opener.records if entry.get("relay_error") == "byte_budget_exhausted"]
        self.assertTrue(exhausted)
        self.assertTrue(all(entry["outcome"] == "relay-budget-exhausted" for entry in exhausted))
        urls = [entry["logical_url"] for entry in opener.records]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertNotEqual(run.exit_code, 0)


class TimeoutTests(RelayCollectHarness):
    relay_timeout = 0.3

    def test_relay_timeout_is_a_bounded_transport_failure(self):
        self.upstream.routes[("/repos/octo/demo/license", ())]["delay"] = 0.8
        run, paths, opener, client = self.relayed()
        timeouts = [entry for entry in opener.records if entry["outcome"] == "relay-timeout"]
        # The collector retries a transport failure MAX_RETRIES times, then records it.
        self.assertEqual(len(timeouts), collector.MAX_RETRIES + 1)
        self.assertTrue(all(entry["relay_status"] == 504 for entry in timeouts))
        self.assertLessEqual(self.server.relay.budget.consumed, self.relay_budget)
        self.assertEqual(run.exit_code, 3)


class AllowlistTests(RelayCollectHarness):
    def test_repository_outside_the_relay_allowlist_is_refused_without_upstream(self):
        run, _, opener, _ = self.relayed(repositories=("octo/other",))
        refused = [entry for entry in opener.records if entry["outcome"] == "relay-refused"]
        self.assertEqual([entry["relay_error"] for entry in refused], ["repository_not_allowed"])
        self.assertEqual([request["path"].partition("?")[0] for request in self.upstream.requests], ["/versions"])
        self.assertEqual(run.exit_code, 4)


class AdapterRefusalTests(unittest.TestCase):
    def setUp(self):
        self.opener = adapter.RelayOpener(collector, "http://127.0.0.1:9", timeout_seconds=1, max_response_bytes=1024)

    def request(self, url, method="GET", data=None, **headers):
        base = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": collector.API_VERSION,
                "User-Agent": "dotfiles-oss-research"}
        base.update(headers)
        return urllib.request.Request(url, data=data, headers=base, method=method)

    def test_collector_request_maps_to_one_relay_target(self):
        request = self.request(API + "/repos/octo/demo/pulls/1/files?per_page=100&page=2")
        self.assertEqual(self.opener.relay_target(request),
                         (API + "/repos/octo/demo/pulls/1/files?per_page=100&page=2",
                          "/v1/rest/repos/octo/demo/pulls/1/files?page=2"))
        self.assertEqual(self.opener.relay_target(self.request(API + "/versions?per_page=100"))[1], "/v1/rest/versions")

    def test_requests_outside_the_contract_are_refused_before_any_connection(self):
        cases = {
            "other host": self.request("https://api.github.com.evil.test/versions?per_page=100"),
            "userinfo": self.request("https://user:pw@api.github.com/versions?per_page=100"),
            "port": self.request("https://api.github.com:443/versions?per_page=100"),
            "plain http": self.request("http://api.github.com/versions?per_page=100"),
            "fragment": self.request(API + "/versions?per_page=100#x"),
            "post": self.request(API + "/graphql?per_page=100", method="POST", data=b"{}"),
            "authorization": self.request(API + "/versions?per_page=100", Authorization="Bearer x"),
            "conditional": self.request(API + "/versions?per_page=100", **{"If-None-Match": '"e"'}),
            "cookie": self.request(API + "/versions?per_page=100", Cookie="a=b"),
            "api version": self.request(API + "/versions?per_page=100", **{"X-GitHub-Api-Version": "2022-11-28"}),
            "repeated query": self.request(API + "/search/issues?per_page=100&q=a&q=b"),
            "no per_page": self.request(API + "/versions"),
            "other per_page": self.request(API + "/versions?per_page=5"),
            "malformed query": self.request(API + "/versions?per_page=100&&"),
        }
        for label, request in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.opener.open(request, timeout=1)
        self.assertEqual(self.opener.records, [])

    def test_relay_url_must_be_exact_loopback_ipv4_and_port(self):
        for value in ("http://localhost:8080", "http://127.0.0.2:8080", "http://[::1]:8080", "https://127.0.0.1:8080",
                      "http://127.0.0.1", "http://127.0.0.1:0", "http://u@127.0.0.1:8080", "http://127.0.0.1:8080/v1",
                      "http://127.0.0.1:8080?x=1", "http://127.0.0.1:8080#f", "http://127.0.0.1:99999"):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    adapter.parse_relay_url(value)
        self.assertEqual(adapter.parse_relay_url("http://127.0.0.1:8080"), ("127.0.0.1", 8080))


class CommandLineTests(RelayCollectHarness):
    def argv(self, name, **changes):
        options = {
            "--relay-url": self.relay_url, "--skills-dir": str(SKILLS),
            "--transport-record": str(self.directory / (name + "-transport.json")),
            "--timeout-seconds": "10", "--max-response-bytes": str(self.max_response_bytes),
            "--repo": REPO, "--output": str(self.directory / (name + "-corpus.json")),
            "--manifest": str(self.directory / (name + "-manifest.json")),
            "--max-per-repo": "3", "--request-budget": "200",
            "--start-at": INTERVAL[0], "--end-at": INTERVAL[1],
        }
        options.update(changes)
        argv = []
        for key, value in options.items():
            argv += [key, value]
        return argv

    def without_proxy_variables(self):
        names = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
        return mock.patch.dict(os.environ, {name: value for name, value in os.environ.items()
                                            if name not in names}, clear=True)

    def test_main_collects_through_the_relay_and_writes_the_transport_record(self):
        with self.without_proxy_variables(), mock.patch.object(sys, "stderr"):
            code = adapter.main(self.argv("cli"))
        self.assertEqual(code, 0)
        record = _load(self.directory / "cli-transport.json")
        self.assertEqual(record["collector"]["sha256"], collector_source["sha256"])
        self.assertEqual(record["transport"], "direct")
        self.assertTrue(all(entry["outcome"] == "relayed" for entry in record["requests"]))
        self.assertEqual(len(_load(self.directory / "cli-corpus.json")["records"]), 3)

    def test_an_ambient_non_loopback_proxy_fails_closed_without_a_direct_fallback(self):
        # setUp leaves HTTP_PROXY=http://10.0.0.1:3128 in the environment, as a parent might.
        with mock.patch.object(sys, "stderr") as stderr:
            self.assertEqual(adapter.main(self.argv("ambient")), 2)
        self.assertEqual(self.upstream.requests, [])
        self.assertNotIn("10.0.0.1", "".join(str(c) for c in stderr.mock_calls))
        self.assertFalse((self.directory / "ambient-corpus.json").exists())

    def test_main_refuses_existing_outputs_and_bad_relay_urls(self):
        existing = self.directory / "old-corpus.json"
        existing.write_text("{}", encoding="utf-8")
        with mock.patch.object(sys, "stderr"), self.assertRaises(SystemExit) as raised:
            adapter.main(self.argv("old", **{"--output": str(existing)}))
        self.assertEqual(raised.exception.code, 2)
        with mock.patch.object(sys, "stderr"):
            self.assertEqual(adapter.main(self.argv("bad", **{"--relay-url": "http://localhost:1"})), 2)
        with mock.patch.object(sys, "stderr"), self.assertRaises(SystemExit):
            adapter.main(self.argv("ver", **{"--api-version": "2022-11-28"}))
        self.assertEqual(self.upstream.requests, [])


PROXY_USER = "srtuser-7f3a"
PROXY_PASSWORD = "pa@ss-91c2"  # percent-encoded in the URL
PROXY_BASIC = "Basic " + __import__("base64").b64encode(
    (PROXY_USER + ":" + PROXY_PASSWORD).encode()).decode()
PROXY_SECRETS = (PROXY_USER, PROXY_PASSWORD, "pa%40ss-91c2", PROXY_BASIC, PROXY_BASIC.split()[1])


class LoopbackProxy(http.server.ThreadingHTTPServer):
    """Stands in for srt's in-sandbox HTTP proxy: session Basic auth, then absolute-form
    GETs to one relay, forwarded without the proxy header."""

    daemon_threads = True

    def __init__(self, relay_port):
        super().__init__(("127.0.0.1", 0), LoopbackProxyHandler)
        self.relay_port = relay_port
        self.requests = []
        self.redirect = False


class LoopbackProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return None

    def do_GET(self):
        self.server.requests.append(self.path)
        parts = urllib.parse.urlsplit(self.path)
        if self.headers.get("Proxy-Authorization") != PROXY_BASIC:
            body = b"proxy authentication required"
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="srt"')
        elif self.server.redirect:
            body = b"{}"
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        elif parts.scheme != "http" or parts.netloc != "127.0.0.1:%d" % self.server.relay_port:
            body = b"{}"
            self.send_response(403)
        else:
            import http.client
            upstream = http.client.HTTPConnection("127.0.0.1", self.server.relay_port, timeout=10)
            target = parts.path + ("?" + parts.query if parts.query else "")
            upstream.request("GET", target, headers={"Accept": "application/json"})
            answer = upstream.getresponse()
            body = answer.read()
            upstream.close()
            self.send_response(answer.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyTransportTests(CommandLineTests):
    """srt's loopback HTTP proxy is the only way to the relay inside srt on Linux."""

    def setUp(self):
        super().setUp()
        self.proxy = LoopbackProxy(self.server.server_address[1])
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def run_with(self, name, variables, *extra):
        with self.without_proxy_variables(), mock.patch.dict(os.environ, variables), \
                mock.patch.object(sys, "stderr") as stderr:
            code = adapter.main(self.argv(name) + list(extra))
        return code, "".join(str(call) for call in stderr.mock_calls)

    def proxy_url(self, host="127.0.0.1", userinfo=PROXY_USER + ":pa%40ss-91c2@"):
        return "http://%s%s:%d" % (userinfo, host, self.proxy.server_address[1])

    def assert_no_secret(self, *texts):
        for text in texts:
            for secret in PROXY_SECRETS:
                self.assertNotIn(secret, text)

    def written(self, name):
        return "".join(path.read_text() for path in self.directory.glob(name + "-*"))

    def test_collects_through_the_proxy_in_absolute_form_and_ignores_no_proxy(self):
        for spelling in ("127.0.0.1", "localhost"):
            with self.subTest(spelling):
                value = self.proxy_url(spelling)
                self.proxy.requests.clear()
                name = "proxy-" + spelling.replace(".", "")
                self.upstream.requests.clear()
                code, stderr = self.run_with(name, {"HTTP_PROXY": value, "http_proxy": value,
                                                    "NO_PROXY": "127.0.0.1,localhost"}, "--require-proxy")
                self.assertEqual(code, 0)
                # The session credential reaches only the proxy: never the relay's upstream,
                # the transport record, the corpus/manifest/inventory or stderr.
                for seen in self.upstream.requests:
                    self.assertNotIn("proxy-authorization", seen["headers"])
                    self.assertNotIn("authorization", seen["headers"])
                self.assert_no_secret(stderr, self.written(name))
                record = _load(self.directory / (name + "-transport.json"))
                self.assertEqual((record["transport"], record["no_proxy_ignored"]), ("srt-proxy", True))
                self.assertEqual(len(_load(self.directory / (name + "-corpus.json"))["records"]), 3)
                relay = "http://127.0.0.1:%d/v1/rest/" % self.server.server_address[1]
                self.assertEqual(len(self.proxy.requests), len(record["requests"]))
                self.assertTrue(all(path.startswith(relay) for path in self.proxy.requests), self.proxy.requests)
                self.assertNotIn(str(self.proxy.server_address[1]), json.dumps(record))

    def test_a_bad_or_conflicting_proxy_fails_closed_with_no_request(self):
        port = self.proxy.server_address[1]
        good = "http://127.0.0.1:%d" % port
        cases = {
            "https": {"HTTP_PROXY": "https://127.0.0.1:%d" % port},
            "userinfo on a non-loopback proxy": {"HTTP_PROXY": "http://user:pw@10.0.0.1:%d" % port},
            "user without password": {"HTTP_PROXY": "http://user@127.0.0.1:%d" % port},
            "empty userinfo": {"HTTP_PROXY": "http://@127.0.0.1:%d" % port},
            "not loopback": {"HTTP_PROXY": "http://10.0.0.1:%d" % port},
            "no port": {"HTTP_PROXY": "http://127.0.0.1"},
            "port zero": {"HTTP_PROXY": "http://127.0.0.1:0"},
            "path": {"HTTP_PROXY": good + "/p"},
            "query": {"HTTP_PROXY": good + "?x=1"},
            "fragment": {"HTTP_PROXY": good + "#f"},
            "empty": {"HTTP_PROXY": ""},
            "lowercase only, bad": {"http_proxy": "http://[::1]:%d" % port},
            "spellings differ": {"HTTP_PROXY": good, "http_proxy": "http://localhost:%d" % port},
            # Parser errors whose own messages carry the raw netloc (never shown).
            "NFKC netloc": {"HTTP_PROXY": "http://leak-user:leak\uff03pw@127.0.0.1:%d" % port},
            "non-numeric port": {"HTTP_PROXY": "http://leak-user:leakpw@127.0.0.1:12ab"},
            "port out of range": {"HTTP_PROXY": "http://leak-user:leakpw@127.0.0.1:99999"},
            "unclosed IPv6": {"HTTP_PROXY": "http://leak-user:leakpw@[::1"},
            "bad percent escape": {"HTTP_PROXY": "http://leak-user:%%ff%%fe@127.0.0.1:%d" % port},
        }
        for label, variables in cases.items():
            with self.subTest(label):
                self.proxy.requests.clear()
                self.upstream.requests.clear()
                code, stderr = self.run_with("bad-" + label.replace(" ", "-").replace(",", ""), variables)
                self.assertEqual(code, 2)
                self.assertEqual((self.proxy.requests, self.upstream.requests), ([], []))
                # The fixed message names the accepted forms; the value itself never shows.
                stderr = stderr.replace("http://localhost:<port> or http://127.0.0.1:<port>", "")
                for value in variables.values():
                    if value:
                        self.assertNotIn(value, stderr)
                self.assertNotIn("leak", stderr)

    def test_a_407_or_a_redirect_from_the_proxy_fails_closed(self):
        wrong = self.proxy_url(userinfo="srtuser-7f3a:wrong-91c2@")
        for label, value, redirect in (("no credential", self.proxy_url(userinfo=""), False),
                                       ("wrong credential", wrong, False),
                                       ("redirect", self.proxy_url(), True)):
            with self.subTest(label):
                self.proxy.redirect = redirect
                self.upstream.requests.clear()
                name = "fail-" + label.replace(" ", "-")
                code, stderr = self.run_with(name, {"HTTP_PROXY": value}, "--require-proxy")
                self.assertNotEqual(code, 0)
                self.assertEqual(self.upstream.requests, [])
                record = _load(self.directory / (name + "-transport.json"))
                self.assertEqual(record["transport"], "srt-proxy")
                self.assertTrue(record["requests"])
                self.assertTrue(all(entry.get("outcome") != "relayed" for entry in record["requests"]))
                self.assert_no_secret(stderr, self.written(name))
                self.assertNotIn("wrong-91c2", stderr + self.written(name))
        self.proxy.redirect = False

    def test_the_isolated_step_requires_the_proxy(self):
        code, stderr = self.run_with("required", {}, "--require-proxy")
        self.assertEqual(code, 2)
        self.assertIn("--require-proxy", stderr)
        self.assertEqual(self.upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
