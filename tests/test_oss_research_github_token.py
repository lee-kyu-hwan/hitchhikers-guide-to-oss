"""The read-only GitHub token path: the relay alone holds the token. Offline, fake tokens only.

    python3 -B -m unittest tests.test_oss_research_github_token

A GitHub token (a fine-grained read-only PAT) can be handed to the relay as a FILE
(--github-token-file). Without the argument everything is anonymous, as before.
The relay adds `Authorization: Bearer` to its own requests to api.github.com and to
nothing else; the srt child that runs the collector or the verifier never sees the
token or the file; no record, log, error text or output holds the value. Every token
here is a sentinel made for the test (SENTINEL); no real token exists. The upstream is
a loopback fixture; non-loopback connections are refused.
"""

import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import tests.test_oss_research_candidate_reads as reads_tests
import tests.test_oss_research_collect_issues as issues_tests
import tests.test_oss_research_relay as relay_tests
import tests.test_oss_research_worker_linux as linux_tests

relay_module = relay_tests.relay_module
REPO = relay_tests.ROOT
BIN = REPO / "bin"
VERIFY = REPO / "share/linux/verify_pickup.py"
SENTINEL = "ghp_SENTINEL0123456789ABCDEFGHIJKLMNOP"  # only ever in a test's temporary directory
OTHER = "ghp_PARENTSECRETZYXWVUTSRQPONMLKJIHGFE"


def setUpModule():
    relay_tests.setUpModule()  # the loopback-only network guard


def tearDownModule():
    relay_tests.tearDownModule()


def make_token_file(root: Path, content: bytes | str = SENTINEL + "\n", mode: int = 0o600,
                    directory_mode: int = 0o700, name: str = "github-readonly") -> Path:
    """<root>/<name>/token, the shape the guest installs and the recommended host path has."""
    directory = root / name
    directory.mkdir(mode=directory_mode, exist_ok=True)
    directory.chmod(directory_mode)
    path = directory / "token"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(mode)
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-ghtoken-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class TokenFileTests(TempCase):
    def load(self, path):
        return relay_module.load_github_token(path)

    def test_a_private_single_line_file_is_loaded_and_only_a_digest_is_kept(self):
        for content in (SENTINEL, SENTINEL + "\n", "  " + SENTINEL + " \n", "github_pat_" + "A1_-" * 20 + "\r\n"):
            with self.subTest(content=content[:6]):
                path = make_token_file(self.root, content, name="ok-%d" % abs(hash(content) % 1000))
                token = self.load(path)
                self.assertEqual(token.reveal(), content.strip())
                self.assertEqual(token.file_sha256, hashlib.sha256(content.encode()).hexdigest())
                for text in (repr(token), str(token), format(token), repr([token])):
                    self.assertNotIn(token.reveal(), text)
                with self.assertRaises(TypeError):
                    import pickle
                    pickle.dumps(token)
        # 0400 is private too.
        path = make_token_file(self.root, mode=0o400, name="readonly")
        self.assertEqual(self.load(path).reveal(), SENTINEL)

    def test_a_file_that_is_not_private_or_not_a_token_is_refused_without_its_content(self):
        good = SENTINEL
        cases = {
            "mode 0644": dict(mode=0o644), "mode 0640": dict(mode=0o640), "mode 0660": dict(mode=0o660),
            "mode 0604": dict(mode=0o604), "directory 0755": dict(directory_mode=0o755),
            "directory 0770": dict(directory_mode=0o770), "directory 0701": dict(directory_mode=0o701),
            "empty": dict(content=""), "only whitespace": dict(content=" \n\t \n"),
            "two lines": dict(content=good + "\n" + good + "\n"), "blank line then token": dict(content="\n" + good + "\n\n" + good),
            "space inside": dict(content=good[:10] + " " + good[10:]),
            "too short": dict(content="ghp_short"), "too long": dict(content="a" * 513),
            "a dot": dict(content=good + ".x"), "a slash": dict(content=good + "/x"), "an equals sign": dict(content=good + "="),
            "non-ASCII": dict(content=good.encode() + "é".encode()), "NUL": dict(content=good.encode() + b"\0"),
            "a quote": dict(content=good + '"'),
        }
        for label, change in cases.items():
            with self.subTest(label):
                path = make_token_file(self.root, name="bad-%s" % re.sub(r"\W", "", label), **change)
                with self.assertRaises(relay_module.TokenFileError) as refused:
                    self.load(path)
                self.assertNotIn(good, str(refused.exception))
                self.assertNotIn(good[:12], str(refused.exception))
                self.assertNotIn(str(path), str(refused.exception))  # fixed text; the path is the caller's to name

    def test_links_special_files_and_other_owners_are_refused(self):
        real = make_token_file(self.root, name="real")
        link = self.root / "link"
        link.symlink_to(real.parent)
        with self.assertRaises(relay_module.TokenFileError):  # the directory is a link
            self.load(link / "token")
        file_link = make_token_file(self.root, name="linkfile").parent / "alias"
        file_link.symlink_to(real)
        with self.assertRaises(relay_module.TokenFileError):  # the file is a link
            self.load(file_link)
        hard = make_token_file(self.root, name="hard")
        os.link(hard, hard.parent / "second")
        with self.assertRaises(relay_module.TokenFileError):  # two links to the same file
            self.load(hard)
        fifo_dir = self.root / "fifo"
        fifo_dir.mkdir(mode=0o700)
        os.mkfifo(fifo_dir / "token", 0o600)
        with self.assertRaises(relay_module.TokenFileError):  # not a regular file; no blocking open
            self.load(fifo_dir / "token")
        directory = self.root / "dir" / "token"
        directory.mkdir(parents=True, mode=0o700)
        with self.assertRaises(relay_module.TokenFileError):
            self.load(directory)
        for missing in (self.root / "nothing" / "token", self.root / "real" / "absent"):
            with self.assertRaises(relay_module.TokenFileError):
                self.load(missing)
        for relative in ("token", "github-readonly/token", "./real/token"):
            with self.assertRaises(relay_module.TokenFileError):
                self.load(Path(relative))
        # A file or directory of another owner: the checks compare with the process's own uid.
        with mock.patch.object(relay_module.os, "geteuid", lambda: os.getuid() + 1):
            with self.assertRaises(relay_module.TokenFileError):
                self.load(real)
        self.assertEqual(self.load(real).reveal(), SENTINEL)  # the positive control

    def test_a_file_that_changes_while_it_is_read_is_refused(self):
        path = make_token_file(self.root)
        real_read = os.read

        def changing(descriptor, size):
            data = real_read(descriptor, size)
            with open(path, "ab") as handle:
                handle.write(b"x")
            return data
        with mock.patch.object(relay_module.os, "read", changing):
            with self.assertRaises(relay_module.TokenFileError):
                self.load(path)

    def test_the_argument_and_the_default(self):
        parser = relay_module.build_parser()
        base = ["--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
                "--response-byte-budget", "10000", "--timeout-seconds", "5"]
        self.assertIsNone(parser.parse_args(base).github_token_file)  # no argument: anonymous
        path = make_token_file(self.root)
        self.assertEqual(parser.parse_args(base + ["--github-token-file", str(path)]).github_token_file, path)
        for bad in ("relative/token", "", "token"):
            with self.subTest(bad), self.assertRaises(SystemExit):
                with mock.patch("sys.stderr"):
                    parser.parse_args(base + ["--github-token-file", bad])
        with self.assertRaises(SystemExit):  # given twice
            with mock.patch("sys.stderr"):
                parser.parse_args(base + ["--github-token-file", str(path), "--github-token-file", str(path)])
        # No environment variable or option stands in for it.
        text = relay_tests.SCRIPT.read_text()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "gh auth token"):
            self.assertNotIn('environ.get("%s' % name, text)
            self.assertNotIn('environ["%s' % name, text)
        self.assertEqual(sorted(a for a in re.findall(r'add_argument\("(--[a-z-]+)"', text)),
                         ["--github-token-file", "--max-response-bytes", "--repo", "--request-budget",
                          "--response-byte-budget", "--skills-dir", "--timeout-seconds"])


class TokenHarness(relay_tests.RelayHarness):
    """The relay harness with a token file: upstream requests are recorded as the fixture saw them."""

    def setUp(self):
        super().setUp()  # the anonymous relay and the fixture upstream; replaced below
        self.token_root = Path(tempfile.mkdtemp(prefix="osr-ghtoken-")).resolve()
        self.addCleanup(shutil.rmtree, self.token_root, ignore_errors=True)
        self.token_path = make_token_file(self.token_root)
        self.start_relay(self.token_path)

    def start_relay(self, token_path):
        import threading
        args = relay_module.build_parser().parse_args([
            "--repo", "octo/demo", "--repo", "octo/.github", "--request-budget", str(self.budget),
            "--max-response-bytes", str(self.max_response_bytes),
            "--response-byte-budget", str(self.response_byte_budget),
            "--timeout-seconds", str(self.timeout_seconds),
            *(["--github-token-file", str(token_path)] if token_path is not None else [])])
        self.opener.requests.clear()
        self.server = relay_module.build_server(args, opener=self.opener, issues_module=relay_tests.issues_module)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def bearer(self, request):
        return request["headers"].get("authorization")


class RelayTokenTests(TokenHarness):
    def test_every_upstream_request_carries_the_bearer_header(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo", "default_branch": "main"})
        self.route("/repos/octo/demo/issues/1/comments", [])
        self.route("/search/issues", {"total_count": 0, "incomplete_results": False, "items": []})
        self.route("/graphql", {"data": {"repository": {"issue": {"number": 1}}}})
        for target in ("/v1/rest/repos/octo/demo", "/v1/rest/repos/octo/demo/issues/1/comments",
                       "/v1/rest/search/issues?q=repo%3Aocto%2Fdemo+%22x%22+is%3Aissue+is%3Aopen"):
            status, _ = self.request("GET", target)
            self.assertEqual(status, 200, target)
        status, _ = self.graphql({"operation": "IssueEvidence", "variables": {"owner": "octo", "name": "demo", "number": 1}})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), 4)
        for request in self.upstream.requests:
            self.assertEqual(self.bearer(request), "Bearer " + SENTINEL, request["path"])
        # The token file's value, not the parent's environment, is what is sent.
        self.assertNotIn(os.environ["GH_TOKEN"], str(self.upstream.requests))
        self.assertNotIn(os.environ["GITHUB_TOKEN"], str(self.upstream.requests))

    def test_without_the_argument_nothing_is_sent_even_with_token_variables_set(self):
        self.start_relay(None)
        self.route("/repos/octo/demo", {"full_name": "octo/demo", "default_branch": "main"})
        self.assertEqual(self.request("GET", "/v1/rest/repos/octo/demo")[0], 200)
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertNotIn("authorization", self.upstream.requests[0]["headers"])
        self.assertIs(self.request("GET", "/v1/health")[1]["authenticated"], False)

    def test_health_says_authenticated_and_nothing_else_about_the_token(self):
        status, document = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertIs(document["authenticated"], True)
        self.assertNotIn(SENTINEL, json.dumps(document))
        self.assertEqual(set(document), {"ok", "budget", "authenticated"})

    def test_the_request_budget_and_route_rules_are_the_same_with_a_token(self):
        refused = [
            ("GET", "/v1/rest/user", 403), ("GET", "/v1/rest/repos/other/repo", 403),
            ("GET", "/v1/rest/repos/octo/demo/git/refs/heads/main", 403),
            ("GET", "/v1/rest/repos/octo/demo/actions/secrets", 403),
            ("GET", "/v1/rest/search/code?q=repo%3Aocto%2Fdemo+secret", 403),
            ("POST", "/v1/rest/repos/octo/demo/issues", 405), ("PUT", "/v1/rest/repos/octo/demo", 405),
            ("PATCH", "/v1/rest/repos/octo/demo", 405), ("DELETE", "/v1/rest/repos/octo/demo", 405),
            ("GET", "/v1/rest/repos/octo/demo/issues/1?state=closed", 400),
            ("GET", "/v1/rest/repos/octo/demo/contents/a%2fb?ref=" + "a" * 40, 400),
        ]
        for method, target, expected in refused:
            with self.subTest(method=method, target=target):
                status, document = self.request(method, target)
                self.assertEqual(status, expected, document)
        status, document = self.graphql({"query": "mutation { addComment(input: {}) { clientMutationId } }"})
        self.assertEqual(status, 400, document)
        status, document = self.graphql({"operation": "IssueEvidence", "variables": {"owner": "other", "name": "repo", "number": 1}})
        self.assertEqual(status, 403, document)
        self.assertEqual(self.upstream.requests, [])  # nothing reached the upstream, token or not

    def test_an_authorization_header_from_the_child_is_still_refused(self):
        for name in ("Authorization", "Proxy-Authorization", "Cookie"):
            with self.subTest(name):
                status, document = self.request("GET", "/v1/rest/repos/octo/demo", headers={name: "Bearer " + OTHER})
                self.assertEqual((status, document["error"]), (400, "header_not_allowed"))
        self.assertEqual(self.upstream.requests, [])

    def test_the_header_goes_only_to_api_github_com(self):
        class Inner:
            def open(self, request, timeout):
                raise AssertionError("must not be sent")
        bounded = relay_module.BoundedOpener(Inner(), max_response_bytes=1000, response_byte_budget=1000,
                                             clock=lambda: 0.0)
        for url in ("https://example.com/repos/octo/demo", "http://api.github.com/repos/octo/demo",
                    "https://api.github.com.evil.example/repos/octo/demo", "https://uploads.github.com/x"):
            with self.subTest(url):
                request = relay_tests.urllib.request.Request(url, headers={"Authorization": "Bearer " + SENTINEL})
                with self.assertRaises(relay_module.UnsafeCredentialUse):
                    bounded.open(request, 5)

    def test_no_response_error_or_log_line_holds_the_token(self):
        seen = []
        # The upstream says something that contains the token (it should not, but an error text can).
        self.route("/repos/octo/demo", {"message": "Bad credentials " + SENTINEL + " for Bearer " + SENTINEL}, status=401)
        self.route("/repos/octo/.github", {"full_name": "octo/.github", "note": SENTINEL, "default_branch": "main"})
        self.route("/repos/octo/demo/issues/2", {"message": "API rate limit exceeded for " + SENTINEL}, status=403,
                   headers={"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-used": "60",
                            "x-ratelimit-reset": str(int(relay_tests.time.time()) + 3000),
                            "x-ratelimit-resource": "core"})
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            for method, target in (("GET", "/v1/rest/repos/octo/demo"), ("GET", "/v1/rest/repos/octo/.github"),
                                   ("GET", "/v1/rest/repos/octo/demo/issues/2"), ("GET", "/v1/health"),
                                   ("GET", "/v1/rest/repos/octo/demo/issues/9"), ("POST", "/v1/rest/repos/octo/demo")):
                status, document = self.request(method, target)
                seen.append(json.dumps(document))
        text = "\n".join(seen) + stderr.getvalue()
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(SENTINEL[:16], text)
        self.assertIn("<credential>", text)  # the places where it would have been
        self.assertTrue(stderr.getvalue().strip(), "the rate-limit event is still logged")

    def test_a_403_without_quota_is_still_returned_at_once_and_not_retried_with_a_token(self):
        quota = {"x-ratelimit-resource": "graphql", "x-ratelimit-limit": "0", "x-ratelimit-remaining": "0",
                 "x-ratelimit-used": "0", "x-ratelimit-reset": str(int(relay_tests.time.time()) + 3600)}
        self.route("/graphql", {"message": "API rate limit exceeded"}, status=403, headers=quota)
        status, document = self.graphql({"operation": "IssueEvidence",
                                         "variables": {"owner": "octo", "name": "demo", "number": 1}})
        self.assertEqual((status, document["error"], document["upstream_status"]), (502, "upstream_failed", 403))
        self.assertEqual(document["upstream_rate_limit"], quota)
        self.assertEqual((len(self.upstream.requests), document["budget"]["consumed"]), (1, 1))
        self.assertEqual(self.bearer(self.upstream.requests[0]), "Bearer " + SENTINEL)

    def test_a_long_retry_wait_is_still_not_waited_for_with_a_token(self):
        self.route("/repos/octo/demo", {"message": "API rate limit exceeded"}, status=403,
                   headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0", "x-ratelimit-used": "5000",
                            "x-ratelimit-resource": "core", "x-ratelimit-reset": str(int(relay_tests.time.time()) + 3000)})
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual((status, document["error"], document["upstream_status"]), (502, "upstream_failed", 403))
        self.assertEqual(len(self.upstream.requests), 1)

    def test_a_redirect_is_not_followed_so_the_header_cannot_leave_api_github_com(self):
        self.route("/repos/octo/demo", {"message": "moved"}, status=302, headers={"Location": "http://127.0.0.1:1/x"})
        status, document = self.request("GET", "/v1/rest/repos/octo/demo")
        self.assertEqual((status, document["error"]), (502, "upstream_failed"))
        self.assertEqual(len(self.upstream.requests), 1)


class RelayCommandLineTests(TempCase):
    def setUp(self):
        super().setUp()
        home = self.root / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex/skills").symlink_to(relay_tests.SKILLS)
        self.environment = dict(os.environ, HOME=str(home), PYTHONPATH=str(relay_tests.FIXTURES),
                                GH_TOKEN="gho_parentsecret", PYTHONDONTWRITEBYTECODE="1")
        self.full = ["--repo", "octo/demo", "--request-budget", "5", "--max-response-bytes", "1000",
                     "--response-byte-budget", "10000", "--timeout-seconds", "5"]

    def run_relay(self, *arguments):
        return subprocess.run([sys.executable, str(relay_tests.SCRIPT), *self.full, *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=30)

    def test_a_bad_token_file_stops_the_relay_before_it_is_ready_with_a_fixed_message(self):
        bad = {"mode 0644": make_token_file(self.root, mode=0o644, name="a"),
               "empty": make_token_file(self.root, content="", name="b"),
               "two lines": make_token_file(self.root, content=SENTINEL + "\n" + SENTINEL, name="c")}
        for label, path in bad.items():
            with self.subTest(label):
                result = self.run_relay("--github-token-file", str(path))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")  # no ready line
                self.assertNotIn(SENTINEL, result.stdout + result.stderr)
                self.assertIn("GitHub token file", result.stderr)

    def test_a_good_token_file_gives_the_same_ready_line_and_nothing_in_the_output(self):
        path = make_token_file(self.root)
        process = subprocess.Popen([sys.executable, str(relay_tests.SCRIPT), *self.full, "--github-token-file", str(path)],
                                   env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = process.stdout.readline()
            ready = json.loads(line)
            self.assertEqual(set(ready), {"event", "host", "port", "base_url", "sources"})  # unchanged
            self.assertEqual(ready["sources"], relay_tests.source_manifest)
            self.assertNotIn(SENTINEL, line)
            connection = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=10)
            connection.request("GET", "/v1/health")
            document = json.loads(connection.getresponse().read())
            connection.close()
            self.assertIs(document["authenticated"], True)
        finally:
            process.terminate()
            out, err = process.communicate(timeout=10)
        self.assertNotIn(SENTINEL, out + err)

    def test_the_token_is_not_in_the_relays_arguments_or_environment_by_the_worker_launcher(self):
        worker = linux_tests.worker
        request = worker.checked_relay_request(["octo/demo"], 5, 1000, 10000, 5.0)
        launch = worker.relay_launch(request, dict(os.environ, GH_TOKEN="gho_parentsecret"))
        self.assertNotIn("GH_TOKEN", launch.environment)
        self.assertNotIn("--github-token-file", launch.argv)  # the entry adds it, from its own argument


def load_entry(name):
    from importlib.machinery import SourceFileLoader
    import importlib.util
    loader = SourceFileLoader("osr_token_" + name.replace("-", "_"), str(BIN / ("oss-research-" + name)))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


collect_module = load_entry("collect")
issues_module = load_entry("collect-issues")
reads_module = load_entry("candidate-reads")
worker = collect_module.load_worker()
ANONYMOUS = {"authenticated": False, "token_file_sha256": None}


class EntryCase(TempCase):
    """The three non-AI entries take --github-token-file the same way."""

    ENTRIES = ("collect", "collect-issues", "candidate-reads")

    def setUp(self):
        super().setUp()
        self.token = make_token_file(self.root)
        self.out = self.root / "out"
        self.out.mkdir()
        self.input = self.root / "in"
        self.input.mkdir(mode=0o755)
        (self.input / "analysis.json").write_text(json.dumps({"patterns": [{"pattern_id": "PAT-001", "superseded_by": None}]}))
        (self.input / "analysis.json").chmod(0o644)

    def arguments(self, entry, *extra, out=None):
        out = out or self.out
        interval = ["--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-09-08T00:00:00Z"]
        return {
            "collect": ["--out-dir", str(out), "--repo", "octo/demo", *interval],
            "collect-issues": ["--out-dir", str(out), "--repo", "octo/demo", "--date-field", "created",
                               "--state", "open", *interval],
            "candidate-reads": ["--out-dir", str(out), "--input-dir", str(self.input), "--stage", "discover",
                                "--repo", "octo/demo", "--pattern", "PAT-001"],
        }[entry] + list(extra)

    def module(self, entry):
        return {"collect": collect_module, "collect-issues": issues_module, "candidate-reads": reads_module}[entry]

    def run_main(self, entry, *extra, patch_run=True):
        """main() of an entry with the unit checks stubbed and run_* replaced; returns (exit, calls, launches)."""
        module = self.module(entry)
        calls, launches = [], []
        real_collect = collect_module if entry == "collect" else module.load_collect_entry()
        local = real_collect.load_worker()
        local.parent_eof_guard = lambda environ: True
        local.verify_runtime = lambda prefix, path: ["/opt/node", "/opt/oss-research/srt-0.0.77/node_modules/"
                                                     "@anthropic-ai/sandbox-runtime/dist/cli.js"]
        local.platform_problems = lambda prefix, platform: []
        fake = local.RelayLaunch(["relay", "--repo", "octo/demo"], {"PATH": "/usr/bin"}, [], "e" * 64)
        local.relay_launch = lambda request, parent: launches.append(("launch", request)) or fake
        stub_name = {"collect": "run_collection", "collect-issues": "run_collection", "candidate-reads": "run_reads"}[entry]

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            result = Path(self.out) / "run/result"
            result.mkdir(parents=True)  # what a finished run leaves, for main's summary line
            (result / "manifest.json").write_text(json.dumps({"status": "succeeded", "exit_code": 0,
                                                              "failures": [], "problems": []}))
            return True
        if entry == "collect":
            real_collect.load_worker = lambda *a, **k: local
        else:
            real_collect.load_worker = lambda *a, **k: local
        patches = [mock.patch.object(module, stub_name, fake_run), mock.patch.object(sys, "platform", "linux"),
                   mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO())]
        if entry != "collect":
            patches.append(mock.patch.object(module, "load_collect_entry", lambda *a: real_collect))
        else:
            patches.append(mock.patch.object(module, "load_worker", lambda *a, **k: local))
        with mock.patch.dict(os.environ, {"OSS_RESEARCH_SRT_PREFIX": "/opt/oss-research/srt-0.0.77", "PATH": "/usr/bin"}):
            for patch in patches:
                patch.start()
            try:
                code = module.main(self.arguments(entry, *extra))
            finally:
                for patch in reversed(patches):
                    patch.stop()
        return code, calls, launches


class EntryTokenTests(EntryCase):
    def test_the_token_file_reaches_the_relay_launch_and_nothing_else(self):
        for entry in self.ENTRIES:
            with self.subTest(entry):
                out = self.root / ("run-" + entry)
                out.mkdir()
                self.out = out
                code, calls, launches = self.run_main(entry, "--github-token-file", str(self.token))
                self.assertEqual(code, 0)
                self.assertEqual(len(calls), 1)
                args, kwargs = calls[0]
                launch = next(a for a in args if hasattr(a, "argv"))
                self.assertEqual(launch.argv[-2:], ["--github-token-file", str(self.token)])
                self.assertEqual(launch.argv.count("--github-token-file"), 1)
                self.assertEqual(launch.environment, {"PATH": "/usr/bin"})  # no token in the relay's environment
                self.assertNotIn(SENTINEL, json.dumps(list(launch.argv) + list(launch.environment.values())))
                marker = kwargs.get("github_token")
                self.assertEqual(marker, {"authenticated": True,
                                          "token_file_sha256": hashlib.sha256((SENTINEL + "\n").encode()).hexdigest()})
                self.assertEqual(Path(kwargs.get("github_token_file")), self.token)

    def test_without_the_argument_the_run_is_anonymous_exactly_as_before(self):
        for entry in self.ENTRIES:
            with self.subTest(entry):
                out = self.root / ("anon-" + entry)
                out.mkdir()
                self.out = out
                code, calls, _ = self.run_main(entry)
                self.assertEqual(code, 0)
                args, kwargs = calls[0]
                launch = next(a for a in args if hasattr(a, "argv"))
                self.assertNotIn("--github-token-file", launch.argv)
                self.assertEqual(kwargs.get("github_token"), ANONYMOUS)
                self.assertIsNone(kwargs.get("github_token_file"))

    def test_a_bad_argument_or_file_is_refused_before_the_relay_starts(self):
        link = self.root / "alias"
        link.symlink_to(self.token.parent)
        bad = {
            "relative": "token", "empty string": "", "not normalized": str(self.token.parent) + "/../github-readonly/token",
            "NUL": str(self.token) + "\0", "missing": str(self.root / "nowhere" / "token"),
            "linked directory": str(link / "token"),
            "mode 0644": str(make_token_file(self.root, mode=0o644, name="m644")),
            "empty file": str(make_token_file(self.root, content="", name="empty")),
            "two lines": str(make_token_file(self.root, content=SENTINEL + "\n" + SENTINEL, name="two")),
            "too long": str(make_token_file(self.root, content="a" * 600, name="long")),
            "a directory": str(self.root),
        }
        for entry in self.ENTRIES:
            for label, value in bad.items():
                with self.subTest(entry=entry, case=label):
                    out = self.root / ("bad-%s-%s" % (entry, re.sub(r"\W", "", label)))
                    out.mkdir()
                    self.out = out
                    code, calls, launches = self.run_main(entry, "--github-token-file", value)
                    self.assertEqual(code, 2)
                    self.assertEqual((calls, launches), ([], []))
                    self.assertEqual(list(out.iterdir()), [])

    def test_a_token_file_the_child_could_read_is_refused(self):
        # Inside the output directory, the input directory or the skills tree: the srt policy reads those.
        _, skills = worker.trusted_relay(Path(worker.__file__))
        for entry in self.ENTRIES:
            places = {"output directory": self.root / ("o-" + entry), "input directory": self.input}
            for label, directory in places.items():
                with self.subTest(entry=entry, place=label):
                    directory.mkdir(exist_ok=True, mode=0o755)
                    inside = make_token_file(directory, name="github-readonly") if label == "output directory" else \
                        make_token_file(self.input, name="github-readonly")
                    out = directory if label == "output directory" else self.root / ("o2-" + entry)
                    out.mkdir(exist_ok=True)
                    if label == "output directory":
                        # --out-dir must be empty, so the file cannot be there before; a later path is the guard.
                        shutil.rmtree(inside.parent)
                        self.out = out
                        guard = out / "run" / "x"
                        self.assertTrue(collect_module.token_path_is_readable_by_child(guard, [str(out)]))
                        continue
                    self.out = out
                    if entry != "candidate-reads":
                        continue  # the other entries have no input directory of the host's
                    code, calls, launches = self.run_main(entry, "--github-token-file", str(inside))
                    self.assertEqual((code, calls, launches), (2, [], []))
        # The policy-level guard itself, for every entry's read set: a path the policy reads is refused.
        for read in (["/usr/lib", "/run/oss-research-credentials/x/github-readonly/token"],
                     ["/run/oss-research-credentials"], ["/run/oss-research-credentials/x/github-readonly"]):
            self.assertTrue(collect_module.token_path_is_readable_by_child(
                Path("/run/oss-research-credentials/x/github-readonly/token"), read), read)
        for read in (["/usr/lib", "/opt/oss-research/src"], ["/run/oss-research-credentials/y"],
                     ["/run/oss-research-credentials/x/github-readonly/token2"]):
            self.assertFalse(collect_module.token_path_is_readable_by_child(
                Path("/run/oss-research-credentials/x/github-readonly/token"), read), read)
        self.assertTrue(skills)

    def test_the_policy_never_reads_the_token_path_and_a_policy_that_would_is_blocked(self):
        for entry in self.ENTRIES:
            with self.subTest(entry):
                seen = {}

                def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                                    environment, **options):
                    policy = policy_for(4242)
                    seen["read"] = policy["filesystem"]["allowRead"]
                    seen["command"] = command_for(Path("/p.json"))
                    seen["environment"] = environment
                    return True
                out = self.root / ("pol-" + entry)
                out.mkdir()
                inputs = self.input
                runtime = ["/opt/node", "/opt/oss-research/srt-0.0.77/node_modules/x/cli.js"]
                module = self.module(entry)
                marker = {"authenticated": True, "token_file_sha256": "a" * 64}
                with mock.patch.object(worker, "run_supervised", fake_supervised):
                    self.call_run(entry, module, runtime, out, inputs, marker, self.token)
                token = str(self.token)
                self.assertFalse([p for p in seen["read"] if token == p or token.startswith(p.rstrip("/") + "/")], seen["read"])
                self.assertNotIn(SENTINEL, json.dumps(seen["command"]) + json.dumps(seen["environment"]))
                self.assertNotIn(token, " ".join(seen["command"]))  # the child is never told where the file is
                self.assertFalse({k for k in seen["environment"] if "TOKEN" in k.upper()})
                # A read set that would cover the file is refused (Blocked), not run.
                out2 = self.root / ("pol2-" + entry)
                out2.mkdir()
                covering = str(self.token.parent)
                with mock.patch.object(worker, "run_supervised", fake_supervised), \
                        mock.patch.object(worker, "build_policy", lambda *a, **k: {"filesystem": {"allowRead": [covering]}, "network": {}}):
                    with self.assertRaises(worker.Blocked):
                        self.call_run(entry, module, runtime, out2, inputs, marker, self.token)

    def call_run(self, entry, module, runtime, out, inputs, marker, token_file):
        if entry == "collect":
            collection = module.checked_collection(worker, ["octo/demo"], "2026-09-01T00:00:00Z",
                                                   "2026-09-08T00:00:00Z", "all", 3, 60)
            return module.run_collection(worker, runtime, out, collection, 60.0, None,
                                         github_token=marker, github_token_file=token_file)
        if entry == "collect-issues":
            collection = module.checked_collection(worker, ["octo/demo"], "created", "open",
                                                   "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z", 5, 60)
            return module.run_collection(worker, module.load_collect_entry(), runtime, out, collection, 60.0, None,
                                         github_token=marker, github_token_file=token_file)
        reads = module.checked_reads(worker, "discover", "octo/demo", ["PAT-001"], [], 5, 5, 5, None, 60)
        return module.run_reads(worker, module.load_collect_entry(), runtime, out, inputs, reads, {}, 60.0, None,
                                github_token=marker, github_token_file=token_file)

    def test_the_marker_is_recorded_in_the_manifest_and_envelope_without_the_value(self):
        marker = {"authenticated": True, "token_file_sha256": hashlib.sha256(SENTINEL.encode()).hexdigest()}
        for entry in self.ENTRIES:
            for given, label in ((marker, "authenticated"), (None, "anonymous")):
                with self.subTest(entry=entry, case=label):
                    run_dir = self.root / ("rec-%s-%s" % (entry, label)) / "run"
                    run_dir.parent.mkdir()
                    worker.prepare_run(run_dir, {"filesystem": {}, "network": {}})
                    module = self.module(entry)
                    names = {"collect": ("corpus.json", "collection-manifest.json", "inventory.md", "transport.json"),
                             "collect-issues": issues_module.OUTPUTS,
                             "candidate-reads": reads_module.OUTPUT_FILES["discover"]}[entry]
                    for name in names:
                        (run_dir / "output" / name).write_text("{}")
                    relay = {"sources": [], "relay_sha256": "e" * 64, "port": 4242, "stderr": ""}
                    if entry == "collect":
                        collection = module.checked_collection(worker, ["octo/demo"], "2026-09-01T00:00:00Z",
                                                               "2026-09-08T00:00:00Z", "all", 3, 60)
                        record = module.recorder(worker, collection, "loopback-mock", "oss-research-0123456789abcdef",
                                                 github_token=given)
                        envelope_name = "collection-envelope.json"
                    elif entry == "collect-issues":
                        collection = module.checked_collection(worker, ["octo/demo"], "created", "open",
                                                               "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z", 5, 60)
                        record = module.recorder(worker, module.load_collect_entry(), collection, "loopback-mock",
                                                 "oss-research-0123456789abcdef", github_token=given)
                        envelope_name = "collection-envelope.json"
                    else:
                        reads = module.checked_reads(worker, "discover", "octo/demo", ["PAT-001"], [], 5, 5, 5, None, 60)
                        record = module.recorder(worker, module.load_collect_entry(), reads, {}, "loopback-mock",
                                                 "oss-research-0123456789abcdef", github_token=given)
                        envelope_name = "read-envelope.json"
                    record(run_dir, 0, "", "", [], relay)
                    manifest = json.loads((run_dir / "result/manifest.json").read_text())
                    envelope = json.loads((run_dir / "result" / envelope_name).read_text())
                    expected = marker if given else ANONYMOUS
                    self.assertEqual(manifest["github_token"], expected)
                    self.assertEqual(envelope["github_token"], expected)
                    for path in (run_dir / "result").iterdir():
                        self.assertNotIn(SENTINEL, path.read_text())

    def test_no_entry_reads_a_token_from_the_environment_or_adds_a_second_way_in(self):
        for entry in self.ENTRIES:
            code = (BIN / ("oss-research-" + entry)).read_text().split('"""', 2)[2]
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "gh auth token"):
                self.assertNotIn('environ.get("%s' % name, code, (entry, name))
                self.assertNotIn('environ["%s' % name, code, (entry, name))
            self.assertEqual(code.count('add_argument("--github-token-file"'), 1, entry)  # one option, one place


class CraftedPickupTokenTests(TempCase):
    """verify_pickup.py records the authenticated marker of --collect, --issues and --candidate-reads."""

    LEAKS = {"a GitHub token shape in relay.log": ("relay.log", "note ghp_" + "A" * 36 + "\n"),
             "a fine-grained token shape": ("relay.log", "github_pat_" + "B" * 40),
             "a bearer value": ("relay.log", "Authorization: Bearer abcdefghijklmnop"),
             "a bearer value in a JSON string": ("transport.json", None)}

    def collect_case(self):
        case = linux_tests.CollectPickupTests("test_only_a_whole_bound_collection_passes")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def issues_case(self):
        case = issues_tests.IssuePickupTests("test_a_bound_complete_collection_passes_and_a_bound_partial_one_is_only_partial")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def run_pickup(self, mode, case, archive, wrapper, *extra, upstream="loopback-mock"):
        head = {"collect": ["--collect", "--collector-sha256", case.SIBLING["sha256"]],
                "issues": ["--issues", "--issue-collector-sha256", case.ISSUES["sha256"] if hasattr(case, "ISSUES") else "",
                           "--sibling-sha256", case.SIBLING["sha256"]]}[mode]
        dest = self.root / ("out-%s-%d" % (archive.stem, len(list(self.root.glob("out-*")))))
        completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 20), str(dest), *head,
                                    "--wrapper-record", str(wrapper), "--upstream", upstream, *extra],
                                   capture_output=True, text=True, timeout=60)
        return completed.returncode, json.loads(completed.stdout)

    def with_marker(self, members, marker):
        members = json.loads(json.dumps(members))
        members["envelope"]["github_token"] = marker
        members["run-manifest.json"]["github_token"] = marker
        return members

    def test_the_marker_is_recorded_and_the_default_contract_still_passes(self):
        digest = "ab" * 32
        for mode, make in (("collect", self.collect_case), ("issues", self.issues_case)):
            case = make()
            for marker, authenticated in ((ANONYMOUS, False), ({"authenticated": True, "token_file_sha256": digest}, True)):
                with self.subTest(mode=mode, authenticated=authenticated):
                    members = self.with_marker(case.members(), marker)
                    archive = case.pack("t%d" % authenticated, members)
                    code, result = self.run_pickup(mode, case, archive, case.wrapper())
                    self.assertIn(code, (0, 3), result)
                    self.assertEqual(result["problems"], [])
                    self.assertIs(result["authenticated"], authenticated)
                    self.assertEqual(result["token_file_sha256"], marker["token_file_sha256"])

    def test_a_missing_or_malformed_marker_fails_the_pickup(self):
        digest = "ab" * 32
        bad = {"missing": None, "a string": "yes", "a list": [], "authenticated not a bool": {"authenticated": "true", "token_file_sha256": digest},
               "authenticated 1": {"authenticated": 1, "token_file_sha256": digest},
               "authenticated without a digest": {"authenticated": True, "token_file_sha256": None},
               "anonymous with a digest": {"authenticated": False, "token_file_sha256": digest},
               "short digest": {"authenticated": True, "token_file_sha256": digest[:-1]},
               "upper-case digest": {"authenticated": True, "token_file_sha256": digest.upper()},
               "digest not a string": {"authenticated": True, "token_file_sha256": 12345},
               "an extra key": {"authenticated": True, "token_file_sha256": digest, "token": "x"},
               "the value as the digest": {"authenticated": True, "token_file_sha256": SENTINEL},
               "a missing key": {"authenticated": True}}
        for mode, make in (("collect", self.collect_case), ("issues", self.issues_case)):
            case = make()
            for label, marker in bad.items():
                with self.subTest(mode=mode, case=label):
                    members = case.members()
                    if marker is None:  # the entry does not say at all
                        del members["envelope"]["github_token"], members["run-manifest.json"]["github_token"]
                    else:
                        members = self.with_marker(members, marker)
                    archive = case.pack("b%d" % len(list(self.root.glob("*.tar"))), members)
                    code, result = self.run_pickup(mode, case, archive, case.wrapper())
                    self.assertEqual(code, 1, result)
                    self.assertTrue(any("github token" in p for p in result["problems"]), result["problems"])
                    self.assertNotIn(SENTINEL, json.dumps(result))
            # The envelope and the run manifest must agree.
            members = self.with_marker(case.members(), ANONYMOUS)
            members["run-manifest.json"]["github_token"] = {"authenticated": True, "token_file_sha256": digest}
            code, result = self.run_pickup(mode, case, case.pack("disagree-" + mode, members), case.wrapper())
            self.assertEqual(code, 1, result)

    def test_a_token_shaped_value_in_a_member_is_refused_and_the_exact_value_when_the_host_names_it(self):
        value_file = self.root / "forbidden"
        value_file.write_text(SENTINEL + "\n")
        for mode, make in (("collect", self.collect_case), ("issues", self.issues_case)):
            case = make()
            for label, (member, text) in self.LEAKS.items():
                with self.subTest(mode=mode, leak=label):
                    members = self.with_marker(case.members(), ANONYMOUS)
                    if text is None:
                        members["transport.json"]["note"] = "sent Bearer abcdefghijklmnopqrstuv upstream"
                    else:
                        members[member] = text
                    code, result = self.run_pickup(mode, case, case.pack("l%d" % len(list(self.root.glob("*.tar"))), members),
                                                   case.wrapper())
                    self.assertEqual(code, 1, result)
                    self.assertTrue(any("credential-shaped" in p for p in result["problems"]), result["problems"])
            # The host names the exact value it forbids (the real token file on a real run).
            members = self.with_marker(case.members(), ANONYMOUS)
            clean = case.pack("clean-" + mode, members)
            code, result = self.run_pickup(mode, case, clean, case.wrapper(), "--forbid-value-file", str(value_file))
            self.assertIn(code, (0, 3), result)
            leaked = self.with_marker(case.members(), ANONYMOUS)
            leaked["relay.log"] = "debug " + SENTINEL
            code, result = self.run_pickup(mode, case, case.pack("leak-" + mode, leaked), case.wrapper(),
                                           "--forbid-value-file", str(value_file))
            self.assertEqual(code, 1, result)
            self.assertTrue(any("forbidden value" in p for p in result["problems"]), result["problems"])
            self.assertNotIn(SENTINEL, json.dumps(result))
            # Corpus members hold remote text and are not pattern-scanned; the exact value still is.
            remote = self.with_marker(case.members(), ANONYMOUS)
            remote["corpus.json"] = json.loads(json.dumps(remote["corpus.json"]))
            remote["corpus.json"]["remote_text"] = "an issue that pasted ghp_" + "C" * 36
            code, result = self.run_pickup(mode, case, case.pack("remote-" + mode, remote), case.wrapper())
            self.assertIn(code, (0, 3), result)
            # A file the host names that cannot be used fails closed (it is not silently skipped).
            for bad in (self.root / "absent", self.root):
                code, result = self.run_pickup(mode, case, clean, case.wrapper(), "--forbid-value-file", str(bad))
                self.assertEqual(code, 1, result)
            tiny = self.root / "tiny"
            tiny.write_text("abc\n")
            code, result = self.run_pickup(mode, case, clean, case.wrapper(), "--forbid-value-file", str(tiny))
            self.assertEqual(code, 1, result)


class ReadsPickupTokenTests(reads_tests.PickupCase):
    """--candidate-reads on the real offline pipeline: the marker and the exposure checks."""

    def tar_members(self, marker):
        if not hasattr(self, "_base"):
            run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
            self._base = self.members("discover", run_dir)
        members = self._base
        if marker == "missing":  # an entry that does not say (the recorder always does)
            for name in ("read-envelope.json", "run-manifest.json"):
                members = reads_tests.edited(members, name, lambda d: d.pop("github_token"))
        else:
            for name in ("read-envelope.json", "run-manifest.json"):
                members = reads_tests.edited(members, name, lambda d: d.__setitem__("github_token", marker))
        return members

    def pickup_extra(self, members, *extra):
        archive = self.root / ("x%d.tar" % len(list(self.root.glob("x*.tar"))))
        with tarfile.open(archive, "w") as bundle:
            for member, data in members.items():
                path = self.root / ("m-%s-%s" % (archive.stem, member))
                path.write_bytes(data)
                bundle.add(path, arcname=member)
        record = self.root / (archive.stem + "-wrapper.json")
        record.write_text(json.dumps({"unit": "oss-research-0123456789abcdef", "exit_code": 5, "failures": [],
                                      "stdout": json.dumps({"mode": "candidate-reads", "stage": "discover",
                                                            "status": "partial"}) + "\n"}) + "\n")
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 24), str(self.root / (archive.stem + "-out")),
             "--candidate-reads", "--stage", "discover", "--verifier-sha256", reads_tests.HOST_VERIFIER_SHA,
             "--relay-issues-sha256", reads_tests.HOST_ISSUES_SHA, "--relay-sibling-sha256", reads_tests.HOST_SIBLING_SHA,
             "--wrapper-record", str(record), "--upstream", "loopback-mock", *extra],
            capture_output=True, text=True, timeout=120)
        return completed.returncode, json.loads(completed.stdout)

    def test_the_marker_is_recorded(self):
        for marker, authenticated in ((ANONYMOUS, False),
                                      ({"authenticated": True, "token_file_sha256": "cd" * 32}, True)):
            with self.subTest(authenticated=authenticated):
                code, result = self.pickup_extra(self.tar_members(marker))
                self.assertEqual((code, result["problems"]), (3, []), result)
                self.assertIs(result["authenticated"], authenticated)
        code, result = self.pickup_extra(self.tar_members("missing"))
        self.assertEqual(code, 1)
        self.assertTrue(any("github token" in p for p in result["problems"]), result["problems"])

    def test_a_malformed_marker_and_an_exposed_value_fail(self):
        digest = "cd" * 32
        for label, marker in (("not a bool", {"authenticated": "yes", "token_file_sha256": digest}),
                              ("digest without authentication", {"authenticated": False, "token_file_sha256": digest}),
                              ("the value", {"authenticated": True, "token_file_sha256": SENTINEL})):
            with self.subTest(label):
                code, result = self.pickup_extra(self.tar_members(marker))
                self.assertEqual(code, 1, result)
        value_file = self.root / "forbidden"
        value_file.write_text(SENTINEL)
        members = self.tar_members({"authenticated": True, "token_file_sha256": digest})
        code, result = self.pickup_extra(members, "--forbid-value-file", str(value_file))
        self.assertEqual((code, result["problems"]), (3, []), result)
        leaked = reads_tests.edited(members, "transport.json", lambda d: d.__setitem__("note", "x " + SENTINEL))
        code, result = self.pickup_extra(leaked, "--forbid-value-file", str(value_file))
        self.assertEqual(code, 1, result)
        self.assertTrue(any("forbidden value" in p for p in result["problems"]), result["problems"])
        self.assertNotIn(SENTINEL, json.dumps(result))
        # A GitHub-token-shaped string in the transport record is refused even without a named value.
        shaped = reads_tests.edited(members, "transport.json", lambda d: d.__setitem__("note", "y ghp_" + "D" * 36))
        code, result = self.pickup_extra(shaped)
        self.assertEqual(code, 1, result)
        self.assertTrue(any("credential-shaped" in p for p in result["problems"]), result["problems"])


if __name__ == "__main__":
    unittest.main()
