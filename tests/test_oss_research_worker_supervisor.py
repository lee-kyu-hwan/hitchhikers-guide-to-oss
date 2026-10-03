"""Check the OSS worker's relay supervisor: one deadline, bounded output, exact failures.

Offline classes drive run_supervised with a pass-through fake srt, a fake
claude and a synthetic relay (python -c). The fakes enforce nothing, so they
prove the supervisor's contract, never a sandbox boundary. RelayReadyTests
starts the real relay without sending it a request: no upstream is contacted.

RealSrtSupervisorTests (only with OSS_RESEARCH_SRT_PREFIX, macOS or Linux) runs the real
relay against a 127.0.0.1 fixture upstream and a non-AI probe inside the real
srt 0.0.77. No model, no GitHub host, no credential. The launch gate stays
closed throughout; ClosedGateTests checks it with relay arguments too.

The setsid case differs by platform. On macOS the escaped descendant is
measured to survive (the documented limit). On Linux it is expected to die
with srt's PID namespace, measured on the Debian 13 guest (2026-09-28). The pid it reports is a
namespace pid, so the Linux test finds it by a unique argv string in the
host's /proc (processes_holding), with a live positive control first.
"""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "dot_local/bin/executable_oss-research-worker"
FIXTURES = REPO / "tests/fixtures/oss_research_worker_supervisor"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


worker = load("oss_research_worker", SCRIPT)
# The model endpoint as the platform's policy spells it (Linux adds :443).
MODEL_DOMAINS = list(worker.ALLOWED_DOMAINS_LINUX if sys.platform == "linux"
                     else worker.ALLOWED_DOMAINS)
linux_tests = load("oss_research_worker_linux_tests", REPO / "tests/test_oss_research_worker_linux.py")
PYTHON = str(Path(sys.executable).resolve())
LINUX = sys.platform == "linux"
SHORT_TMP = "/tmp" if LINUX else "/private/tmp"
BASE_PREFIX = str(Path(sys.base_prefix).resolve())
# Linux: the system python's library is under /usr/lib (SYSTEM_READ_LINUX).
FIXTURE_READ = ([PYTHON] + ([] if BASE_PREFIX.startswith("/usr") else [BASE_PREFIX])
                if LINUX else [BASE_PREFIX, "/opt/homebrew"])
PREFIX = os.environ.get("OSS_RESEARCH_SRT_PREFIX")
SOURCES = [{"role": "issues", "path": "/fixture/issues.py", "sha256": "a" * 64},
           {"role": "sibling", "path": "/fixture/sibling.py", "sha256": "b" * 64}]
FAKE_PORT = 40999


def ready_line(port=FAKE_PORT, sources=SOURCES, **overrides):
    ready = {"event": "ready", "host": "127.0.0.1", "port": port,
             "base_url": f"http://127.0.0.1:{port}", "sources": sources}
    ready.update(overrides)
    return json.dumps(ready)


def result_json(text="# Summary\n"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "permission_denials": [], "result": text})


def alive(pid):
    state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                           capture_output=True, text=True).stdout.strip()
    return state not in ("", "Z")


def write_executable(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osrs-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.started = self.root / "child-started"
        self.pids = self.root / "pids"
        self.pids.mkdir()
        write_executable(self.bin / "srt", (
            "import os,sys\n"
            "arguments=sys.argv[1:]\n"
            "assert arguments[0]=='--settings' and arguments[2]=='--'\n"
            "os.execv(arguments[3],arguments[3:])\n"
        ))

    def relay(self, code, sources=SOURCES):
        preamble = (f"import json,os,sys,time\n"
                    f"open({str(self.pids / 'relay')!r},'w').write(str(os.getpid()))\n")
        return worker.RelayLaunch([PYTHON, "-B", "-c", preamble + code],
                                  {"PATH": "/usr/bin:/bin"}, sources, "0" * 64)

    def relay_ready(self, after="time.sleep(60)\n", line=None, stderr=""):
        line = ready_line() if line is None else line
        return self.relay(f"sys.stderr.write({stderr!r});sys.stderr.flush()\n"
                          f"sys.stdout.write({line + chr(10)!r});sys.stdout.flush()\n" + after)

    def supervise(self, body="", stdout=None, relay=None, seconds=20):
        stdout = result_json() if stdout is None else stdout
        write_executable(self.bin / "claude", (
            "import os,subprocess,sys,time\n"
            f"open({str(self.started)!r},'w').write(str(os.getpid()))\n" + body +
            f"sys.stdout.write({stdout!r})\n"
        ))
        claude = self.bin / "claude"
        began = time.monotonic()
        succeeded = worker.run_supervised(
            self.input, self.run_dir, "claude-profile1", seconds, relay,
            lambda port: worker.build_policy(self.input, self.run_dir, claude, port),
            lambda policy: worker.build_command([str(self.bin / "srt")], policy,
                                                str(claude), "prompt"),
            worker.build_environment({"PATH": "/usr/bin:/bin"}, self.run_dir))
        self.elapsed = time.monotonic() - began
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        policy = json.loads((self.run_dir / "policy/srt-settings.json").read_text())
        return succeeded, manifest, policy

    def assert_failed(self, succeeded, manifest, failure):
        self.assertFalse(succeeded)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn(failure, manifest["failures"])
        self.assertIsNone(manifest["exit_code"])
        self.assertFalse((self.run_dir / "result/report.md").exists())

    def assert_groups_gone(self):
        for pid_file in self.pids.iterdir():
            self.assertFalse(alive(int(pid_file.read_text())), pid_file.name)
        if self.started.exists():
            self.assertFalse(alive(int(self.started.read_text())), "child")


class RelayRunTests(Fixture):
    def test_ready_relay_adds_only_its_port_and_the_run_succeeds(self):
        succeeded, manifest, policy = self.supervise(
            relay=self.relay_ready(stderr="relay log line\n"))
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(manifest["relay"],
                         {"sources": SOURCES, "relay_sha256": "0" * 64, "port": FAKE_PORT})
        self.assertEqual(policy["network"]["allowedDomains"],
                         [*MODEL_DOMAINS, f"127.0.0.1:{FAKE_PORT}"])
        self.assertIs(policy["network"]["allowLocalBinding"], False)
        self.assertEqual((self.run_dir / "result/relay.log").read_text(), "relay log line\n")
        self.assert_groups_gone()

    def test_relay_that_exits_before_ready_starts_no_child(self):
        succeeded, manifest, policy = self.supervise(relay=self.relay("sys.exit(3)\n"))
        self.assert_failed(succeeded, manifest, "relay-exited")
        self.assertFalse(self.started.exists())
        self.assertEqual(policy["network"]["allowedDomains"], MODEL_DOMAINS)
        self.assertIsNone(manifest["relay"]["port"])

    def test_silent_relay_times_out_at_the_one_deadline(self):
        succeeded, manifest, _ = self.supervise(relay=self.relay("time.sleep(60)\n"), seconds=2)
        self.assert_failed(succeeded, manifest, "timeout")
        self.assertLess(self.elapsed, 2 + worker.CLEANUP_SECONDS)
        self.assertFalse(self.started.exists())
        self.assert_groups_gone()

    def test_endless_ready_without_newline_is_refused(self):
        code = "sys.stdout.write('x'*20000);sys.stdout.flush();time.sleep(60)\n"
        succeeded, manifest, _ = self.supervise(relay=self.relay(code))
        self.assert_failed(succeeded, manifest, "relay-ready-too-large")
        self.assertLess(self.elapsed, 10)
        self.assert_groups_gone()

    def test_ready_bound_counts_the_newline(self):
        at_limit = "x" * (worker.MAX_READY_BYTES - 1)
        succeeded, manifest, _ = self.supervise(relay=self.relay_ready(line=at_limit))
        self.assertFalse(succeeded)
        self.assertTrue(any(item.startswith("relay-not-ready") for item in manifest["failures"]),
                        manifest["failures"])
        self.run_dir = self.root / "run-over"
        over = "x" * worker.MAX_READY_BYTES
        self.assert_failed(*self.supervise(relay=self.relay_ready(line=over))[:2],
                           "relay-ready-too-large")

    def test_relay_stderr_flood_before_ready_is_an_output_failure(self):
        code = "sys.stderr.write('e'*(2*1024*1024));sys.stderr.flush();time.sleep(60)\n"
        succeeded, manifest, _ = self.supervise(relay=self.relay(code))
        self.assert_failed(succeeded, manifest, "output-too-large: relay stderr")
        self.assertLess(self.elapsed, 10)

    def test_ready_line_with_other_sources_is_refused(self):
        other = [dict(SOURCES[0], sha256="c" * 64), SOURCES[1]]
        succeeded, manifest, policy = self.supervise(
            relay=self.relay_ready(line=ready_line(sources=other)))
        self.assertFalse(succeeded)
        self.assertEqual(manifest["failures"],
                         ["relay-not-ready: relay sources differ from the parent-hashed skills"])
        self.assertFalse(self.started.exists())
        self.assertEqual(policy["network"]["allowedDomains"], MODEL_DOMAINS)

    def test_relay_that_exits_while_the_child_runs_fails_the_run(self):
        succeeded, manifest, _ = self.supervise(
            body="time.sleep(3)\n", relay=self.relay_ready(after="time.sleep(0.3)\n"))
        self.assert_failed(succeeded, manifest, "relay-exited")
        self.assertLess(self.elapsed, 3)
        self.assert_groups_gone()

    def test_relay_that_exits_with_the_child_still_fails(self):
        # Both finish at about the same time; the relay must outlive the child.
        succeeded, manifest, _ = self.supervise(
            body="time.sleep(0.5)\n", relay=self.relay_ready(after="time.sleep(0.5)\n"))
        self.assert_failed(succeeded, manifest, "relay-exited")

    def test_relay_that_cannot_start_is_recorded(self):
        relay = worker.RelayLaunch([str(self.root / "no-relay")], {}, SOURCES, "0" * 64)
        succeeded, manifest, _ = self.supervise(relay=relay)
        self.assert_failed(succeeded, manifest, "relay-not-started: FileNotFoundError")
        self.assertFalse(self.started.exists())

    def test_timeout_kills_the_relay_and_the_child_groups(self):
        body = (f"grandchild=subprocess.Popen(['sleep','60'])\n"
                f"open({str(self.pids / 'grandchild')!r},'w').write(str(grandchild.pid))\n"
                "time.sleep(60)\n")
        succeeded, manifest, _ = self.supervise(body=body, relay=self.relay_ready(), seconds=2)
        self.assert_failed(succeeded, manifest, "timeout")
        self.assertLess(self.elapsed, 2 + worker.CLEANUP_SECONDS)
        self.assert_groups_gone()


class ChildOutputTests(Fixture):
    def test_stdout_overflow_is_not_collected_even_if_the_prefix_parses(self):
        # The kept prefix is a valid result followed by spaces: it would parse.
        padded = result_json() + " " * worker.MAX_STDOUT_BYTES
        succeeded, manifest, _ = self.supervise(stdout=padded)
        self.assert_failed(succeeded, manifest, "output-too-large: stdout")
        kept = (self.run_dir / "result/stdout.json").read_text()
        self.assertEqual(len(kept), worker.MAX_STDOUT_BYTES)
        self.assertEqual(json.loads(kept)["type"], "result")

    def test_stderr_overflow_fails(self):
        body = f"sys.stderr.write('e'*{worker.MAX_STDERR_BYTES + 1});sys.stderr.flush()\n"
        self.assert_failed(*self.supervise(body=body)[:2], "output-too-large: stderr")

    def test_grandchild_holding_the_pipes_is_bounded_and_killed(self):
        body = (f"grandchild=subprocess.Popen(['sleep','60'])\n"
                f"open({str(self.pids / 'grandchild')!r},'w').write(str(grandchild.pid))\n")
        succeeded, manifest, _ = self.supervise(body=body)
        self.assert_failed(succeeded, manifest, "child-pipes-held-open")
        self.assertLess(self.elapsed, worker.EXIT_GRACE_SECONDS + worker.CLEANUP_SECONDS + 3)
        self.assert_groups_gone()

    def test_setsid_escape_is_bounded_but_not_killed(self):
        # The documented limit: a descendant in a new session is out of every
        # group the supervisor kills. The run still fails within its bounds.
        escaped = self.pids / "escaped"
        body = ("pid=os.fork()\n"
                "if pid==0:\n"
                " os.setsid()\n"
                f" open({str(escaped)!r},'w').write(str(os.getpid()))\n"
                " time.sleep(60)\n"
                " os._exit(0)\n"
                "time.sleep(0.3)\n")
        succeeded, manifest, _ = self.supervise(body=body)
        pid = int(escaped.read_text())
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)
        self.assert_failed(succeeded, manifest, "child-pipes-held-open")
        self.assertLess(self.elapsed, worker.EXIT_GRACE_SECONDS + worker.CLEANUP_SECONDS + 3)
        self.assertTrue(alive(pid), "the escaped process is expected to survive cleanup")
        escaped.unlink()
        self.assert_groups_gone()

    def test_child_that_cannot_start_is_recorded(self):
        (self.bin / "srt").unlink()
        succeeded, manifest, _ = self.supervise()
        self.assert_failed(succeeded, manifest, "child-not-started: FileNotFoundError")

    def test_passed_deadline_starts_nothing_and_still_records(self):
        succeeded, manifest, _ = self.supervise(relay=self.relay_ready(), seconds=1e-9)
        self.assert_failed(succeeded, manifest, "timeout")
        self.assertFalse(self.started.exists())
        self.assertEqual(list(self.pids.iterdir()), [])

    def test_launch_keeps_its_tuple_and_bounds(self):
        write_executable(self.bin / "noisy", f"import sys\nsys.stdout.write('x'*{worker.MAX_STDOUT_BYTES + 1})\n")
        exit_code, stdout, stderr = worker.launch([str(self.bin / "noisy")], {"PATH": "/usr/bin:/bin"},
                                                  self.input, 10)
        self.assertIsNone(exit_code)
        self.assertEqual(len(stdout), worker.MAX_STDOUT_BYTES)
        write_executable(self.bin / "quiet", "import sys\nsys.stdout.write('ok');sys.exit(5)\n")
        self.assertEqual(worker.launch([str(self.bin / "quiet")], {"PATH": "/usr/bin:/bin"},
                                       self.input, 10), (5, "ok", ""))


class ReadyLineTests(unittest.TestCase):
    def refused(self, line, reason):
        with self.assertRaises(ValueError) as caught:
            worker.checked_ready(line.encode(), SOURCES)
        self.assertIn(reason, str(caught.exception))

    def test_exact_ready_line_gives_the_port(self):
        self.assertEqual(worker.checked_ready((ready_line() + "\n").encode(), SOURCES), FAKE_PORT)

    def test_malformed_ready_lines_are_refused(self):
        self.refused("{", "not JSON")
        self.refused("\xff", "not JSON")
        self.refused(ready_line()[:-1] + ', "port": 1}', "not JSON")
        self.refused(ready_line(extra=1), "exactly the ready fields")
        self.refused(ready_line(event="started"), "ready event")
        self.refused(ready_line(host="0.0.0.0"), "ready event")
        self.refused(ready_line(port=True, base_url="http://127.0.0.1:True"), "TCP port")
        self.refused(ready_line(port=0, base_url="http://127.0.0.1:0"), "TCP port")
        self.refused(ready_line(port=70000, base_url="http://127.0.0.1:70000"), "TCP port")
        self.refused(ready_line(base_url="http://127.0.0.1:1"), "base_url")
        self.refused(ready_line(sources=SOURCES[:1]), "sources differ")


class RelayRequestTests(unittest.TestCase):
    def checked(self, repositories=("octo/demo",), request=10, largest=1024, total=4096,
                seconds=5.0):
        return worker.checked_relay_request(list(repositories), request, largest, total, seconds)

    def test_valid_request(self):
        self.assertEqual(self.checked(("octo/demo", "octo/.github")).repositories,
                         ("octo/demo", "octo/.github"))

    def test_invalid_requests(self):
        for arguments in ({"repositories": ()}, {"repositories": ("octo",)},
                          {"repositories": ("octo/demo", "OCTO/demo")},
                          {"repositories": ("../x/y",)},
                          {"repositories": [f"o/r{i}" for i in range(21)]},
                          {"request": 0}, {"request": 10_001}, {"request": True},
                          {"largest": 64 * 1024 * 1024 + 1}, {"total": 0},
                          {"seconds": 0.0}, {"seconds": 301.0}, {"seconds": float("nan")},
                          {"seconds": float("inf")}):
            with self.subTest(arguments), self.assertRaises(ValueError):
                self.checked(**arguments)


class RelayReadyTests(unittest.TestCase):
    """The real relay's ready line against the parent's own source hashes (no request)."""

    def test_trusted_relay_is_the_sibling_and_its_ready_line_checks_out(self):
        relay, skills = worker.trusted_relay(SCRIPT)
        self.assertEqual(relay, REPO / "dot_local/bin/executable_oss-research-relay")
        self.assertEqual(skills, REPO / "dot_codex/skills")
        request = worker.checked_relay_request(["octo/demo"], 5, 4096, 65536, 5.0)
        launch = worker.relay_launch(request, {"PATH": "/usr/bin:/bin", "GH_TOKEN": "sentinel"})
        self.assertEqual(launch.argv[:3], [PYTHON, "-B", str(relay)])
        self.assertEqual(launch.argv[-2:], ["--skills-dir", str(skills)])
        self.assertNotIn("GH_TOKEN", launch.environment)
        supervisor = worker.Supervisor(20)
        try:
            process = supervisor.start(launch.argv, launch.environment, REPO)
            line = supervisor.read_ready(process)
        finally:
            supervisor.cleanup()
        self.assertEqual(supervisor.failures, [])
        port = worker.checked_ready(line, launch.sources)
        self.assertTrue(1 <= port <= 65535)

    def test_hanging_resolver_does_not_hold_back_the_ready_line(self):
        # macOS CI showed ~35 s relay startups, consistent with a stalled reverse
        # lookup in HTTPServer.server_bind; this fixture models such a resolver.
        # The relay must reach ready within Supervisor(20) without one.
        request = worker.checked_relay_request(["octo/demo"], 5, 4096, 65536, 5.0)
        launch = worker.relay_launch(request, {"PATH": "/usr/bin:/bin"})
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "sitecustomize.py").write_text(
                "import socket, time\n"
                "def hang(*args):\n"
                "    time.sleep(60)\n"
                "socket.getfqdn = socket.gethostbyaddr = hang\n")
            supervisor = worker.Supervisor(20)
            try:
                process = supervisor.start(launch.argv, dict(launch.environment, PYTHONPATH=directory),
                                           REPO)
                line = supervisor.read_ready(process)
            finally:
                supervisor.cleanup()
        self.assertEqual(supervisor.failures, [])
        self.assertTrue(1 <= worker.checked_ready(line, launch.sources) <= 65535)

    def test_missing_sibling_relay_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            lone = Path(directory) / "oss-research-worker"
            lone.write_text("")
            with self.assertRaises(worker.Blocked):
                worker.trusted_relay(lone)


class ClosedGateTests(unittest.TestCase):
    """The gate stays first: relay arguments change nothing before it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osrs-gate-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "input").mkdir()
        (self.root / "input/task.txt").write_text("summarize")
        self.run_dir = self.root / "run"

    def arguments(self, *extra):
        return ["--profile", "claude-profile1", "--input-dir", str(self.root / "input"),
                "--prompt-file", str(self.root / "input/task.txt"),
                "--run-dir", str(self.run_dir), *extra]

    def cli(self, *extra):
        return subprocess.run([PYTHON, "-B", str(SCRIPT), *self.arguments(*extra)],
                              env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)},
                              capture_output=True, text=True, timeout=60)

    def test_relay_arguments_are_checked_before_the_gate(self):
        for extra in (("--relay-repo", "octo"), ("--relay-repo", "octo/demo",
                                                 "--relay-request-budget", "0"),
                      ("--relay-repo", "a/b", "--relay-repo", "A/b")):
            with self.subTest(extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, worker.EXIT_BAD_INPUT, result.stderr)
                self.assertFalse(self.run_dir.exists())

    def test_gate_blocks_before_any_relay_process_or_directory(self):
        result = self.cli("--relay-repo", "octo/demo")
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn("blocked", result.stderr)
        self.assertFalse(self.run_dir.exists())
        with mock.patch.object(worker, "relay_launch") as relay_launch, \
                mock.patch.object(worker.subprocess, "Popen") as popen, \
                mock.patch.object(sys, "argv", ["worker", *self.arguments("--relay-repo", "o/r")]):
            self.assertEqual(worker.main(), worker.EXIT_BLOCKED)
        relay_launch.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.run_dir.exists())


@unittest.skipUnless(PREFIX and sys.platform in ("darwin", "linux"),
                     "set OSS_RESEARCH_SRT_PREFIX to the pinned srt install (macOS, Linux)")
class RealSrtSupervisorTests(unittest.TestCase):
    """Product supervisor + real relay + real srt + non-AI probe; 127.0.0.1 only."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = worker.verify_runtime(Path(PREFIX), os.environ["PATH"])
        relay_tests = load("oss_research_relay_tests", REPO / "tests/test_oss_research_relay.py")
        cls.upstream = relay_tests.FixtureUpstream()
        cls.upstream.routes["/repos/octo/demo"] = {"status": 200, "body": b'{"full_name":"octo/demo"}'}
        cls.upstream.routes["/repos/octo/hang"] = {"status": 200, "delay": 120,
                                                   "body": b'{"full_name":"octo/hang"}'}
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        # Unix socket paths must stay under 104 bytes, so not the scratchpad.
        self.root = Path(tempfile.mkdtemp(prefix="osrs-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        shutil.copy(FIXTURES / "relay_probe.py", self.input / "relay_probe.py")
        self.run_dir = self.root / "run"
        self.upstream.requests.clear()

    def supervise(self, probe_arguments, seconds):
        request = worker.checked_relay_request(["octo/demo", "octo/hang"], 20, 65536, 1048576, 300.0)
        launch = worker.relay_launch(request, dict(os.environ))
        environment = dict(launch.environment, PYTHONPATH=str(FIXTURES),
                           OSS_SUPERVISOR_UPSTREAM_PORT=str(self.upstream.server_address[1]),
                           GH_TOKEN="gho_supervisor_parent_sentinel")
        launch = launch._replace(environment=environment)
        read = FIXTURE_READ
        # The probe needs the relay port, known only once ready checked out.
        ports = {}

        def policy_for(port):
            ports["relay"] = port
            policy = worker.build_policy(self.input, self.run_dir, self.root / "no-claude",
                                         port, model=False, runtime=self.runtime)
            policy["filesystem"]["allowRead"] += read
            return policy

        def command_for(policy_file):
            return worker.sandboxed(self.runtime, policy_file,
                                    [PYTHON, "-B", str(self.input / "relay_probe.py"),
                                     *probe_arguments(ports)])

        parent = {"PATH": os.environ["PATH"], "GH_TOKEN": "gho_supervisor_parent_sentinel",
                  "ORCA_TERMINAL_HANDLE": "sentinel"}
        began = time.monotonic()
        succeeded = worker.run_supervised(self.input, self.run_dir, "claude-profile1", seconds,
                                          launch, policy_for, command_for,
                                          worker.build_environment(parent, self.run_dir))
        self.elapsed = time.monotonic() - began
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        policy = json.loads((self.run_dir / "policy/srt-settings.json").read_text())
        return succeeded, manifest, policy

    def test_relay_read_through_srt_and_the_rest_refused(self):
        mock_port = self.upstream.server_address[1]
        succeeded, manifest, policy = self.supervise(
            lambda ports: ["read", str(ports["relay"]), str(mock_port)], seconds=60)
        self.assertTrue(succeeded, manifest)
        port = manifest["relay"]["port"]
        self.assertEqual(policy["network"]["allowedDomains"], [f"127.0.0.1:{port}"])
        self.assertIs(policy["network"]["allowLocalBinding"], False)
        observed = json.loads((self.run_dir / "result/report.md").read_text())
        self.assertEqual(observed["get"], [200, "octo/demo"])
        self.assertEqual(observed["post"], 405)
        # macOS refuses a direct connect with EPERM. Linux gives the probe its own
        # network namespace, so the connect is refused there; the control is the
        # same relay answering 200 through the proxy above and the upstream
        # seeing only that one GET below.
        direct = ("ConnectionRefusedError", "PermissionError") if LINUX else ("PermissionError",)
        self.assertTrue(observed["direct_relay"].startswith(direct), observed)
        self.assertEqual(observed["proxy_mock"], 403)
        self.assertTrue(observed["direct_mock"].startswith(direct), observed)
        self.assertEqual([(r["method"], r["path"].partition("?")[0]) for r in self.upstream.requests],
                         [("GET", "/repos/octo/demo")])
        for seen in self.upstream.requests:
            self.assertNotIn("authorization", seen["headers"])
            self.assertNotIn("gho_supervisor_parent_sentinel", json.dumps(seen["headers"]))

    def test_one_deadline_ends_a_hung_relay_request_and_both_groups(self):
        succeeded, manifest, _ = self.supervise(
            lambda ports: ["hang", str(ports["relay"])], seconds=8)
        self.assertFalse(succeeded)
        self.assertEqual(manifest["failures"], ["timeout"])
        self.assertLess(self.elapsed, 8 + worker.CLEANUP_SECONDS)
        self.assertEqual([r["path"].partition("?")[0] for r in self.upstream.requests],
                         ["/repos/octo/hang"])
        self.assertFalse((self.run_dir / "result/report.md").exists())

    @unittest.skipIf(LINUX, "macOS measurement; Linux is the PID namespace test below")
    def test_setsid_inside_srt_is_bounded_but_not_killed(self):
        # Measures the documented limit under the real srt: the probe's forked
        # descendant calls setsid and keeps the child's stdout open.
        marker = self.run_dir / "output/escaped.json"
        succeeded, manifest, _ = self.supervise(
            lambda ports: ["escape", str(ports["relay"]), str(marker)], seconds=30)
        escaped = json.loads(marker.read_text())
        pid = escaped["pid"]
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)
        self.assertFalse(succeeded)
        self.assertEqual(manifest["failures"], ["child-pipes-held-open"])
        self.assertLess(self.elapsed, worker.EXIT_GRACE_SECONDS + worker.CLEANUP_SECONDS + 5)
        self.assertEqual(escaped["outcome"], "setsid")
        self.assertTrue(alive(pid), "srt did not stop setsid; the escaped process survives")

    @unittest.skipUnless(LINUX, "Linux PID namespace (measured on the Debian guest)")
    def test_setsid_inside_srt_dies_with_the_pid_namespace(self):
        marker = self.run_dir / "output/escaped.json"
        needle = str(marker).encode()
        # Positive control: the scan sees a live process carrying a unique string.
        control_tag = f"osrs-control-{os.getpid()}-{time.monotonic_ns()}"
        control = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)", control_tag])
        self.addCleanup(control.wait)
        self.addCleanup(control.kill)
        self.assertEqual(linux_tests.processes_holding(control_tag.encode()), [control.pid])
        self.assertEqual(linux_tests.processes_holding(needle), [])

        def kill_leftovers():
            for pid in linux_tests.processes_holding(needle):
                os.kill(pid, signal.SIGKILL)
        self.addCleanup(kill_leftovers)
        succeeded, manifest, _ = self.supervise(
            lambda ports: ["escape", str(ports["relay"]), str(marker)], seconds=30)
        escaped = json.loads(marker.read_text())
        self.assertEqual(escaped["outcome"], "setsid")
        self.assertFalse(succeeded)
        self.assertLess(self.elapsed, worker.EXIT_GRACE_SECONDS + worker.CLEANUP_SECONDS + 5)
        self.assertTrue(set(manifest["failures"]) <= {"child-pipes-held-open"}, manifest)
        # escaped["pid"] is a namespace pid: never checked with os.kill here.
        survivors = linux_tests.processes_holding(needle)
        until = time.monotonic() + worker.CLEANUP_SECONDS
        while survivors and time.monotonic() < until:
            time.sleep(0.1)
            survivors = linux_tests.processes_holding(needle)
        self.assertEqual(survivors, [], "a setsid descendant outlived srt's PID namespace")


if __name__ == "__main__":
    unittest.main()
