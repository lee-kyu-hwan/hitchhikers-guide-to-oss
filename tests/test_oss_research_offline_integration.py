"""Run the relay and a sandboxed child together as real processes (#82 offline integration).

Run explicitly with the pinned srt install and the Homebrew interpreter:

    OSS_RESEARCH_SRT_PREFIX=/path/to/srt-0.0.77 \
      /opt/homebrew/bin/python3 -B -m unittest tests.test_oss_research_offline_integration -v

Three kinds of process take part, all on 127.0.0.1:

- a fixture upstream (the #192 relay tests' ``FixtureUpstream``) in this test
  process, standing in for api.github.com and counting every request it gets;
- the product relay, ``oss-research-relay``, as its own process group with
  ``--skills-dir`` fixed to this checkout's skills. A test-only
  sitecustomize sends its api.github.com calls to the fixture upstream and
  refuses and logs every other connection;
- a non-AI probe (tests/fixtures/oss_research_offline_integration/relay_probe.py)
  under the real srt, as its own process group, with the #189 runner's
  ``build_policy(..., relay_port, model=False)``: the relay port is the only
  allowed network destination (a model run also keeps the model endpoint).

The ``ProcessGroups`` wall-clock limit below belongs to this test harness only;
the relay's --timeout-seconds is a per-request deadline, not that limit. The
product's one deadline over relay and child is the #189 supervisor
(``run_supervised``), measured under the real srt by
tests/test_oss_research_worker_supervisor.py (``RealSrtSupervisorTests``: relay
read, hung relay request ending both groups, and the setsid escape that
survives cleanup). This file does not repeat those scenarios. No model, no
GitHub host, no credential and no skill or Agent runs here. Without the
variable the srt tests are skipped, which is not evidence of anything.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid


REPO = Path(__file__).resolve().parents[1]
RELAY = REPO / "bin/oss-research-relay"
WORKER = REPO / "bin/oss-research-worker"
SKILLS = REPO / "skills"
FIXTURES = REPO / "tests/fixtures/oss_research_offline_integration"
PROBE = FIXTURES / "relay_probe.py"
PREFIX = os.environ.get("OSS_RESEARCH_SRT_PREFIX")
SHA = "0123456789abcdef0123456789abcdef01234567"
API = "https://api.github.com"
SENTINEL = "gho_offline_integration_parent_secret"
# The relay's ready line holds two source paths and hashes; far below this.
MAX_LINE_BYTES = 16 * 1024
RELAY_SOURCES = {
    "issues": SKILLS / "collecting-open-source-issues/scripts/collect_open_source_issues.py",
    "sibling": SKILLS / "collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py",
}


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# Reused, not reimplemented: the #192 fixture upstream and the #189 runtime
# fixture's interpreter read paths and runner module.
relay_tests = _load("oss_research_relay_tests", REPO / "tests/test_oss_research_relay.py")
runtime_tests = _load("oss_research_worker_runtime_tests",
                      REPO / "tests/test_oss_research_worker_runtime.py")
worker = runtime_tests.worker
PYTHON = runtime_tests.PYTHON
FIXTURE_READ = runtime_tests.FIXTURE_READ

_real_connect = socket.socket.connect


def setUpModule():
    def loopback_only(self, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise PermissionError("parent test guard refused " + repr(address))
        return _real_connect(self, address)
    socket.socket.connect = loopback_only


def tearDownModule():
    socket.socket.connect = _real_connect


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ProcessGroups:
    """One wall-clock limit, enforced by the parent over every group it starts."""

    def __init__(self, wall_seconds):
        self.deadline = time.monotonic() + wall_seconds
        self.processes = []
        self.timed_out = False

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def start(self, argv, **options):
        if self.remaining() <= 0:
            raise TimeoutError("wall deadline passed; no new process is started")
        process = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        self.processes.append(process)
        return process

    def read_line(self, process):
        """Read one bounded stdout line before the wall deadline, or kill every group.

        The deadline is checked on every byte, so a process that keeps stdout
        readable cannot hold the parent past it.
        """
        line = b""
        while not line.endswith(b"\n"):
            remaining = self.remaining()
            if remaining <= 0 or len(line) >= MAX_LINE_BYTES:
                self.expire()
                return None
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                self.expire()
                return None
            chunk = os.read(process.stdout.fileno(), 1)
            if not chunk:
                return line.decode() or None
            line += chunk
        return line.decode()

    def wait(self, process):
        """Wait for one process within the wall deadline; on expiry kill every group."""
        try:
            stdout, stderr = process.communicate(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            self.expire()
            return None, "", ""
        return process.returncode, stdout.decode(), stderr.decode()

    def expire(self):
        self.timed_out = True
        self.kill()

    def kill(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in self.processes:
            process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def live_groups(self, settle_seconds=2.0):
        """Process groups with any member left; orphans get a moment to be reaped."""
        settle = time.monotonic() + settle_seconds
        while True:
            live = []
            for process in self.processes:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    pass
                live.append(process.pid)
            if not live or time.monotonic() >= settle:
                return live
            time.sleep(0.05)


class ProcessGroupsTests(unittest.TestCase):
    """The parent limit itself, without srt: it must not depend on the child's cooperation."""

    def groups(self, wall_seconds):
        groups = ProcessGroups(wall_seconds)
        self.addCleanup(groups.kill)
        return groups

    def python(self, groups, code):
        return groups.start([PYTHON, "-B", "-c", code])

    def test_endless_output_without_newline_cannot_pass_the_deadline(self):
        groups = self.groups(2)
        # Writes slower than the line cap fills, so only the deadline ends it.
        process = self.python(groups, "import sys, time\n"
                                      "while True:\n sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.01)")
        started = time.monotonic()
        self.assertIsNone(groups.read_line(process))
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(groups.timed_out)
        self.assertEqual(groups.live_groups(), [])

    def test_oversized_line_is_refused_before_the_deadline(self):
        groups = self.groups(60)
        process = self.python(groups, "import sys, time\n"
                                      f"sys.stdout.write('x' * {MAX_LINE_BYTES * 2}); sys.stdout.flush()\n"
                                      "time.sleep(3600)")
        started = time.monotonic()
        self.assertIsNone(groups.read_line(process))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(groups.live_groups(), [])

    def test_silent_group_and_its_grandchild_are_killed_at_the_deadline(self):
        groups = self.groups(2)
        process = self.python(groups, "import subprocess, sys, time\n"
                                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
                                      "time.sleep(3600)")
        self.assertEqual(groups.wait(process), (None, "", ""))
        self.assertTrue(groups.timed_out)
        self.assertEqual(groups.live_groups(), [])

    def test_no_process_starts_after_the_deadline(self):
        groups = self.groups(0.01)
        time.sleep(0.05)
        with self.assertRaises(TimeoutError):
            self.python(groups, "pass")
        self.assertEqual(groups.processes, [])


@unittest.skipUnless(PREFIX and sys.platform == "darwin",
                     "set OSS_RESEARCH_SRT_PREFIX to the pinned srt install (macOS)")
class IntegrationFixture(unittest.TestCase):
    wall_seconds = 120
    relay_timeout_seconds = "20"

    @classmethod
    def setUpClass(cls):
        cls.runtime = worker.verify_runtime(Path(PREFIX), os.environ["PATH"])

    def setUp(self):
        # Short path: srt's proxy sockets and macOS limits favour /private/tmp.
        self.root = Path(tempfile.mkdtemp(prefix="osri-", dir="/private/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tag = uuid.uuid4().hex
        self.input = self.root / "input"
        self.input.mkdir()
        shutil.copy(PROBE, self.input / "relay_probe.py")
        (self.input / "corpus.txt").write_text("public OSS notes\n")
        self.run_dir = self.root / "run"
        self.guard_log = self.root / "relay-guard.log"
        self.guard_log.write_text("")

        self.upstream = relay_tests.FixtureUpstream()
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.mock_port = self.upstream.server_address[1]
        # A second loopback listener the child must never reach.
        self.other = socket.socket()
        self.other.bind(("127.0.0.1", 0))
        self.other.listen(8)
        self.other.setblocking(False)
        self.addCleanup(self.other.close)
        self.other_port = self.other.getsockname()[1]

        self.groups = ProcessGroups(self.wall_seconds)
        self.addCleanup(self.groups.kill)

    def route(self, path, payload, **options):
        self.upstream.routes[path] = dict(status=200, body=relay_tests._json(payload), **options)

    def start_relay(self):
        home = self.root / "relay-home"
        home.mkdir()
        environment = {
            "PATH": os.environ["PATH"], "HOME": str(home), "PYTHONPATH": str(FIXTURES),
            "PYTHONDONTWRITEBYTECODE": "1", "GH_TOKEN": SENTINEL, "GITHUB_TOKEN": SENTINEL,
            "OSS_INTEGRATION_UPSTREAM_PORT": str(self.mock_port),
            "OSS_INTEGRATION_GUARD_LOG": str(self.guard_log),
        }
        self.relay = self.groups.start(
            [PYTHON, "-B", str(RELAY), "--repo", "octo/demo", "--repo", "octo/.github",
             "--request-budget", "20", "--max-response-bytes", "65536",
             "--response-byte-budget", "1048576",
             "--timeout-seconds", self.relay_timeout_seconds, "--skills-dir", str(SKILLS)],
            env=environment)
        line = self.groups.read_line(self.relay)
        self.assertIsNotNone(line, "relay printed no ready line before the wall deadline")
        ready = json.loads(line)
        self.assertEqual(ready["event"], "ready")
        self.relay_port = ready["port"]
        self.relay_authority = f"127.0.0.1:{self.relay_port}"
        return ready

    def policy(self):
        # The product policy for a non-AI relay probe: no model endpoint, the
        # relay port only. Only the interpreter read paths are added here.
        policy = worker.build_policy(self.input, self.run_dir, self.root / "no-claude",
                                     self.relay_port, model=False)
        self.assertEqual(policy["network"]["allowedDomains"], [self.relay_authority])
        policy["filesystem"]["allowRead"] += FIXTURE_READ
        return policy

    def start_child(self, steps, exit_code):
        policy_file = worker.prepare_run(self.run_dir, self.policy())
        self.output = self.run_dir / "output/probe-results.json"
        plan_file = self.input / f"plan-{self.tag}.json"
        plan_file.write_text(json.dumps({"steps": steps, "output": str(self.output),
                                         "exit_code": exit_code}))
        parent = {"PATH": os.environ["PATH"], "LANG": "en_US.UTF-8", "TERM": "dumb",
                  "GH_TOKEN": SENTINEL, "ORCA_TERMINAL_HANDLE": SENTINEL}
        command = worker.sandboxed(self.runtime, policy_file,
                                   [PYTHON, str(self.input / "relay_probe.py"), str(plan_file)])
        return self.groups.start(command, cwd=self.input,
                                 env=worker.build_environment(parent, self.run_dir))


class RelayChildTests(IntegrationFixture):
    def steps(self):
        relay = self.relay_authority

        def get(name, target, **extra):
            return dict(op="proxied", name=name, relay=relay, method="GET", target=target, **extra)

        def graphql(name, document):
            return dict(op="proxied", name=name, relay=relay, method="POST", target="/v1/graphql",
                        body=json.dumps(document))

        search = urllib.parse.urlencode({"q": "repo:octo/demo is:issue"})
        return [
            {"op": "read", "name": "input read", "path": str(self.input / "corpus.txt")},
            {"op": "write", "name": "output write", "text": "notes\n",
             "path": str(self.run_dir / "output/notes.md")},
            {"op": "write", "name": "input write", "text": "planted\n",
             "path": str(self.input / "planted.sh")},
            # Allowed reads: each one reaches the upstream exactly once.
            get("rest repository", "/v1/rest/repos/octo/demo"),
            get("rest list", "/v1/rest/repos/octo/demo/issues/7/comments?page=1"),
            get("rest search", "/v1/rest/search/issues?" + search),
            get("rest commit", f"/v1/rest/repos/octo/demo/commits/{SHA}"),
            get("contents at sha", f"/v1/rest/repos/octo/.github/contents/SECURITY.md?ref={SHA}"),
            graphql("graphql query", {"operation": "IssueEvidence",
                                      "variables": {"owner": "octo", "name": "demo", "number": 5}}),
            # Refused by the relay before any upstream call.
            dict(op="proxied", name="rest post", relay=relay, method="POST",
                 target="/v1/rest/repos/octo/demo/issues", body='{"title":"probe"}'),
            dict(op="proxied", name="rest put", relay=relay, method="PUT",
                 target="/v1/rest/repos/octo/demo/contents/README.md", body="{}"),
            dict(op="proxied", name="rest patch", relay=relay, method="PATCH",
                 target="/v1/rest/repos/octo/demo/issues/7", body="{}"),
            dict(op="proxied", name="rest delete", relay=relay, method="DELETE",
                 target="/v1/rest/repos/octo/demo/issues/7/comments"),
            graphql("graphql raw mutation",
                    {"query": "mutation { addComment(input:{}) { clientMutationId } }"}),
            graphql("graphql unknown operation", {"operation": "AddComment",
                                                  "variables": {"owner": "octo", "name": "demo",
                                                                "number": 5}}),
            get("other repository", "/v1/rest/repos/other/repo"),
            get("endpoint outside allowlist", "/v1/rest/repos/octo/demo/collaborators"),
            get("search without scope", "/v1/rest/search/issues?"
                + urllib.parse.urlencode({"q": "is:issue"})),
            get("search second scope", "/v1/rest/search/issues?"
                + urllib.parse.urlencode({"q": "repo:octo/demo repo:other/repo"})),
            # srt's URL parser resolves %2e%2e before forwarding; the tunnel
            # carries the encoded bytes to the relay unchanged.
            get("encoded path", f"/v1/rest/repos/octo/demo/contents/%2e%2e/secret?ref={SHA}"),
            {"op": "connect", "name": "tunnel encoded path", "authority": relay,
             "inner": {"method": "GET", "host": relay,
                       "target": f"/v1/rest/repos/octo/demo/contents/%2e%2e/secret?ref={SHA}"}},
            get("encoded slash", "/v1/rest/repos/octo%2Fdemo"),
            # srt rewrites Host to the destination authority, so a foreign Host
            # only reaches the relay unchanged through a CONNECT tunnel.
            get("srt host rewrite", "/v1/rest/repos/octo/demo/license", host="api.github.com"),
            {"op": "connect", "name": "tunnel foreign host", "authority": relay,
             "inner": {"method": "GET", "target": "/v1/rest/repos/octo/demo",
                       "host": "api.github.com"}},
            {"op": "connect", "name": "connect through relay", "authority": relay,
             "inner": {"method": "CONNECT", "target": "api.github.com:443", "host": relay}},
            # Refused by srt: no destination but the relay port.
            {"op": "connect", "name": "connect github", "authority": "api.github.com:443"},
            {"op": "connect", "name": "connect mock", "authority": f"127.0.0.1:{self.mock_port}"},
            dict(op="proxied", name="proxied github", relay="api.github.com", method="GET",
                 target="/repos/octo/demo"),
            dict(op="proxied", name="proxied mock", relay=f"127.0.0.1:{self.mock_port}",
                 method="GET", target="/repos/octo/demo"),
            dict(op="proxied", name="proxied other port", relay=f"127.0.0.1:{self.other_port}",
                 method="GET", target="/v1/health"),
            {"op": "tcp", "name": "direct relay", "host": "127.0.0.1", "port": self.relay_port},
            {"op": "tcp", "name": "direct mock", "host": "127.0.0.1", "port": self.mock_port},
            {"op": "tcp", "name": "direct other port", "host": "127.0.0.1",
             "port": self.other_port},
        ]

    def test_child_reaches_github_reads_only_through_the_relay(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"})
        self.route("/repos/octo/demo/issues/7/comments", [{"id": 1}])
        self.route("/search/issues", {"total_count": 1, "items": [
            {"number": 3, "repository_url": API + "/repos/octo/demo"}]})
        self.route(f"/repos/octo/demo/commits/{SHA}",
                   {"sha": SHA, "url": f"{API}/repos/octo/demo/commits/{SHA}"})
        self.route("/repos/octo/.github/contents/SECURITY.md", {
            "path": "SECURITY.md", "sha": "f" * 40,
            "url": f"{API}/repos/octo/.github/contents/SECURITY.md?ref={SHA}"})
        self.route("/graphql", {"data": {"repository": {"issue": None}}})
        self.route("/repos/octo/demo/license", {"url": f"{API}/repos/octo/demo/license"})

        ready = self.start_relay()
        self.assertEqual(
            {entry["role"]: (entry["path"], entry["sha256"]) for entry in ready["sources"]},
            {role: (str(path.resolve()), sha256(path)) for role, path in RELAY_SOURCES.items()})
        corpus = sha256(self.input / "corpus.txt")
        # Exit 5 is a deliberate non-zero code, so the collected exit code is
        # shown to be the child's own; it is not a model result of any kind.
        child = self.start_child(self.steps(), exit_code=5)
        exit_code, stdout, stderr = self.groups.wait(child)
        self.assertFalse(self.groups.timed_out, "child passed the parent wall deadline")
        self.assertEqual(self.relay.poll(), None, "relay stopped during the child run")

        # Collected results agree with what the child actually did.
        self.assertEqual(exit_code, 5, stderr)
        self.assertEqual(self.output.read_text(), stdout.strip())
        results = {item["name"]: item for item in json.loads(stdout)}
        self.assertEqual(results["input read"]["sha256"], corpus)
        self.assertTrue(results["output write"]["ok"], results["output write"])
        self.assertEqual((self.run_dir / "output/notes.md").read_text(), "notes\n")
        self.assertEqual(results["input write"]["errno"], "Operation not permitted")
        self.assertEqual(sorted(path.name for path in self.input.iterdir()),
                         sorted(["corpus.txt", "relay_probe.py", f"plan-{self.tag}.json"]))
        self.assertEqual(sha256(self.input / "corpus.txt"), corpus)
        self.assertEqual(sorted(path.name for path in (self.run_dir / "output").iterdir()),
                         ["notes.md", "probe-results.json"])
        self.assertEqual(worker.parent_path_problems(self.run_dir), [])
        self.assertEqual(worker.area_problems(self.run_dir), [])

        for name in ("rest repository", "rest list", "rest search", "rest commit",
                     "contents at sha", "graphql query", "srt host rewrite"):
            with self.subTest(allowed=name):
                self.assertEqual((results[name]["status"], results[name]["relay_ok"]), (200, True),
                                 results[name])
        refused = {
            "rest post": (405, "method_not_allowed"), "rest put": (405, "method_not_allowed"),
            "rest patch": (405, "method_not_allowed"), "rest delete": (405, "method_not_allowed"),
            "graphql raw mutation": (400, "bad_graphql"),
            "graphql unknown operation": (403, "operation_not_allowed"),
            "other repository": (403, "repository_not_allowed"),
            "endpoint outside allowlist": (403, "endpoint_not_allowed"),
            "search without scope": (403, "search_scope_not_allowed"),
            "search second scope": (403, "search_scope_not_allowed"),
            "encoded path": (403, "endpoint_not_allowed"), "encoded slash": (400, "bad_path"),
        }
        for name, (status, code) in refused.items():
            with self.subTest(refused=name):
                self.assertEqual((results[name]["status"], results[name]["relay_error"]),
                                 (status, code), results[name])
        for name, (status, code) in {"tunnel foreign host": (400, "bad_host"),
                                     "tunnel encoded path": (400, "bad_path"),
                                     "connect through relay": (405, "method_not_allowed")}.items():
            with self.subTest(tunnel=name):
                tunnel = results[name]
                self.assertEqual(tunnel["status"], 200, tunnel)
                self.assertEqual((tunnel["inner"]["status"], tunnel["inner"]["relay_error"]),
                                 (status, code), tunnel)
        for name in ("connect github", "connect mock", "proxied github", "proxied mock",
                     "proxied other port"):
            with self.subTest(srt=name):
                self.assertEqual((results[name]["status"], results[name]["proxy_error"]),
                                 (403, "blocked-by-allowlist"), results[name])
        for name in ("direct relay", "direct mock", "direct other port"):
            with self.subTest(direct=name):
                self.assertEqual(results[name]["errno"], "Operation not permitted", results[name])

        # Upstream counter: seven reads, one each, anonymous, nothing else.
        seen = [(request["method"], request["path"].partition("?")[0])
                for request in self.upstream.requests]
        self.assertEqual(seen, [
            ("GET", "/repos/octo/demo"), ("GET", "/repos/octo/demo/issues/7/comments"),
            ("GET", "/search/issues"), ("GET", f"/repos/octo/demo/commits/{SHA}"),
            ("GET", "/repos/octo/.github/contents/SECURITY.md"), ("POST", "/graphql"),
            ("GET", "/repos/octo/demo/license")])
        self.assertIn(f"ref={SHA}", self.upstream.requests[4]["path"])
        self.assertIn(b"query IssueEvidence", self.upstream.requests[5]["body"])
        for request in self.upstream.requests:
            self.assertEqual(request["headers"]["host"], "api.github.com")
            self.assertNotIn("authorization", request["headers"])
            self.assertNotIn(SENTINEL, json.dumps(request["headers"]))
        with self.assertRaises(BlockingIOError):
            self.other.accept()
        self.assertEqual(self.guard_log.read_text(), "")

        # The product collector records the probe as a failed model result, never success.
        self.assertFalse(worker.collect(self.run_dir, "claude-profile2", exit_code, stdout, stderr))
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("stdout is not a result object", manifest["problems"])
        self.assertFalse((self.run_dir / "result/report.md").exists())

        self.relay.send_signal(signal.SIGTERM)
        self.assertEqual(self.groups.wait(self.relay)[0], 0)
        self.assertEqual(self.groups.live_groups(), [])


class WallClockTests(IntegrationFixture):
    """Harness regression only; the product deadline is RealSrtSupervisorTests in the #189 suite."""

    wall_seconds = 12
    # Longer than the wall limit, so only the parent can end this run in time.
    relay_timeout_seconds = "300"

    def test_parent_wall_limit_kills_relay_and_child_groups_on_a_silent_upstream(self):
        self.route("/repos/octo/demo", {"full_name": "octo/demo"}, delay=3600)
        self.start_relay()
        child = self.start_child([
            {"op": "proxied", "name": "hang", "relay": self.relay_authority, "method": "GET",
             "target": "/v1/rest/repos/octo/demo", "timeout": 3600},
        ], exit_code=0)
        started = time.monotonic()
        exit_code, _, _ = self.groups.wait(child)
        elapsed = time.monotonic() - started

        self.assertTrue(self.groups.timed_out)
        self.assertIsNone(exit_code)
        self.assertLess(elapsed, self.wall_seconds + 5)
        self.assertEqual(self.relay.returncode, -signal.SIGKILL)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(self.groups.live_groups(), [])
        leftovers = subprocess.run(["/usr/bin/pgrep", "-f", f"plan-{self.tag}.json"],
                                   capture_output=True, text=True)
        self.assertEqual(leftovers.stdout, "")
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", self.relay_port), timeout=2).close()
        # The request did reach the silent upstream: the hang was real, not a refusal.
        self.assertEqual([request["path"].partition("?")[0] for request in self.upstream.requests],
                         ["/repos/octo/demo"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.guard_log.read_text(), "")


@unittest.skipUnless(PREFIX and sys.platform == "darwin",
                     "set OSS_RESEARCH_SRT_PREFIX to the pinned srt install (macOS)")
class ClosedGateTests(unittest.TestCase):
    def test_product_launcher_verifies_srt_but_starts_no_model(self):
        root = Path(tempfile.mkdtemp(prefix="osri-", dir="/private/tmp")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "input").mkdir()
        (root / "input/task.txt").write_text("Summarize the corpus.")
        (root / "home").mkdir()
        result = subprocess.run(
            [PYTHON, "-B", str(WORKER), "--profile", "claude-profile2",
             "--input-dir", str(root / "input"), "--prompt-file", str(root / "input/task.txt"),
             "--run-dir", str(root / "run"), "--runtime-prefix", PREFIX],
            env={"PATH": os.environ["PATH"], "HOME": str(root / "home")},
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn(f"srt {worker.SRT_VERSION} verified", result.stderr)
        self.assertIn("no credential binding for claude-profile2", result.stderr)
        self.assertFalse((root / "run").exists())


if __name__ == "__main__":
    unittest.main()
