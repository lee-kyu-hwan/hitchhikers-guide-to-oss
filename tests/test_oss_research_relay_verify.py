"""Tests for the candidate verifier's relay path (oss-research-relay-verify).

Every run is local: a loopback mock stands in for api.github.com, the real
relay serves it, and the adapter drives the real verifier's own main. A socket
guard refuses any address but 127.0.0.1. Nothing reads a token.
"""

from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
import http.client
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
RELAY_SCRIPT = ROOT / "bin/oss-research-relay"
ADAPTER_SCRIPT = ROOT / "bin/oss-research-relay-verify"
SKILLS = ROOT / "skills"
ANALYSIS = SKILLS / "verifying-open-source-contribution-candidates/tests/fixtures/common/analysis.json"
API = "https://api.github.com"
TARGET = "octo/demo"
ORG = "octo/.github"
HEAD = "0123456789abcdef0123456789abcdef01234567"
CLUES = ("validation boundary", "invalid input", "validation error")  # PAT-001's search clues
POLICY_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "SECURITY.md",
                ".github/SECURITY.md", "docs/SECURITY.md", ".github/PULL_REQUEST_TEMPLATE.md",
                ".github/ISSUE_TEMPLATE", "CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md")
FIXED_TIME = 946684800.0  # 2000-01-01T00:00:00Z

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


relay_module = _load("oss_research_relay_for_verify", RELAY_SCRIPT)
adapter = _load("oss_research_relay_verify_under_test", ADAPTER_SCRIPT)
issues_module, _ = relay_module.load_issues_module(SKILLS)
verifier, verifier_source = adapter.load_verifier(SKILLS)

_pause = threading.Event().wait


# -- mock api.github.com -------------------------------------------------------


def _query_key(query):
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return tuple(sorted((name, value) for name, value in pairs if name != "per_page"))


def world(*, org_branch="trunk", pvr_status=200, code_status=401, fork_parent=None, org_exists=True):
    """Routes for the verifier's R3 requests about TARGET, with realistic url fields."""
    routes = {}

    def add(path, params, payload, status=200):
        routes[(path, _query_key(urllib.parse.urlencode(params or {})))] = {"status": status, "payload": payload}

    def repository(full_name, branch):
        return {"node_id": "NODE-" + full_name.replace("/", "-"), "full_name": full_name, "archived": False,
                "disabled": False, "fork": fork_parent is not None and full_name == TARGET,
                "parent": {"full_name": fork_parent, "node_id": "NODE-P"} if fork_parent and full_name == TARGET
                else None, "has_issues": True, "default_branch": branch, "pushed_at": "2000-01-01T00:00:00Z",
                "visibility": "public", "license": {"spdx_id": "MIT"}}

    add("/repos/" + TARGET, None, repository(TARGET, "main"))
    if org_exists:
        add("/repos/" + ORG, None, repository(ORG, org_branch))
    add("/repos/%s/commits/main" % TARGET, None, {"sha": HEAD, "url": "%s/repos/%s/commits/%s" % (API, TARGET, HEAD)})
    add("/repos/%s/community/profile" % TARGET, None,
        {"health_percentage": 50, "files": {"contributing": {"url": API + "/repos/%s/contents/CONTRIBUTING.md" % TARGET},
                                            "code_of_conduct": None, "issue_template": None,
                                            "pull_request_template": None, "license": None, "readme": None}})
    if pvr_status == 200:
        add("/repos/%s/private-vulnerability-reporting" % TARGET, None, {"enabled": False})
    else:
        add("/repos/%s/private-vulnerability-reporting" % TARGET, None,
            {"message": "Must have admin rights to Repository."}, status=pvr_status)
    guide = b"Please open an issue first. Do not submit AI-generated pull requests without review."
    add("/repos/%s/contents/CONTRIBUTING.md" % TARGET, {"ref": "main"}, {
        "type": "file", "encoding": "base64", "content": base64.b64encode(guide).decode(), "size": len(guide),
        "sha": "f" * 40, "path": "CONTRIBUTING.md", "name": "CONTRIBUTING.md",
        "url": "%s/repos/%s/contents/CONTRIBUTING.md?ref=main" % (API, TARGET),
        "html_url": "https://github.com/%s/blob/main/CONTRIBUTING.md" % TARGET})
    add("/repos/%s/contents/.github/ISSUE_TEMPLATE" % TARGET, {"ref": "main"}, [{
        "type": "file", "name": "bug.md", "path": ".github/ISSUE_TEMPLATE/bug.md", "sha": "e" * 40, "size": 10,
        "url": "%s/repos/%s/contents/.github/ISSUE_TEMPLATE/bug.md?ref=main" % (API, TARGET),
        "html_url": "https://github.com/%s/blob/main/.github/ISSUE_TEMPLATE/bug.md" % TARGET}])
    for clue in CLUES:
        for kind in ("issue", "pr"):
            for state in ("open", "closed"):
                items = []
                if clue == CLUES[0] and kind == "issue" and state == "closed":
                    items = [{"number": 7, "title": "Validation boundary", "state": "closed", "state_reason": "completed",
                              "html_url": "https://github.com/%s/issues/7" % TARGET, "created_at": "1999-12-01T00:00:00Z",
                              "closed_at": "1999-12-02T00:00:00Z", "pull_request": None,
                              "repository_url": "%s/repos/%s" % (API, TARGET)}]
                add("/search/issues", {"q": 'repo:%s "%s" is:%s is:%s' % (TARGET, clue, kind, state)},
                    {"total_count": len(items), "incomplete_results": False, "items": items})
        if code_status == 200:
            add("/search/code", {"q": 'repo:%s "%s"' % (TARGET, clue)},
                {"total_count": 0, "incomplete_results": False, "items": []})
        else:
            add("/search/code", {"q": 'repo:%s "%s"' % (TARGET, clue)},
                {"message": "Requires authentication"}, status=code_status)
    return routes


STATIC_README = b"# demo\n\n## Install\n\n```sh\nnpm install demo-plugin\n```\n\n- [one](docs/rules/one.md)\n" \
    b"- [gone](docs/rules/gone.md)\n- [site](https://example.com/x)\n"
STATIC_DOCS = {"one.md": b"# one\n\n## Rule Details\n", "two.md": b"# two\n\n## Options\n\nNone.\n"}


def add_static_routes(routes, readme=STATIC_README, docs=None, directory="docs/rules"):
    """README.md and one rule directory at HEAD, the way the contents route answers them."""
    docs = STATIC_DOCS if docs is None else docs

    def url(path):
        return "%s/repos/%s/contents/%s?ref=%s" % (API, TARGET, path, HEAD)

    def file(path, data):
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode(), "size": len(data),
                "sha": "%040x" % len(path), "path": path, "name": path.rsplit("/", 1)[-1], "url": url(path),
                "html_url": "https://github.com/%s/blob/%s/%s" % (TARGET, HEAD, path)}

    def add(path, payload):
        routes[("/repos/%s/contents/%s" % (TARGET, path), _query_key("ref=" + HEAD))] = {"status": 200, "payload": payload}

    add("README.md", file("README.md", readme))
    add(directory, [{"type": "file", "name": name, "path": directory + "/" + name, "sha": "%040x" % index,
                     "size": len(data), "url": url(directory + "/" + name)}
                    for index, (name, data) in enumerate(sorted(docs.items()))])
    for name, data in docs.items():
        add(directory + "/" + name, file(directory + "/" + name, data))


class MockUpstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.routes = routes
        self.requests = []


class MockHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return None

    def do_GET(self):
        path, _, query = self.path.partition("?")
        self.server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        route = self.server.routes.get((path, _query_key(query)), {"status": 404, "payload": {"message": "Not Found"}})
        body = json.dumps(route["payload"]).encode("utf-8")
        self.send_response(route["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.requests.append({"path": self.path, "method": "POST"})
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class UpstreamOpener:
    """api.github.com -> the mock, keeping the logical URL; usable as .open (relay) or a callable (verifier)."""

    def __init__(self, upstream):
        self.upstream = upstream
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Refuse)

    def open(self, request, timeout):
        parts = urllib.parse.urlsplit(request.full_url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com", request.full_url
        local = "http://127.0.0.1:%d%s?%s" % (self.upstream.server_address[1], parts.path, parts.query)
        forwarded = urllib.request.Request(local, headers=dict(request.header_items()), method="GET")
        try:
            response = self._opener.open(forwarded, timeout=timeout)
        except urllib.error.HTTPError as error:
            error.url = request.full_url
            raise
        response.url = request.full_url
        return response

    __call__ = open


# -- harness -------------------------------------------------------------------


class Harness(unittest.TestCase):
    budget = 200
    routes_options: dict = {}

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://10.0.0.1:3128"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "HTTP_PROXY", "http_proxy"):
            os.environ.pop(name, None)
        self.root = Path(tempfile.mkdtemp(prefix="osrv-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.upstream = MockUpstream(world(**self.routes_options))
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        sleeper = mock.patch.object(relay_module.time, "sleep", lambda seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.start_relay([TARGET, ORG])

    def start_relay(self, repositories):
        argv = []
        for repository in repositories:
            argv += ["--repo", repository]
        args = relay_module.build_parser().parse_args(argv + [
            "--request-budget", str(self.budget), "--max-response-bytes", str(1 << 20),
            "--response-byte-budget", str(8 << 20), "--timeout-seconds", "10"])
        self.server = relay_module.build_server(args, opener=UpstreamOpener(self.upstream),
                                                issues_module=issues_module)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def relay_get(self, target, method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, target, body=body)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"null")
        finally:
            connection.close()

    def discover_argv(self, name, **extra):
        out = self.root / name
        argv = ["--output", str(out) + "-discovery.json", "--manifest", str(out) + "-manifest.json",
                "--request-budget", str(extra.get("budget", 150)), "--analysis", str(ANALYSIS),
                "--repo", TARGET, "--pattern", "PAT-001", "--max-candidates-per-repo", "5",
                "--max-candidates-total", "5", "--max-clues-per-pattern", "3"]
        return argv, out

    def direct(self, **extra):
        argv, out = self.discover_argv("direct", **extra)
        argv += list(extra.get("static", ()))
        code = verifier.main(["discover"] + argv, opener=UpstreamOpener(self.upstream),
                             sleeper=lambda seconds: None, clock=lambda: FIXED_TIME)
        return code, out

    def relayed(self, name="relayed", environ=None, **extra):
        argv, out = self.discover_argv(name, **extra)
        argv += list(extra.get("static", ()))
        code = adapter.main(["discover", "--relay-url", "http://127.0.0.1:%d" % self.port, "--skills-dir", str(SKILLS),
                             "--transport-record", str(out) + "-transport.json", "--timeout-seconds", "30",
                             "--max-response-bytes", str(1 << 20), "--deadline-seconds", "60"] + argv,
                            environ=environ if environ is not None else {}, clock=lambda: FIXED_TIME,
                            sleep=lambda seconds: None)
        return code, out


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


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


ALLOWED_DIFFERENCE = re.compile(r"\A\.runs\[\d+\]\.retry_events\[\d+\]\.headers\.Content-Type\Z")


def _unallowed(differences):
    return [d for d in differences if ALLOWED_DIFFERENCE.fullmatch(d) is None]


# -- tests ---------------------------------------------------------------------


class RelayRouteTests(Harness):
    def test_branch_refs_bind_to_the_observed_default_branch(self):
        status, document = self.relay_get("/v1/rest/repos/%s/commits/main" % TARGET)
        self.assertEqual((status, document["error"]), (403, "ref_not_observed"))
        status, _ = self.relay_get("/v1/rest/repos/%s/contents/CONTRIBUTING.md?ref=main" % TARGET)
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.relay_get("/v1/rest/repos/" + TARGET)[0], 200)
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/commits/main" % TARGET)[0], 200)
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/contents/CONTRIBUTING.md?ref=main" % TARGET)[0], 200)
        for target, code in (("/v1/rest/repos/%s/commits/trunk" % TARGET, 403),
                             ("/v1/rest/repos/%s/contents/CONTRIBUTING.md?ref=trunk" % TARGET, 400),
                             ("/v1/rest/repos/%s/commits/trunk" % ORG, 403)):
            with self.subTest(target):
                self.assertEqual(self.relay_get(target)[0], code)
        # The org repository's own default branch binds its own refs only.
        self.assertEqual(self.relay_get("/v1/rest/repos/" + ORG)[0], 200)
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/contents/SECURITY.md?ref=trunk" % ORG)[0], 502)  # 404 upstream
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/contents/SECURITY.md?ref=trunk" % TARGET)[0], 400)

    def test_only_the_verifier_search_forms_are_added(self):
        issue = 'repo:%s "%s" is:issue is:open' % (TARGET, CLUES[0])
        code = 'repo:%s "%s"' % (TARGET, CLUES[0])
        ok = [("/search/issues", issue), ("/search/issues", 'repo:%s "x repo:other/y" is:pr is:closed' % TARGET)]
        for endpoint, q in ok:
            with self.subTest(q):
                before = len(self.upstream.requests)
                status, document = self.relay_get("/v1/rest%s?%s" % (endpoint, urllib.parse.urlencode({"q": q})))
                # Accepted by the relay and sent upstream, scoped to TARGET (the quoted repo: is text).
                self.assertEqual(len(self.upstream.requests), before + 1, document)
                sent = urllib.parse.parse_qs(urllib.parse.urlsplit(self.upstream.requests[-1]["path"]).query)["q"]
                self.assertEqual(sent, [q])
        refused = [
            ("/search/issues", '"validation boundary repo:%s" is:issue is:open' % TARGET),
            ("/search/issues", 'repo:%s "a" "b" is:issue is:open' % TARGET),
            ("/search/issues", 'repo:%s "a" is:issue is:open OR repo:x/y' % TARGET),
            ("/search/issues", 'repo:x/y "a" is:issue is:open'),
            ("/search/issues", 'repo:%s "a\\"b" is:issue is:open' % TARGET),
            ("/search/issues", 'repo:%s "a" is:issue is:open org:x' % TARGET),
            ("/search/code", 'repo:%s validation' % TARGET),
            ("/search/code", 'repo:%s "a" is:issue is:open' % TARGET),
            ("/search/code", 'repo:x/y "a"'),
            ("/search/commits", code),
        ]
        before = len(self.upstream.requests)
        for endpoint, q in refused:
            with self.subTest(q):
                status, document = self.relay_get("/v1/rest%s?%s" % (endpoint, urllib.parse.urlencode({"q": q})))
                self.assertIn(status, (400, 403), document)
        self.assertEqual(len(self.upstream.requests), before)
        # Code search is anonymous: GitHub's 401 is passed on, never turned into an empty result.
        status, document = self.relay_get("/v1/rest/search/code?" + urllib.parse.urlencode({"q": code}))
        self.assertEqual((status, document["error"], document["upstream_status"]), (502, "upstream_failed", 401))

    def test_new_repository_facts_are_exact_reads_only(self):
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/community/profile" % TARGET)[0], 200)
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/private-vulnerability-reporting" % TARGET)[0], 200)
        for target, method in (("/v1/rest/repos/%s/community/profile?page=2" % TARGET, "GET"),
                               ("/v1/rest/repos/other/repo/community/profile", "GET"),
                               ("/v1/rest/repos/%s/private-vulnerability-reporting" % TARGET, "PUT"),
                               ("/v1/rest/repos/%s/community" % TARGET, "GET"),
                               ("/v1/rest/repos/%s/git/refs/heads/main" % TARGET, "GET")):
            with self.subTest(target=target, method=method):
                self.assertIn(self.relay_get(target, method)[0], (400, 403, 404, 405))

    def test_out_of_scope_payloads_fail(self):
        self.upstream.routes[("/repos/%s/community/profile" % TARGET, ())] = {"status": 200, "payload": ["x"]}
        self.upstream.routes[("/repos/%s/private-vulnerability-reporting" % TARGET, ())] = {"status": 200,
                                                                                          "payload": {"enabled": "yes"}}
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/community/profile" % TARGET)[1]["error"], "upstream_out_of_scope")
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/private-vulnerability-reporting" % TARGET)[1]["error"],
                         "upstream_out_of_scope")
        self.relay_get("/v1/rest/repos/" + TARGET)
        self.upstream.routes[("/repos/%s/commits/main" % TARGET, ())] = {
            "status": 200, "payload": {"sha": HEAD, "url": API + "/repos/other/repo/commits/" + HEAD}}
        self.assertEqual(self.relay_get("/v1/rest/repos/%s/commits/main" % TARGET)[1]["error"], "upstream_out_of_scope")


class IdentityTests(Harness):
    def test_relayed_discover_matches_direct_except_transport_metadata(self):
        direct_code, direct_out = self.direct()
        self.upstream.requests.clear()
        relayed_code, relayed_out = self.relayed()
        self.assertEqual((direct_code, relayed_code), (3, 3))  # code search 401: partial, never complete
        direct = _load_json(str(direct_out) + "-discovery.json")
        relayed = _load_json(str(relayed_out) + "-discovery.json")
        # The discovery document is identical. The manifest's retry events differ only in the
        # Content-Type response header, which the relay does not forward (metadata, not evidence).
        self.assertEqual(_differences(direct, relayed), [])
        self.assertEqual(_unallowed(_differences(_load_json(str(direct_out) + "-manifest.json"),
                                                 _load_json(str(relayed_out) + "-manifest.json"))), [])
        combos = relayed.get("records") or []
        self.assertTrue(combos)
        self.assertFalse(combos[0]["code_search"]["available"])
        # Anonymous, one per_page, never a write.
        for request in self.upstream.requests:
            self.assertNotIn("authorization", request["headers"])
            self.assertNotEqual(request.get("method"), "POST")
        transport = _load_json(str(relayed_out) + "-transport.json")
        self.assertIn("not TLS evidence", transport["note"])
        self.assertEqual(transport["verifier"]["sha256"], verifier_source["sha256"])
        self.assertTrue(all(entry["outcome"] in ("relayed", "upstream-error") for entry in transport["requests"]))

    def relayed_with_sleep(self, name, sleep, deadline="60"):
        argv, out = self.discover_argv(name)
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = adapter.main(["discover", "--relay-url", "http://127.0.0.1:%d" % self.port,
                                 "--skills-dir", str(SKILLS), "--transport-record", str(out) + "-transport.json",
                                 "--timeout-seconds", "30", "--max-response-bytes", str(1 << 20),
                                 "--deadline-seconds", deadline] + argv,
                                environ={}, clock=lambda: FIXED_TIME, sleep=sleep)
        return code, stderr.getvalue()

    def test_search_pacing_waits_go_through_the_deadline_bounded_sleep(self):
        """#279: the verifier's search pacing uses the adapter's sleeper, not its own clock."""
        sleeps = []
        code, _ = self.relayed_with_sleep("paced", sleeps.append)
        self.assertEqual(code, 3)
        waits = {round(seconds, 6) for seconds in sleeps}
        self.assertIn(verifier.SEARCH_ISSUES_INTERVAL_SECONDS, waits)
        self.assertIn(verifier.SEARCH_CODE_INTERVAL_SECONDS, waits)

    def test_a_pacing_wait_past_the_deadline_stops_the_step(self):
        code, stderr = self.relayed_with_sleep("late", lambda seconds: None, deadline="2")
        self.assertEqual(code, 2)
        self.assertIn("a wait of 2.1s would pass the step deadline", stderr)

    def test_token_variables_stop_the_step_before_any_request(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            with self.subTest(name):
                code, out = self.relayed(name="tok-" + name, environ={name: "gho_sentinel"})
                self.assertEqual(code, 2)
                self.assertFalse(Path(str(out) + "-discovery.json").exists())
        self.assertEqual(self.upstream.requests, [])

    def test_an_authorization_header_is_refused_by_the_adapter(self):
        opener = adapter.build_opener(verifier, adapter.load_relay_collect(), "http://127.0.0.1:%d" % self.port,
                                      timeout_seconds=5, max_response_bytes=1 << 20, proxy=None)
        request = urllib.request.Request(API + "/repos/" + TARGET, method="GET", headers={
            "Accept": "application/vnd.github+json", "User-Agent": "x",
            "X-GitHub-Api-Version": verifier.DEFAULT_API_VERSION, "Authorization": "Bearer x"})
        with self.assertRaises(verifier.HttpRequestRefused):
            opener(request, 5)
        with self.assertRaises(verifier.HttpRequestRefused):
            opener(urllib.request.Request(API + "/repos/" + TARGET, data=b"{}", method="POST"), 5)
        self.assertEqual(self.upstream.requests, [])


class ForkParentTests(Harness):
    routes_options = {"fork_parent": "upstream/demo"}

    def test_a_request_outside_the_allowlist_stops_the_run_instead_of_being_recorded_absent(self):
        code, out = self.relayed()
        self.assertEqual(code, 2)
        transport = _load_json(str(out) + "-transport.json")
        self.assertEqual(transport["requests"][-1]["relay_error"], "repository_not_allowed")


class PartialAnswerTests(Harness):
    routes_options = {"pvr_status": 403, "org_exists": False}

    def test_a_403_and_a_missing_org_repository_stay_what_they_are(self):
        direct_code, direct_out = self.direct()
        relayed_code, relayed_out = self.relayed()
        self.assertEqual((direct_code, relayed_code), (3, 3))
        self.assertEqual(_differences(_load_json(str(direct_out) + "-discovery.json"),
                                      _load_json(str(relayed_out) + "-discovery.json")), [])
        self.assertEqual(_unallowed(_differences(_load_json(str(direct_out) + "-manifest.json"),
                                                 _load_json(str(relayed_out) + "-manifest.json"))), [])


class BudgetTests(Harness):
    budget = 5

    def test_the_relay_budget_stop_is_the_verifiers_budget_stop(self):
        code, out = self.relayed()
        self.assertEqual(code, 3)
        transport = _load_json(str(out) + "-transport.json")
        self.assertIn("relay-budget-exhausted", [entry.get("outcome") for entry in transport["requests"]])
        self.assertLessEqual(len(self.upstream.requests), 5)


STATIC = ("--static-reads", "rest", "--static-dir", "docs/rules")


class StaticReadsTests(Harness):
    """--static-reads rest through the adapter and the unchanged relay (#256)."""

    def setUp(self):
        super().setUp()
        add_static_routes(self.upstream.routes)

    def test_relayed_static_reads_match_direct_and_read_only_the_fixed_paths(self):
        direct_code, direct_out = self.direct(static=STATIC)
        self.upstream.requests.clear()
        relayed_code, relayed_out = self.relayed(static=STATIC)
        self.assertEqual((direct_code, relayed_code), (3, 3))  # the anonymous code search 401 stays partial
        direct, relayed = (_load_json(str(out) + "-discovery.json") for out in (direct_out, relayed_out))
        self.assertEqual(_differences(direct, relayed), [])
        static = relayed["records"][0]["static_search"]
        self.assertEqual((static["mode"], static["ref"], static["complete"]), ("rest-contents", HEAD, True))
        self.assertEqual(sorted((hit["rule"], hit["path"], hit["line"]) for hit in static["hits"]), [
            ("options-heading-missing", "docs/rules/one.md", 0),
            ("readme-install-without-eslint", "README.md", 6),
            ("relative-link-missing-target", "README.md", 10)])
        self.assertEqual([item["url"] for item in static["external_links"]], ["https://example.com/x"])
        # The relay adds its fixed per_page upstream; the path and the ref are the verifier's.
        contents = sorted(r["path"].partition("?")[0] for r in self.upstream.requests
                          if "/contents/" in r["path"] and "ref=" + HEAD in r["path"])
        self.assertEqual(contents, ["/repos/%s/contents/%s" % (TARGET, path) for path in (
            "README.md", "docs/rules", "docs/rules/one.md", "docs/rules/two.md")])
        self.assertFalse([r for r in self.upstream.requests if "example.com" in r["path"]])
        transport = _load_json(str(relayed_out) + "-transport.json")
        self.assertTrue(all(entry["outcome"] in ("relayed", "upstream-error") for entry in transport["requests"]))

    def test_the_repository_static_section_repeats_the_records_static_reads(self):
        code, out = self.relayed(static=STATIC)
        self.assertEqual(code, 3)
        discovery = _load_json(str(out) + "-discovery.json")
        self.assertEqual([(item["repository"], item["head_sha"]) for item in discovery["static_reads"]],
                         [(TARGET, HEAD)])
        self.assertEqual(discovery["static_reads"][0]["static_search"], discovery["records"][0]["static_search"])

    def test_the_default_mode_has_no_static_section(self):
        code, out = self.relayed()
        self.assertEqual(code, 3)
        discovery = _load_json(str(out) + "-discovery.json")
        self.assertNotIn("static_reads", discovery)
        self.assertFalse({"static_reads", "static_dirs"} & set(discovery["inputs"]))

    def test_static_reads_survive_a_budget_that_ends_in_the_searches(self):
        """#266 S3: the static reads finish, the searches run out; the reads stay in the output."""
        docs = {"r%02d.md" % index: b"# r\n\n## Rule Details\n" for index in range(verifier.STATIC_MAX_FILES)}
        add_static_routes(self.upstream.routes, docs=docs)
        floor = verifier.static_request_reserve(1)
        direct_code, direct_out = self.direct(static=STATIC, budget=floor)
        self.upstream.requests.clear()
        relayed_code, relayed_out = self.relayed(static=STATIC, budget=floor)
        self.assertEqual((direct_code, relayed_code), (3, 3))
        direct, relayed = (_load_json(str(out) + "-discovery.json") for out in (direct_out, relayed_out))
        self.assertEqual(_differences(direct, relayed), [])
        # The budget ran out in the searches, after every static read.
        self.assertTrue([r for r in self.upstream.requests if r["path"].startswith("/search/")])
        self.assertEqual((relayed["records"], relayed["failed_scopes"]),
                         ([], [{"repository": TARGET, "pattern_id": "PAT-001", "reason": "budget-exhausted"}]))
        self.assertEqual(relayed["status"], "partial")
        [item] = relayed["static_reads"]
        self.assertEqual((item["repository"], item["head_sha"]), (TARGET, HEAD))
        static = item["static_search"]
        self.assertEqual((static["mode"], static["ref"], static["complete"], len(static["files"])),
                         ("rest-contents", HEAD, True, 1 + verifier.STATIC_MAX_FILES))
        self.assertEqual(sorted(hit["path"] for hit in static["hits"] if hit["rule"] == "options-heading-missing"),
                         ["docs/rules/" + name for name in sorted(docs)])
        manifest = _load_json(str(relayed_out) + "-manifest.json")
        self.assertEqual(manifest["runs"][-1]["budget"]["requests_consumed"], floor)

    def test_a_repository_without_static_reads_still_has_its_entry(self):
        def entry(discovery):
            [item] = discovery["static_reads"]
            static = item["static_search"]
            return (item["repository"], item["head_sha"], static["mode"], static["ref"], static["available"],
                    static["files"], static["hits"], static["error"])

        # Below the floor (only reachable with the floor lowered) the budget ends in the repository facts.
        with mock.patch.object(verifier, "static_request_reserve", lambda directories: 1):
            code, out = self.direct(static=STATIC, budget=3)
        self.assertEqual(code, 3)
        self.assertEqual(entry(_load_json(str(out) + "-discovery.json")),
                         (TARGET, HEAD, "rest-contents", None, False, [], [], "budget-exhausted"))
        del self.upstream.routes[("/repos/" + TARGET, ())]
        code, out = self.direct(static=STATIC)
        self.assertEqual(code, 4)
        self.assertEqual(entry(_load_json(str(out) + "-discovery.json")),
                         (TARGET, None, "rest-contents", None, False, [], [], "repository-unavailable"))

    def test_the_verifier_argv_is_unchanged_without_rest(self):
        relay_collect = adapter.load_relay_collect()
        common = ["discover", "--relay-url", "http://127.0.0.1:1", "--skills-dir", str(SKILLS),
                  "--transport-record", "t", "--timeout-seconds", "30", "--max-response-bytes", "1024",
                  "--deadline-seconds", "60", "--output", "o", "--manifest", "m", "--request-budget", "60",
                  "--analysis", "a", "--repo", TARGET, "--pattern", "PAT-001"]
        legacy = ["discover", "--output", "o", "--manifest", "m", "--request-budget", "60", "--analysis", "a",
                  "--repo", TARGET, "--max-candidates-per-repo", "5", "--max-candidates-total", "5",
                  "--max-clues-per-pattern", "5", "--pattern", "PAT-001"]
        parse = adapter.build_parser(relay_collect).parse_args
        self.assertEqual(adapter.verifier_argv(parse(common)), legacy)
        self.assertEqual(adapter.verifier_argv(parse(common + ["--static-reads", "none"])), legacy)
        self.assertEqual(adapter.verifier_argv(parse(common + ["--static-reads", "rest"])),
                         legacy + ["--static-reads", "rest"])
        self.assertEqual(adapter.verifier_argv(parse(common + list(STATIC) + ["--static-dir", "docs"])),
                         legacy + list(STATIC) + ["--static-dir", "docs"])

    def test_out_of_rule_static_options_stop_before_the_network(self):
        cases = {
            "dir without rest": ("--static-dir", "docs/rules"),
            "dot-dot": ("--static-reads", "rest", "--static-dir", "docs/../x"),
            "absolute": ("--static-reads", "rest", "--static-dir", "/docs"),
            "too deep": ("--static-reads", "rest", "--static-dir", "a/b/c/d"),
            "too many": ("--static-reads", "rest", "--static-dir", "a", "--static-dir", "b", "--static-dir", "c",
                         "--static-dir", "d"),
            "duplicate": ("--static-reads", "rest", "--static-dir", "a", "--static-dir", "a"),
            "encoded": ("--static-reads", "rest", "--static-dir", "docs%2Frules"),
        }
        for name, static in cases.items():
            with self.subTest(name):
                code, out = self.relayed(name="bad-" + name.replace(" ", "-"), static=static)
                self.assertEqual(code, 2)
                self.assertFalse(Path(str(out) + "-discovery.json").exists())
                self.assertFalse(Path(str(out) + "-transport.json").exists())
        with self.assertRaises(SystemExit) as raised:
            self.relayed(name="bad-mode", static=("--static-reads", "clone"))
        self.assertEqual(raised.exception.code, 2)
        # The verifier's own budget floor (repository facts, README, listings, 30 files) also stops early.
        code, out = self.relayed(name="small-budget", static=STATIC, budget=57)
        self.assertEqual(code, 2)
        self.assertEqual(self.upstream.requests, [])

    def test_the_content_path_rule_is_the_relays(self):
        self.assertEqual(adapter.CONTENT_SEGMENT, relay_module._CONTENT_SEGMENT)
        self.assertEqual(verifier.CONTENT_SEGMENT, relay_module._CONTENT_SEGMENT)
        self.assertEqual((adapter.STATIC_READS_MODES, adapter.STATIC_MAX_DIRS, adapter.STATIC_MAX_DEPTH),
                         (verifier.STATIC_READS_MODES, verifier.STATIC_MAX_DIRS, verifier.STATIC_MAX_DEPTH))
        # Every path the static reads can request is a contents path the relay accepts.
        pattern = re.compile(r"\A" + relay_module._CONTENT_PATH + r"\Z")
        for path in ("README.md", "docs/rules", "docs/rules/one.md", ".github/x/y.md", "a/b/c/" + "n" * 200 + ".md"):
            self.assertIsNotNone(pattern.match(path), path)


if __name__ == "__main__":
    unittest.main()
