"""Check the read-only OSS research relay against loopback fixtures only.

Every test runs with non-loopback connections refused. The relay's upstream is
a local fixture HTTP server reached through a test-only opener; the product CLI
has no way to point the relay anywhere but api.github.com.
"""

import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
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
SCRIPT = ROOT / "dot_local/bin/executable_oss-research-relay"
SKILLS = ROOT / "dot_codex/skills"
FIXTURES = ROOT / "tests/fixtures/oss_research_relay"
SHA = "0123456789abcdef0123456789abcdef01234567"
API = "https://api.github.com"

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


def _load_relay():
    loader = importlib.machinery.SourceFileLoader("oss_research_relay_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


relay_module = _load_relay()
issues_module, source_manifest = relay_module.load_issues_module(SKILLS)


# The harness stubs time.sleep for the relay's retry waits; fixture delays must not use it.
_pause = threading.Event().wait


class FixtureUpstream(http.server.ThreadingHTTPServer):
    """A loopback stand-in for api.github.com that records every request."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.routes = {}
        self.requests = []


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return None

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "method": self.command, "path": self.path,
            "headers": {name.lower(): value for name, value in self.headers.items()}, "body": body,
        })
        route = self.server.routes.get(self.path.partition("?")[0])
        if route is None:
            route = {"status": 404, "body": b'{"message":"Not Found"}'}
        if route.get("delay"):
            _pause(route["delay"])
        payload = route["body"]
        self.send_response(route["status"])
        for name, value in route.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        if route.get("drip"):
            self.send_header("Content-Length", str(len(payload) * 50))
            self.end_headers()
            for _ in range(50):
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except OSError:
                    return
                _pause(route["drip"])
            return
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class FixtureOpener:
    """Test-only opener: sends the relay's api.github.com request to the fixture."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.requests = []
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Refuse)

    def open(self, request, timeout):
        self.requests.append(request)
        parts = urllib.parse.urlsplit(request.full_url)
        assert parts.scheme == "https" and parts.netloc == "api.github.com", request.full_url
        local = "http://127.0.0.1:{0}{1}".format(
            self.upstream.server_address[1], parts.path + ("?" + parts.query if parts.query else ""),
        )
        forwarded = urllib.request.Request(
            local, data=request.data, headers=dict(request.header_items()), method=request.get_method(),
        )
        try:
            response = self._opener.open(forwarded, timeout=timeout)
        except urllib.error.HTTPError as error:
            error.url = request.full_url
            raise
        response.url = request.full_url
        return response


def _json(document):
    return json.dumps(document).encode("utf-8")


class RelayHarness(unittest.TestCase):
    budget = 20
    max_response_bytes = 64 * 1024
    response_byte_budget = 1024 * 1024
    timeout_seconds = 5.0

    def setUp(self):
        environment = mock.patch.dict(os.environ, {
            "GH_TOKEN": "gho_parentsecret", "GITHUB_TOKEN": "ghp_parentsecret",
            "HTTPS_PROXY": "http://10.0.0.1:3128",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.upstream = FixtureUpstream()
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.opener = FixtureOpener(self.upstream)
        args = relay_module.build_parser().parse_args([
            "--repo", "octo/demo", "--repo", "octo/.github",
            "--request-budget", str(self.budget),
            "--max-response-bytes", str(self.max_response_bytes),
            "--response-byte-budget", str(self.response_byte_budget),
            "--timeout-seconds", str(self.timeout_seconds),
        ])
        self.server = relay_module.build_server(args, opener=self.opener, issues_module=issues_module)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        # Retries after a 5xx or a transport failure would slow the tests down.
        sleeper = mock.patch.object(relay_module.time, "sleep", lambda seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def route(self, path, payload=None, status=200, **options):
        body = options.pop("body", None)
        self.upstream.routes[path] = dict(
            status=status, body=body if body is not None else _json(payload), **options,
        )

    def request(self, method, target, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    def raw(self, text):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(text.encode("latin-1"))
            data = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body) if body else None

    def graphql(self, document, headers=None):
        body = document if isinstance(document, bytes) else _json(document)
        return self.request("POST", "/v1/graphql", body=body,
                            headers=headers or {"Content-Type": "application/json"})

    def assert_refused(self, status, document, expected_status, code=None):
        self.assertEqual(status, expected_status, document)
        self.assertFalse(document["ok"])
        if code is not None:
            self.assertEqual(document["error"], code)
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(self.upstream.requests, [])


class AllowedReadTests(RelayHarness):
    def test_health_needs_no_upstream(self):
        status, document = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(document["ok"])
        self.assertEqual(document["budget"]["consumed"], 0)
        self.assertEqual(self.upstream.requests, [])

    def test_rest_list_read_is_anonymous_and_canonical(self):
        self.route("/repos/octo/demo/issues/7/comments", [{"id": 1}], headers={"ETag": '"abc"'})
        status, document = self.request("GET", "/v1/rest/repos/Octo/Demo/issues/7/comments?page=2")
        self.assertEqual(status, 200, document)
        self.assertEqual(document["payload"], [{"id": 1}])
        self.assertEqual(document["headers"]["etag"], '"abc"')
        self.assertEqual(document["budget"]["consumed"], 1)
        [seen] = self.upstream.requests
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/repos/octo/demo/issues/7/comments?per_page=100&page=2")
        self.assertNotIn("authorization", seen["headers"])
        self.assertNotIn("cookie", seen["headers"])
        self.assertNotIn("parentsecret", json.dumps(seen["headers"]))

    def test_versions_is_one_exact_anonymous_read(self):
        # The collector's API-version preflight: exact path, no query, same budget and scope check.
        self.route("/versions", ["2022-11-28", "2026-03-10"])
        status, document = self.request("GET", "/v1/rest/versions")
        self.assertEqual(status, 200, document)
        self.assertEqual(document["payload"], ["2022-11-28", "2026-03-10"])
        self.assertEqual(document["budget"]["consumed"], 1)
        [seen] = self.upstream.requests
        self.assertEqual((seen["method"], seen["path"]), ("GET", "/versions?per_page=100"))
        self.assertNotIn("authorization", seen["headers"])
        for target in ("/v1/rest/versions?page=1", "/v1/rest/versions?per_page=100"):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assertEqual((status, document["error"]), (400, "query_not_allowed"), document)
        status, document = self.request("POST", "/v1/rest/versions", body=b"{}")
        self.assertEqual(status, 405, document)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_versions_scope_check_refuses_a_non_version_payload(self):
        self.route("/versions", {"message": "not a list"})
        status, document = self.request("GET", "/v1/rest/versions")
        self.assertEqual((status, document["error"]), (502, "upstream_out_of_scope"), document)

    def test_rest_repository_item_commit_contents_and_search(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"})
        self.route("/repos/octo/demo/pulls/3", {"url": API + "/repos/octo/demo/pulls/3"})
        self.route("/repos/octo/demo/commits/" + SHA, {"sha": SHA, "url": API + "/repos/octo/demo/commits/" + SHA})
        self.route("/repos/octo/.github/contents/SECURITY.md", {
            "path": "SECURITY.md", "url": API + "/repos/octo/.github/contents/SECURITY.md?ref=" + SHA,
        })
        self.route("/search/issues", {"total_count": 1, "items": [
            {"number": 3, "repository_url": API + "/repos/octo/demo"},
        ]})
        for target in (
            "/v1/rest/repos/octo/demo",
            "/v1/rest/repos/octo/demo/pulls/3",
            "/v1/rest/repos/octo/demo/commits/" + SHA,
            "/v1/rest/repos/octo/.github/contents/SECURITY.md?ref=" + SHA,
            "/v1/rest/search/issues?" + urllib.parse.urlencode({"q": 'repo:octo/demo is:issue label:good-first',
                                                                "page": 1}),
        ):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assertEqual(status, 200, document)
                self.assertTrue(document["ok"])
        self.assertEqual([request["method"] for request in self.upstream.requests], ["GET"] * 5)
        self.assertIn("ref=" + SHA, self.upstream.requests[3]["path"])

    def test_graphql_sends_only_the_fixed_issue_evidence_query(self):
        self.route("/graphql", {"data": {"repository": {"issue": None}}})
        status, document = self.graphql({"operation": "IssueEvidence",
                                         "variables": {"owner": "OCTO", "name": "demo", "number": 5}})
        self.assertEqual(status, 200, document)
        [seen] = self.upstream.requests
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/graphql")
        self.assertNotIn("authorization", seen["headers"])
        sent = json.loads(seen["body"])
        self.assertEqual(sent["query"], issues_module.ISSUE_EVIDENCE_QUERY)
        self.assertEqual(sent["variables"], {"owner": "octo", "name": "demo", "number": 5})

    def test_clients_have_no_credential_provider(self):
        relay = self.server.relay
        self.assertIsNone(relay.rest._token_provider())
        self.assertIsNone(relay.graphql._token_provider())
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("gh must not run")):
            self.route("/repos/octo/demo", {"full_name": "octo/demo"})
            status, _document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 200)


class WriteAndMethodRefusalTests(RelayHarness):
    def test_write_methods_are_refused_without_upstream(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"):
            for target in ("/v1/rest/repos/octo/demo/issues", "/v1/rest/repos/octo/demo/issues/1/comments",
                           "/v1/rest/repos/octo/demo/pulls/1/reviews"):
                with self.subTest(method=method, target=target):
                    connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                    connection.request(method, target, body=b"{}" if method != "HEAD" else None,
                                       headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    self.assertEqual(response.status, 405)
        self.assertEqual(self.upstream.requests, [])

    def test_graphql_route_refuses_get_and_query_string(self):
        status, document = self.request("GET", "/v1/graphql")
        self.assert_refused(status, document, 405)
        status, document = self.request("POST", "/v1/graphql?x=1", body=b"{}",
                                        headers={"Content-Type": "application/json"})
        self.assert_refused(status, document, 405)


class PathAndSsrfRefusalTests(RelayHarness):
    def test_endpoints_outside_the_allowlist_are_refused(self):
        for target in (
            "/v1/rest/user", "/v1/rest/versions/extra", "/v1/rest/meta", "/v1/rest/repos/octo/demo/git/refs",
            "/v1/rest/repos/octo/demo/hooks", "/v1/rest/repos/octo/demo/actions/secrets",
            "/v1/rest/repos/octo/demo/collaborators",
            "/v1/rest/repos/octo/demo/git/upload-pack", "/v1/rest/repos/octo/demo/tarball/main",
            "/v1/rest/graphql",
        ):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assert_refused(status, document, 403, "endpoint_not_allowed")
        # Added for the candidate verifier, still refused here: code search takes only its exact
        # quoted form, and a branch commit only the default branch this relay observed.
        for target, code in (("/v1/rest/search/code?q=repo:octo/demo", "search_grammar_not_allowed"),
                             ("/v1/rest/repos/octo/demo/commits/main", "ref_not_observed")):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assert_refused(status, document, 403, code)

    def test_repositories_outside_the_allowlist_are_refused(self):
        for target in ("/v1/rest/repos/other/demo", "/v1/rest/repos/octo/demo2/issues/1",
                       "/v1/rest/repos/other/.github/contents/SECURITY.md?ref=" + SHA):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assert_refused(status, document, 403, "repository_not_allowed")

    def test_encoded_dot_and_escaped_paths_are_refused(self):
        for target in (
            "/v1/rest/repos/octo/demo/contents/..%2F..%2Fuser?ref=" + SHA,
            "/v1/rest/repos/octo/demo/contents/%2e%2e/x?ref=" + SHA,
            "/v1/rest/repos/octo%2Fdemo/issues/1",
            "/v1/rest/repos/octo/demo/contents/a/../../../user?ref=" + SHA,
            "/v1/rest/repos/octo/demo/contents/./x?ref=" + SHA,
            "/v1/rest/repos/octo/demo//issues/1",
            "/v1/rest//api.github.com/user",
            "/v1/rest/repos/octo/demo/issues/1;x",
            "/v1/rest/repos/octo/demo/contents/a\\b?ref=" + SHA,
        ):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assert_refused(status, document, 400, "bad_path")

    def test_upstream_url_forms_cannot_be_injected(self):
        for target in ("/v1/rest/https://evil.example/x", "/v1/rest/repos/octo/demo?url=http://169.254.169.254/",
                       "/v1/rest/repos/octo/demo/issues/1?page=1&host=evil.example"):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assertIn(status, (400, 403), document)
        self.assertEqual(self.upstream.requests, [])

    def test_absolute_form_connect_and_foreign_host_are_refused(self):
        status, document = self.raw("GET http://api.github.com/repos/octo/demo HTTP/1.1\r\n"
                                    "Host: 127.0.0.1:{0}\r\n\r\n".format(self.port))
        self.assert_refused(status, document, 400, "bad_target")
        status, document = self.raw("CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
        self.assert_refused(status, document, 405)
        status, document = self.raw("GET /v1/rest/repos/octo/demo HTTP/1.1\r\nHost: evil.example\r\n\r\n")
        self.assert_refused(status, document, 400, "bad_host")
        status, document = self.raw("GET /v1/rest/repos/octo/demo HTTP/1.1\r\n"
                                    "Host: 127.0.0.1:{0}\r\nHost: evil.example\r\n\r\n".format(self.port))
        self.assert_refused(status, document, 400, "bad_host")

    def test_credential_cookie_and_conditional_headers_are_refused(self):
        for header in ("Authorization", "Cookie", "Proxy-Authorization", "If-None-Match", "Expect"):
            with self.subTest(header=header):
                status, document = self.request("GET", "/v1/rest/repos/octo/demo", headers={header: "x"})
                self.assert_refused(status, document, 400, "header_not_allowed")

    def test_query_parameters_outside_the_allowlist_are_refused(self):
        for target, code in (
            ("/v1/rest/repos/octo/demo/issues/1/comments?per_page=5", "query_not_allowed"),
            ("/v1/rest/repos/octo/demo/issues/1/comments?since=2020-01-01", "query_not_allowed"),
            ("/v1/rest/repos/octo/demo/issues/1/comments?page=1&page=2", "bad_query"),
            ("/v1/rest/repos/octo/demo/issues/1/comments?page=0", "bad_query"),
            ("/v1/rest/repos/octo/demo/issues/1/comments?page=1001", "bad_query"),
            ("/v1/rest/repos/octo/demo?page=1", "query_not_allowed"),
            ("/v1/rest/repos/octo/demo/commits/" + SHA + "?page=1", "query_not_allowed"),
            ("/v1/rest/repos/octo/demo/contents/README.md", "bad_query"),
            ("/v1/rest/repos/octo/demo/contents/README.md?ref=main", "bad_query"),
            ("/v1/rest/repos/octo/demo/contents/README.md?ref=" + SHA.upper(), "bad_query"),
        ):
            with self.subTest(target=target):
                status, document = self.request("GET", target)
                self.assert_refused(status, document, 400, code)

    def test_search_queries_that_can_leave_the_repository_are_refused(self):
        for query in (
            "is:issue", "repo:octo/demo repo:other/x", "repo:octo/demo OR repo:other/x",
            "repo:octo/demo or is:pr", "repo:octo/demo NOT is:pr", "(repo:octo/demo) is:pr",
            "repo:octo/demo -label:bug", "repo:octo/demo org:other", "repo:octo/demo user:other",
            "repo:other/x", '"repo:octo/demo" repo:other/x', 'repo:octo/demo label:"x',
            "repo:octo/demo owner:other", "repo:octo/demo\\ repo:other/x", "REPO : other/x repo:octo/demo",
            "repo:octo/demo\nrepo:other/x",
            # A qualifier inside a quoted phrase is text to GitHub and scopes nothing.
            '"foo repo:octo/demo bar"', '"repo:octo/demo"', 'repo:octo/demo label:"a b"',
            "repo:octo/demo label:'a'", 'is:issue "repo:octo/demo',
        ):
            with self.subTest(query=query):
                target = "/v1/rest/search/issues?" + urllib.parse.urlencode({"q": query})
                status, document = self.request("GET", target)
                self.assertIn(status, (400, 403), document)
                self.assertFalse(document["ok"])
        self.assertEqual(self.upstream.requests, [])


    def test_quoted_scope_is_refused_by_the_checker_itself(self):
        relay = self.server.relay
        for query in ('"foo repo:octo/demo bar"', '"repo:octo/demo"', "'repo:octo/demo'"):
            with self.subTest(query=query):
                with self.assertRaises(relay_module.RelayError) as caught:
                    relay._check_search_query(query)
                self.assertEqual(caught.exception.status, 403)
        self.assertEqual(relay._check_search_query("repo:Octo/Demo is:issue"), "octo/demo")


class GraphqlRefusalTests(RelayHarness):
    def test_raw_documents_mutations_and_bad_variables_are_refused(self):
        valid = {"owner": "octo", "name": "demo", "number": 1}
        for document, expected in (
            ({"query": "mutation { addStar(input: {}) { clientMutationId } }"}, 400),
            ({"query": issues_module.ISSUE_EVIDENCE_QUERY, "variables": valid}, 400),
            ({"operation": "IssueEvidence", "variables": valid, "query": "query { viewer { login } }"}, 400),
            ({"operation": "AddStar", "variables": valid}, 403),
            ({"operation": "mutation IssueEvidence", "variables": valid}, 403),
            ({"operation": "IssueEvidence", "variables": dict(valid, extra=1)}, 400),
            ({"operation": "IssueEvidence", "variables": dict(valid, number=True)}, 400),
            ({"operation": "IssueEvidence", "variables": dict(valid, number="1")}, 400),
            ({"operation": "IssueEvidence", "variables": dict(valid, number=0)}, 400),
            ({"operation": "IssueEvidence", "variables": dict(valid, owner="other")}, 403),
            ({"operation": "IssueEvidence", "variables": dict(valid, name="demo/../x")}, 403),
            ({"operation": "IssueEvidence", "variables": [valid]}, 400),
        ):
            with self.subTest(document=document):
                status, reply = self.graphql(document)
                self.assert_refused(status, reply, expected)

    def test_non_string_operations_are_bad_requests(self):
        valid = {"owner": "octo", "name": "demo", "number": 1}
        for operation in ([], {}, None, True, 1, ["IssueEvidence"]):
            with self.subTest(operation=operation):
                with self.assertRaises(relay_module.RelayError) as caught:
                    self.server.relay.check_graphql(_json({"operation": operation, "variables": valid}))
                self.assertEqual(caught.exception.status, 400)
                status, reply = self.graphql({"operation": operation, "variables": valid})
                self.assert_refused(status, reply, 400, "bad_graphql")
        for variables in ({"owner": [], "name": "demo", "number": 1}, {"owner": "octo", "name": None, "number": 1},
                          {"owner": "octo", "name": "demo", "number": 1.0}, None, "x"):
            with self.subTest(variables=variables):
                status, reply = self.graphql({"operation": "IssueEvidence", "variables": variables})
                self.assert_refused(status, reply, 400, "bad_variables")

    def test_duplicate_keys_and_non_standard_constants_are_ambiguous(self):
        for body in (
            b'{"operation":"IssueEvidence","operation":"AddStar","variables":{"owner":"octo","name":"demo","number":1}}',
            b'{"operation":"IssueEvidence","variables":{"owner":"octo","owner":"other","name":"demo","number":1}}',
            b'{"operation":"IssueEvidence","variables":{"owner":"octo","name":"demo","number":NaN}}',
            b'{"operation":"IssueEvidence","variables":{"owner":"octo","name":"demo","number":Infinity}}',
        ):
            with self.subTest(body=body):
                status, reply = self.graphql(body)
                self.assert_refused(status, reply, 400, "bad_json")

    def test_malformed_wrong_type_and_oversize_bodies_are_refused(self):
        status, reply = self.graphql(b"{not json")
        self.assert_refused(status, reply, 400, "bad_json")
        status, reply = self.graphql(b"{}", headers={"Content-Type": "text/plain"})
        self.assert_refused(status, reply, 415)
        status, reply = self.graphql(b"x" * (relay_module.MAX_REQUEST_BYTES + 1))
        self.assert_refused(status, reply, 413)
        status, reply = self.raw("POST /v1/graphql HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n"
                                 "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                                 "2\r\n{{}}\r\n0\r\n\r\n".format(self.port))
        self.assert_refused(status, reply, 400, "header_not_allowed")


class UpstreamFailureTests(RelayHarness):
    budget = 3
    max_response_bytes = 1024
    response_byte_budget = 4096
    timeout_seconds = 0.6

    def test_redirect_is_not_followed(self):
        self.route("/repos/octo/demo", {"message": "Moved"}, status=301,
                   headers={"Location": "http://169.254.169.254/latest"})
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 502, document)
        self.assertEqual(document["error"], "upstream_failed")
        self.assertEqual(document["upstream_status"], 301)
        self.assertEqual([request["path"] for request in self.upstream.requests], ["/repos/octo/demo?per_page=100"])

    def test_oversize_response_is_refused(self):
        self.route("/repos/octo/demo", body=b'{"full_name":"octo/demo","x":"' + b"a" * 2048 + b'"}')
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 502, document)
        self.assertEqual(document["error"], "upstream_too_large")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_malformed_and_out_of_scope_results_are_failures(self):
        for path, body, code in (
            ("/repos/octo/demo", b"{not json", "upstream_failed"),
            ("/repos/octo/demo/issues/1", _json({"url": API + "/repos/other/demo/issues/1"}), "upstream_out_of_scope"),
            ("/repos/octo/demo/issues/1/comments", _json({"not": "a list"}), "upstream_out_of_scope"),
        ):
            with self.subTest(path=path):
                self.upstream.requests.clear()
                self.route(path, body=body)
                status, document = self.request("GET", "/v1/rest" + path)
                self.assertEqual(status, 502, document)
                self.assertEqual(document["error"], code)
                self.assertNotIn("payload", document)

    def test_search_item_from_another_repository_is_a_failure(self):
        self.route("/search/issues", {"items": [{"repository_url": API + "/repos/octo/demo"},
                                                {"repository_url": API + "/repos/other/x"}]})
        target = "/v1/rest/search/issues?" + urllib.parse.urlencode({"q": "repo:octo/demo is:issue"})
        status, document = self.request("GET", target)
        self.assertEqual(status, 502, document)
        self.assertEqual(document["error"], "upstream_out_of_scope")

    def test_request_budget_stops_before_upstream(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"})
        for _ in range(3):
            status, _document = self.request("GET", "/v1/rest/repos/octo/demo")
            self.assertEqual(status, 200)
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 429, document)
        self.assertEqual(document["error"], "budget_exhausted")
        self.assertEqual(len(self.upstream.requests), 3)

    def test_response_byte_budget_spans_requests(self):
        body = b'{"full_name":"octo/demo","x":"' + b"a" * 900 + b'"}'
        self.route("/repos/octo/demo", body=body)
        self.server.relay._opener.response_byte_budget = len(body) * 2 + 10
        for _ in range(2):
            status, _document = self.request("GET", "/v1/rest/repos/octo/demo")
            self.assertEqual(status, 200)
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 429, document)
        self.assertEqual(document["error"], "byte_budget_exhausted")

    def test_slow_upstream_hits_the_deadline(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"}, delay=3)
        started = time.monotonic()
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(status, 504, document)
        self.assertEqual(document["error"], "upstream_timeout")

    def test_dripping_body_hits_the_deadline(self):
        self.route("/repos/octo/demo", body=b" ", drip=0.1)
        started = time.monotonic()
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(status, 504, document)

    def test_queued_requests_share_the_deadline(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"}, delay=3)
        results = []

        def call():
            results.append(self.request("GET", "/v1/rest/repos/octo/demo")[0])

        threads = [threading.Thread(target=call) for _ in range(3)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(sorted(results), [504, 504, 504])


class RateLimitStatusTests(RelayHarness):
    """A received rate-limit answer whose retry wait passes the deadline keeps its status.

    The relay still does not wait and does not ask again: one upstream request,
    one budget unit, answered at once as upstream_failed with the status it got."""

    budget = 20
    timeout_seconds = 5.0
    RATE = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-used": "60",
            "x-ratelimit-resource": "graphql"}

    def far_reset(self):
        return str(int(time.time()) + 3000)

    def assert_kept(self, status, document, upstream_status, headers, message):
        self.assertEqual(status, 502, document)
        self.assertEqual((document["error"], document["upstream_status"]), ("upstream_failed", upstream_status))
        self.assertGreaterEqual(document["upstream_retry_wait_seconds"], self.timeout_seconds - 1)
        self.assertEqual(document["upstream_rate_limit"], headers)
        # The upstream's own message, as for any other upstream failure; the relay's
        # reason is a separate field.
        self.assertEqual(document["message"], message)
        self.assertEqual(document["relay_reason"], "the retry wait passes the relay request deadline")
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(document["budget"]["consumed"], 1)

    def test_rest_403_rate_limit_with_a_far_reset_keeps_403(self):
        reset = self.far_reset()
        self.route("/repos/octo/demo", {"message": "API rate limit exceeded"}, status=403,
                   headers=dict(self.RATE, **{"x-ratelimit-reset": reset}))
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assert_kept(status, document, 403, dict(self.RATE, **{"x-ratelimit-reset": reset}),
                         "API rate limit exceeded")

    def test_graphql_429_with_a_long_retry_after_keeps_429(self):
        self.route("/graphql", {"message": "Too Many Requests"}, status=429, headers={"Retry-After": "60"})
        status, document = self.graphql({"operation": "IssueEvidence",
                                         "variables": {"owner": "octo", "name": "demo", "number": 1}})
        self.assert_kept(status, document, 429, {"retry-after": "60"}, "Too Many Requests")

    def test_graphql_403_rate_message_without_headers_keeps_403(self):
        self.route("/graphql", {"message": "You have exceeded a secondary rate limit"}, status=403)
        status, document = self.graphql({"operation": "IssueEvidence",
                                         "variables": {"owner": "octo", "name": "demo", "number": 1}})
        self.assert_kept(status, document, 403, {}, "You have exceeded a secondary rate limit")

    def test_unsafe_or_malformed_rate_headers_are_not_forwarded(self):
        self.route("/repos/octo/demo", {"message": "API rate limit exceeded"}, status=403,
                   headers={"Retry-After": "60", "x-ratelimit-reset": "soon; x=1", "Set-Cookie": "a=b",
                            "x-github-request-id": "ABC"})
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assert_kept(status, document, 403, {"retry-after": "60"}, "API rate limit exceeded")

    def test_a_graphql_403_without_quota_is_returned_at_once_with_its_headers(self):
        quota = {"x-ratelimit-resource": "graphql", "x-ratelimit-limit": "0", "x-ratelimit-remaining": "0",
                 "x-ratelimit-used": "0", "x-ratelimit-reset": self.far_reset()}
        self.route("/graphql", {"message": "API rate limit exceeded"}, status=403, headers=quota)
        status, document = self.graphql({"operation": "IssueEvidence",
                                         "variables": {"owner": "octo", "name": "demo", "number": 1}})
        self.assertEqual((status, document["error"], document["upstream_status"]), (502, "upstream_failed", 403))
        self.assertEqual(document["upstream_rate_limit"], quota)
        self.assertNotIn("upstream_retry_wait_seconds", document)  # no wait was planned
        self.assertEqual((len(self.upstream.requests), document["budget"]["consumed"]), (1, 1))

    def test_a_short_wait_is_still_retried_inside_the_deadline(self):
        # Unchanged: a wait that fits is waited (sleep is patched here) and retried.
        self.route("/repos/octo/demo", {"message": "API rate limit exceeded"}, status=403,
                   headers={"Retry-After": "1"})
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual((status, document["error"], document["upstream_status"]), (502, "upstream_failed", 403))
        self.assertNotIn("upstream_retry_wait_seconds", document)
        self.assertEqual(len(self.upstream.requests), 4)

    def test_a_transport_failure_before_a_long_wait_stays_a_timeout(self):
        # No answer was received on the last attempt, so there is no status to keep: still 504.
        relay, opener = self.server.relay, self.server.relay._opener
        self.route("/repos/octo/demo", {"message": "API rate limit exceeded"}, status=403,
                   headers={"Retry-After": "60"})
        self.request("GET", "/v1/rest/repos/octo/demo")  # leaves a 403 as the last answer
        self.assertEqual(opener.last_response, (403, {"retry-after": "60"}))
        with mock.patch.object(self.opener, "open", side_effect=OSError("connection reset")):
            with self.assertRaises(OSError):
                opener.open(urllib.request.Request(API + "/repos/octo/demo"), timeout=1)
        self.assertIsNone(opener.last_response)
        opener.deadline = time.monotonic() + 0.5
        try:
            with self.assertRaises(relay_module.DeadlineExceeded) as caught:
                relay._sleep_within_deadline(2.0)
        finally:
            opener.deadline = None
        self.assertNotIsInstance(caught.exception, relay_module.RetryWaitPassesDeadline)

    def test_a_slow_upstream_is_still_a_timeout(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"}, delay=8)
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual((status, document["error"]), (504, "upstream_timeout"), document)
        self.assertNotIn("upstream_status", document)


class RetryBudgetTests(RelayHarness):
    budget = 3

    def test_retries_count_against_the_request_budget(self):
        self.route("/repos/octo/demo", {"message": "unavailable"}, status=503)
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual(status, 429, document)
        self.assertEqual(len(self.upstream.requests), 3)


class ConnectionCapTests(RelayHarness):
    def test_connections_past_the_cap_are_closed(self):
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5)
                for _ in range(relay_module.MAX_CONCURRENT_CONNECTIONS)]
        try:
            time.sleep(0.2)
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as extra:
                try:
                    extra.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
                    self.assertEqual(extra.recv(1024), b"")
                except ConnectionResetError:
                    pass
        finally:
            for connection in idle:
                connection.close()
        time.sleep(0.2)
        status, _document = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)


class NetworkGuardTests(unittest.TestCase):
    def test_non_loopback_connections_are_refused(self):
        with self.assertRaises(PermissionError):
            socket.create_connection(("140.82.112.6", 443), timeout=1)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        home = Path(self.temporary.name)
        (home / ".codex").mkdir()
        (home / ".codex/skills").symlink_to(SKILLS)
        self.environment = dict(os.environ, HOME=str(home), PYTHONPATH=str(FIXTURES),
                                GH_TOKEN="gho_parentsecret", PYTHONDONTWRITEBYTECODE="1")

    def run_relay(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=30)

    def test_subprocess_network_guard_is_active(self):
        result = subprocess.run(
            [sys.executable, "-c", "import socket; socket.create_connection(('140.82.112.6', 443), timeout=1)"],
            env=self.environment, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test network guard refused", result.stderr)

    def test_boundaries_are_required(self):
        full = ["--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
                "--response-byte-budget", "10000", "--timeout-seconds", "5"]
        for index in range(0, len(full), 2):
            with self.subTest(missing=full[index]):
                result = self.run_relay(*(full[:index] + full[index + 2:]))
                self.assertEqual(result.returncode, 2)
        for bad in (["--repo", "octo"], ["--repo", "https://github.com/octo/demo"],
                    ["--request-budget", "0"], ["--timeout-seconds", "301"], ["--upstream", "http://x"],
                    ["--skills-dir", "dot_codex/skills"], ["--skills-dir", self.temporary.name],
                    ["--repo", "octo/demo"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_relay(*(full + bad)).returncode, 2)

    def test_ready_line_and_loopback_service(self):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--repo", "octo/demo", "--request-budget", "5",
             "--max-response-bytes", "1000", "--response-byte-budget", "10000", "--timeout-seconds", "5"],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            ready = json.loads(process.stdout.readline())
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["host"], "127.0.0.1")
            self.assertEqual(ready["base_url"], "http://127.0.0.1:{0}".format(ready["port"]))
            self.assertEqual(ready["sources"], source_manifest)
            connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=10)
            connection.request("GET", "/v1/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["budget"]["consumed"], 0)
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=10)
            connection.request("POST", "/v1/rest/repos/octo/demo/issues", body=b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
            connection.close()
        finally:
            process.terminate()
            _stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("parentsecret", stderr)

    def test_explicit_skills_dir_is_loaded_and_recorded(self):
        (Path(self.temporary.name) / ".codex/skills").unlink()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--repo", "octo/demo", "--request-budget", "5",
             "--max-response-bytes", "1000", "--response-byte-budget", "10000", "--timeout-seconds", "5",
             "--skills-dir", str(SKILLS)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            ready = json.loads(process.stdout.readline())
        finally:
            process.terminate()
            process.communicate(timeout=10)
        expected = [
            {"role": "issues", "path": str((SKILLS / relay_module.ISSUES_SCRIPT).resolve()),
             "sha256": hashlib.sha256((SKILLS / relay_module.ISSUES_SCRIPT).read_bytes()).hexdigest()},
            {"role": "sibling", "path": str((SKILLS / relay_module.SIBLING_SCRIPT).resolve()),
             "sha256": hashlib.sha256((SKILLS / relay_module.SIBLING_SCRIPT).read_bytes()).hexdigest()},
        ]
        self.assertEqual(ready["sources"], expected)
        self.assertEqual(process.returncode, 0)

    def test_source_changed_while_loading_fails(self):
        with mock.patch.object(relay_module, "_sha256", side_effect=["a", "b", "a", "changed"]):
            with self.assertRaises(ImportError):
                relay_module.load_issues_module(SKILLS)

    def test_http_cannot_choose_the_client_source(self):
        args = relay_module.build_parser().parse_args([
            "--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
            "--response-byte-budget", "10000", "--timeout-seconds", "5",
        ])
        server = relay_module.build_server(args, opener=None, issues_module=issues_module)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for target in ("/v1/rest/repos/octo/demo?skills_dir=/tmp", "/v1/rest/repos/octo/demo?source=/tmp/x.py"):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", target)
            response = connection.getresponse()
            document = json.loads(response.read())
            connection.close()
            self.assertEqual((response.status, document["error"]), (400, "query_not_allowed"))
            self.assertNotIn("sources", document)

    def test_server_bind_does_no_reverse_lookup(self):
        # HTTPServer.server_bind would call socket.getfqdn(127.0.0.1); a slow
        # resolver then holds the ready line back past the worker's deadline.
        args = relay_module.build_parser().parse_args([
            "--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
            "--response-byte-budget", "10000", "--timeout-seconds", "5",
        ])
        refused = AssertionError("no resolver lookup while binding")
        with mock.patch.object(socket, "getfqdn", side_effect=refused), \
                mock.patch.object(socket, "gethostbyaddr", side_effect=refused):
            server = relay_module.build_server(args, opener=None, issues_module=issues_module)
        self.addCleanup(server.server_close)
        port = server.socket.getsockname()[1]
        self.assertEqual(server.server_address, ("127.0.0.1", port))
        self.assertEqual((server.server_name, server.server_port), ("127.0.0.1", port))

    def test_missing_installed_skills_fail_closed(self):
        (Path(self.temporary.name) / ".codex/skills").unlink()
        result = self.run_relay("--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
                                "--response-byte-budget", "10000", "--timeout-seconds", "5")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
