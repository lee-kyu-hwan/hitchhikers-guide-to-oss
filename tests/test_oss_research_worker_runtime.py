"""Observe the OSS worker's srt policy with the real sandbox runtime (macOS, Linux).

Run explicitly against the pinned local install (never a global one):

    OSS_RESEARCH_SRT_PREFIX=/path/to/srt-0.0.77 \
      python3 -B -m unittest tests.test_oss_research_worker_runtime -v

OSS_RESEARCH_NPM_CACHE (default <prefix>/.npm-cache) lets the install test run
npm ci --offline from the committed lock fixture.

The sandboxed program is tests/fixtures/oss_research_worker/probe.py, not
Claude: no model, no OSS host and no real credential is involved. The only
Claude run is ClaudeStartupTests: --version and --help, no network, no
credential, a fresh HOME. Every policy
here is the runner's build_policy plus the read paths the fixture's own
interpreter and git need (FIXTURE_READ). A test named ..._not_blocked records a
gap the runtime leaves open; it passes when the gap is observed as documented.
Without the variable every test is skipped, which is not evidence of anything.

On Linux (bubblewrap) the expectations were measured on the Debian 13 guest
(2026-09-28, docs/research/2026-09-28-189-linux-isolation-evidence.md). A denial is accepted by
errno only beside a host-side control that the same path or port is reachable
outside the sandbox, and the macOS-only cases (Keychain, ICU crash, local
binding) are skipped there.
"""

import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin/oss-research-worker"
PROBE = REPO / "tests/fixtures/oss_research_worker/probe.py"
LOADER = importlib.machinery.SourceFileLoader("oss_research_worker", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
worker = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(worker)

PREFIX = os.environ.get("OSS_RESEARCH_SRT_PREFIX")
PYTHON = str(Path(sys.executable).resolve())
LINUX = sys.platform == "linux"
BASE_PREFIX = str(Path(sys.base_prefix).resolve())
if LINUX:
    SHORT_TMP = "/tmp"
    GIT = shutil.which("git", path="/usr/bin")
    # The system python's library is under /usr/lib (SYSTEM_READ_LINUX); a
    # python elsewhere brings its prefix. Unmeasured.
    FIXTURE_READ = [PYTHON] + ([] if BASE_PREFIX.startswith("/usr") else [BASE_PREFIX])
    OPENSSL_CONFIG = "/etc/ssl/openssl.cnf"
else:
    SHORT_TMP = "/private/tmp"
    GIT = shutil.which("git", path="/opt/homebrew/bin")
    FIXTURE_READ = [BASE_PREFIX, "/opt/homebrew"]
    OPENSSL_CONFIG = "/private/etc/ssl/openssl.cnf"
# /bin/echo is not in the Linux read set; bash (which is) prints instead.
ECHO = ["/usr/bin/bash", "-c", "echo RAN"] if LINUX else ["/bin/echo", "RAN"]
# Linux denial errnos (measured on the Debian guest): bubblewrap refuses with a read-only mount,
# a pinned directory, seccomp's EPERM or a tmpfs that hides the path. ENOENT
# counts only when the host shows the path (or a write's parent) exists.
# EACCES is never counted: plain file permissions give it outside srt too.
LINUX_DENIED = {"Operation not permitted", "Read-only file system", "Device or resource busy",
                # A rename across srt's bind mounts (measured on the Debian guest).
                "Invalid cross-device link"}
LINUX_HIDDEN = "No such file or directory"
SECURITY = "/usr/bin/security"
# /usr/bin/security links system frameworks the runner's policy keeps closed.
SECURITY_READ = [SECURITY, "/System/Library/Frameworks", "/System/Library/PrivateFrameworks"]
LOCK_FIXTURE = REPO / "tests/fixtures/oss_research_worker/runtime"
CLAUDE = shutil.which("claude")
CLAUDE = str(Path(CLAUDE).resolve()) if CLAUDE else None
PARENT_ONLY_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY",
                         "CLAUDE_CODE_OAUTH_TOKEN", "ORCA_TERMINAL_HANDLE",
                         "ORCA_AGENT_HOOK_TOKEN", "AWS_PROFILE")
# Set by srt itself for its proxies; none comes from the parent environment.
SRT_VARIABLES = {
    "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "FTP_PROXY", "ftp_proxy", "GRPC_PROXY", "grpc_proxy", "RSYNC_PROXY",
    "DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY", "NO_PROXY", "no_proxy",
    "CLOUDSDK_PROXY_ADDRESS", "CLOUDSDK_PROXY_PASSWORD", "CLOUDSDK_PROXY_PORT",
    "CLOUDSDK_PROXY_TYPE", "CLOUDSDK_PROXY_USERNAME", "GIT_CONFIG_PARAMETERS",
    "GIT_SSH_COMMAND", "JAVA_TOOL_OPTIONS", "SANDBOX_RUNTIME",
}
if LINUX:
    # wrapCommandWithSandboxLinux's own --setenv names (srt 0.0.77).
    SRT_VARIABLES |= {"CLAUDE_CODE_HOST_HTTP_PROXY_PORT", "CLAUDE_CODE_HOST_SOCKS_PROXY_PORT",
                      "SRT_OBSERVE_SOCK", "SRT_ENCODED_CMD"}
# Added by the shell/OS on every process start.
PROCESS_VARIABLES = {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
if LINUX:
    # Python's C-locale coercion (PEP 538) sets it when LANG's locale is not
    # installed, as on the Debian guest; the parent environment has none.
    PROCESS_VARIABLES |= {"LC_CTYPE"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Recorder(http.server.BaseHTTPRequestHandler):
    """Local stand-in for a REST/GraphQL/git host that would accept writes."""

    def log_message(self, *arguments):
        pass

    def record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode(errors="replace") if length else ""
        self.server.requests.append((self.command, self.path, body[:200]))

    def reply(self, status):
        payload = b'{"ok":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.record()
        self.reply(403 if "git-receive-pack" in self.path else 200)

    def do_POST(self):
        self.record()
        self.reply(200 if self.path == "/graphql" else 201)


def serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def host_shows(path):
    """The control for a Linux ENOENT: the path, or a write's parent, exists here."""
    path = Path(path)
    return path.exists() or path.parent.is_dir()


@unittest.skipUnless(PREFIX and sys.platform in ("darwin", "linux"),
                     "set OSS_RESEARCH_SRT_PREFIX to the pinned srt install (macOS, Linux)")
class RuntimeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = worker.verify_runtime(Path(PREFIX), os.environ["PATH"])

    def setUp(self):
        # Unix socket paths must stay under 104 bytes, so not the scratchpad.
        self.root = Path(tempfile.mkdtemp(prefix="osrw-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.sentinel = f"oss-runtime-sentinel-{uuid.uuid4().hex}"
        self.tag = uuid.uuid4().hex
        self.input = self.root / "input"
        self.input.mkdir()
        shutil.copy(PROBE, self.input / "probe.py")
        (self.input / "corpus.txt").write_text("public OSS notes\n")
        self.home = self.root / "parent-home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh/id_ed25519").write_text(self.sentinel)
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text("{}")
        self.other = self.root / "other-worktree"
        self.other.mkdir()
        self.run_dir = self.root / "run"

    def policy(self, allowed=(), local_binding=False, extra_read=()):
        policy = worker.build_policy(self.input, self.run_dir, self.root / "claude",
                                     runtime=self.runtime)
        policy["filesystem"]["allowRead"] += FIXTURE_READ + list(extra_read)
        policy["network"]["allowedDomains"] += list(allowed)
        policy["network"]["allowLocalBinding"] = local_binding
        return policy

    def parent_environment(self):
        environment = {name: self.sentinel for name in PARENT_ONLY_VARIABLES}
        environment.update(PATH=os.environ["PATH"], LANG="en_US.UTF-8", TERM="dumb",
                           HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.home / ".claude"))
        return environment

    def sandbox(self, plan, policy=None, policy_file=None):
        if policy_file is None:
            policy_file = worker.prepare_run(self.run_dir, policy or self.policy())
        plan_file = self.input / f"plan-{uuid.uuid4().hex}.json"
        plan_file.write_text(json.dumps(plan))
        command = worker.sandboxed(self.runtime, policy_file,
                                   [PYTHON, str(self.input / "probe.py"), str(plan_file)])
        environment = worker.build_environment(self.parent_environment(), self.run_dir)
        exit_code, stdout, stderr = worker.launch(command, environment, self.input, 120)
        self.assertEqual(exit_code, 0, stderr)
        return {item["name"]: item for item in json.loads(stdout)}

    def assertDenied(self, result, path=None):
        self.assertFalse(result["ok"], result)
        if not LINUX:
            self.assertEqual(result.get("errno"), "Operation not permitted", result)
            return
        if result.get("errno") == LINUX_HIDDEN:
            self.assertIsNotNone(path, f"ENOENT without a host control: {result}")
            self.assertTrue(host_shows(path), f"ENOENT but the host lacks {path} too")
        else:
            self.assertIn(result.get("errno"), LINUX_DENIED, result)

    def assertNetworkDenied(self, result, port):
        """Direct TCP: EPERM on macOS; on Linux the empty network namespace
        refuses, which counts only because the same port answers from here."""
        self.assertFalse(result["ok"], result)
        if not LINUX:
            self.assertEqual(result.get("errno"), "Operation not permitted", result)
            return
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        self.assertIn(result.get("errno"), {"Connection refused", "Network is unreachable",
                                            "Operation not permitted"}, result)


class FileBoundaryTests(RuntimeFixture):
    def plan(self):
        return [
            {"op": "read", "name": "input read", "path": str(self.input / "corpus.txt")},
            {"op": "write", "name": "output write", "path": str(self.run_dir / "output/notes.md")},
            {"op": "write", "name": "tmp write", "path": str(self.run_dir / "tmp/scratch")},
            {"op": "write", "name": "home write", "path": str(self.run_dir / "home/state")},
            {"op": "write", "name": "input tamper", "replace": True,
             "path": str(self.input / "corpus.txt")},
            {"op": "write", "name": "input new file", "path": str(self.input / "planted.sh")},
            {"op": "read", "name": "parent home secret",
             "path": str(self.home / ".ssh/id_ed25519")},
            {"op": "write", "name": "parent home config", "replace": True,
             "path": str(self.home / ".claude/settings.json")},
            {"op": "listdir", "name": "real HOME list", "path": str(Path.home())},
            {"op": "write", "name": "real HOME write",
             "path": str(Path.home() / f".osrw-probe-{self.tag}")},
            {"op": "write", "name": "other worktree write",
             "path": str(self.other / "planted.txt")},
            {"op": "write", "name": "this worktree write",
             "path": str(REPO / f".osrw-probe-{self.tag}")},
            {"op": "write", "name": "policy replace", "replace": True,
             "path": str(self.run_dir / "policy/srt-settings.json")},
            {"op": "write", "name": "policy new file",
             "path": str(self.run_dir / "policy/next-run.json")},
            {"op": "write", "name": "result plant", "path": str(self.run_dir / "result/report.md")},
            {"op": "rename", "name": "move output root", "path": str(self.run_dir / "output"),
             "target": str(self.run_dir / "output-moved")},
            {"op": "write", "name": "shared tmp write",
             "path": f"{SHORT_TMP}/osrw-probe-{self.tag}"},
            {"op": "write", "name": "srt default tmp write",
             "path": f"/tmp/claude/osrw-probe-{self.tag}"},
            {"op": "read", "name": "hosts read", "path": "/etc/hosts"},
            {"op": "read", "name": "etc passwd", "path": "/etc/passwd"},
            {"op": "read", "name": "private etc ssh",
             "path": "/etc/ssh/ssh_config" if LINUX else "/private/etc/ssh/ssh_config"},
            {"op": "listdir", "name": "usr local", "path": "/usr/local"},
            # Linux: /usr/bin lists as srt's own tmpfs holding only the read-set
            # binaries, so a binary outside the set is read instead (rg exists).
            {"op": "read", "name": "usr bin", "path": "/usr/bin/rg"} if LINUX else
            {"op": "listdir", "name": "usr bin", "path": "/usr/bin"},
            {"op": "listdir", "name": "library preferences",
             "path": "/var/log" if LINUX else "/Library/Preferences"},
            {"op": "read", "name": "zoneinfo default",
             "path": "/usr/share/zoneinfo/UTC" if LINUX
             else "/usr/share/zoneinfo.default/UTC"},
        ]

    def tearDown(self):
        for leftover in (Path.home() / f".osrw-probe-{self.tag}",
                         REPO / f".osrw-probe-{self.tag}",
                         Path(f"{SHORT_TMP}/osrw-probe-{self.tag}"),
                         Path(f"/tmp/claude/osrw-probe-{self.tag}")):
            if leftover.exists():
                leftover.unlink()
                self.fail(f"sandboxed probe created {leftover}")

    def check(self, results, plan):
        paths = {step["name"]: step["path"] for step in plan if "path" in step}
        contained = set()
        shared = results["shared tmp write"]
        if LINUX and (shared["ok"] or shared.get("errno") == "File exists"):
            # Linux: /tmp is srt's private tmpfs, so the write succeeds there
            # (the child then finds the parent's copy) but must not reach the
            # host (tearDown also fails on a host copy).
            self.assertFalse(Path(paths["shared tmp write"]).exists())
            contained.add("shared tmp write")
        if LINUX and not host_shows(paths["srt default tmp write"]):
            # srt's TMPDIR (/tmp/claude) exists neither on the host nor inside:
            # nothing to deny, so no claim is made for it on Linux.
            self.assertFalse(results["srt default tmp write"]["ok"])
            contained.add("srt default tmp write")
        for allowed in ("input read", "output write", "tmp write", "home write", "hosts read"):
            self.assertTrue(results[allowed]["ok"], results[allowed])
        for denied in ("input tamper", "input new file", "parent home secret",
                       "parent home config", "real HOME list", "real HOME write",
                       "other worktree write", "this worktree write", "policy replace",
                       "policy new file", "result plant", "move output root",
                       "shared tmp write", "srt default tmp write", "etc passwd",
                       "private etc ssh", "usr local", "usr bin", "library preferences",
                       "zoneinfo default"):
            if denied not in contained:
                self.assertDenied(results[denied], paths[denied])

    def test_process_and_child_share_the_file_boundary(self):
        corpus = sha256(self.input / "corpus.txt")
        plan = self.plan()
        results = self.sandbox(plan + [{"op": "child", "name": "child", "plan": [
            dict(step, path=step["path"].replace("notes.md", "child.md")
                 .replace("scratch", "child-scratch").replace("state", "child-state"))
            for step in plan]}])
        self.check(results, plan)
        child = results["child"]
        self.assertTrue(child["ok"], child)
        self.check({item["name"]: item for item in child["results"]}, plan)
        self.assertEqual(sha256(self.input / "corpus.txt"), corpus)
        self.assertEqual(sorted(p.name for p in self.input.iterdir()
                                if not p.name.startswith("plan-")),
                         ["corpus.txt", "probe.py"])
        self.assertEqual((self.home / ".claude/settings.json").read_text(), "{}")
        self.assertEqual(list(self.other.iterdir()), [])
        self.assertEqual(sorted(p.name for p in (self.run_dir / "policy").iterdir()),
                         ["srt-settings.json"])
        self.assertEqual(list((self.run_dir / "result").iterdir()), [])
        self.assertTrue((self.run_dir / "output/notes.md").is_file())
        self.assertTrue((self.run_dir / "output/child.md").is_file())
        self.assertEqual(worker.parent_path_problems(self.run_dir), [])
        self.assertEqual(worker.area_problems(self.run_dir), [])

    def test_output_config_names_are_writable_but_inert(self):
        """README's mandatory write denies did not apply outside the working directory."""
        results = self.sandbox([
            {"op": "write", "name": "output mcp", "path": str(self.run_dir / "output/.mcp.json")},
            {"op": "write", "name": "home claude settings",
             "path": str(self.run_dir / "home/.claude/settings.json")},
        ])
        self.assertTrue(results["output mcp"]["ok"], results)
        self.assertTrue(results["home claude settings"]["ok"], results)


class EnvironmentTests(RuntimeFixture):
    def test_parent_sentinels_do_not_reach_process_or_child(self):
        step = {"op": "env", "name": "env", "sentinel": self.sentinel}
        results = self.sandbox([step, {"op": "child", "name": "child", "plan": [step]}])
        for seen in (results["env"], results["child"]["results"][0]):
            self.assertFalse(seen["sentinel_seen"])
            names = set(seen["names"])
            self.assertFalse(names & set(PARENT_ONLY_VARIABLES), names)
            self.assertFalse({n for n in names if n.startswith("ORCA_")}, names)
            self.assertEqual(names - SRT_VARIABLES - PROCESS_VARIABLES,
                             {"PATH", "LANG", "TERM", "HOME", "TMPDIR", "CLAUDE_CONFIG_DIR"})

    def test_runtime_passes_parent_environment_through_unchanged(self):
        """srt does not scrub: the runner's allowlist is the only filter."""
        policy_file = worker.prepare_run(self.run_dir, self.policy())
        plan_file = self.input / "plan.json"
        plan_file.write_text(json.dumps([{"op": "env", "name": "env",
                                          "sentinel": self.sentinel}]))
        command = worker.sandboxed(self.runtime, policy_file,
                                   [PYTHON, str(self.input / "probe.py"), str(plan_file)])
        unfiltered = self.parent_environment()
        unfiltered["TMPDIR"] = str(self.run_dir / "tmp")
        exit_code, stdout, stderr = worker.launch(command, unfiltered, self.input, 60)
        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(json.loads(stdout)[0]["sentinel_seen"])


class UnixSocketTests(RuntimeFixture):
    def test_orca_like_socket_is_refused_for_process_and_child(self):
        path = self.root / "orca.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(4)
        server.settimeout(0.2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.connect(str(path))
        server.accept()[0].close()
        step = {"op": "unix", "name": "orca socket", "path": str(path)}
        results = self.sandbox([step, {"op": "child", "name": "child", "plan": [step]}])
        self.assertDenied(results["orca socket"], path)
        self.assertDenied(results["child"]["results"][0], path)
        with self.assertRaises(TimeoutError):
            server.accept()


@unittest.skipUnless(LINUX, "Linux guest services")
class GuestServiceTests(RuntimeFixture):
    """Guest daemons that would query or act for a caller (systemd-resolved over
    varlink or D-Bus): reachable by the research user outside srt (the
    control), refused inside it. DNS over 127.0.0.53 is not claimed here: the
    guest's nft rule already refuses it outside srt, so it has no control."""

    SOCKETS = ("/run/systemd/resolve/io.systemd.Resolve", "/run/dbus/system_bus_socket")

    def test_resolver_and_bus_sockets_are_refused_inside_srt(self):
        present = [path for path in self.SOCKETS if Path(path).exists()]
        if not present:
            self.skipTest("no resolver or bus socket on this guest")
        for path in present:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                control.settimeout(5)
                control.connect(path)
        steps = [{"op": "unix", "name": path, "path": path} for path in present]
        results = self.sandbox(steps + [{"op": "child", "name": "child", "plan": steps}])
        child = {item["name"]: item for item in results["child"]["results"]}
        for path in present:
            self.assertDenied(results[path], path)
            self.assertDenied(child[path], path)


class NetworkTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.allowed = serve()
        self.other_host = serve()
        for server in (self.allowed, self.other_host):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.a = self.allowed.server_address[1]
        self.b = self.other_host.server_address[1]

    def url(self, port, path):
        return f"http://127.0.0.1:{port}{path}"

    def test_allowed_local_lookup_passes_only_through_the_proxy(self):
        results = self.sandbox([
            {"op": "http", "name": "proxied read", "proxy": True,
             "url": self.url(self.a, "/repos/o/r")},
            {"op": "http", "name": "direct read", "url": self.url(self.a, "/repos/o/r?direct")},
            {"op": "tcp", "name": "direct tcp allowed port", "host": "127.0.0.1", "port": self.a},
            {"op": "http", "name": "proxied other port", "proxy": True,
             "url": self.url(self.b, "/repos/o/r")},
            {"op": "tcp", "name": "direct tcp other port", "host": "127.0.0.1", "port": self.b},
            {"op": "http", "name": "proxied unlisted host", "proxy": True,
             "url": "http://api.github.invalid/repos/o/r"},
            {"op": "child", "name": "child", "plan": [
                {"op": "tcp", "name": "child direct", "host": "127.0.0.1", "port": self.a},
                {"op": "http", "name": "child proxied other", "proxy": True,
                 "url": self.url(self.b, "/child")}]},
        ], policy=self.policy(allowed=[f"127.0.0.1:{self.a}"]))
        self.assertEqual(results["proxied read"]["status"], 200, results)
        self.assertNetworkDenied(results["direct read"], self.a)
        self.assertNetworkDenied(results["direct tcp allowed port"], self.a)
        self.assertNetworkDenied(results["direct tcp other port"], self.b)
        for refused in ("proxied other port", "proxied unlisted host"):
            self.assertEqual(results[refused]["status"], 403, results[refused])
            self.assertEqual(results[refused]["proxy_error"], "blocked-by-allowlist")
        child = {item["name"]: item for item in results["child"]["results"]}
        self.assertNetworkDenied(child["child direct"], self.a)
        self.assertEqual(child["child proxied other"]["status"], 403)
        self.assertEqual(self.allowed.requests, [("GET", "/repos/o/r", "")])
        self.assertEqual(self.other_host.requests, [])

    def test_production_policy_reaches_no_local_fixture(self):
        results = self.sandbox([
            {"op": "http", "name": "proxied", "proxy": True, "url": self.url(self.a, "/x")},
            {"op": "tcp", "name": "direct", "host": "127.0.0.1", "port": self.a},
        ])
        self.assertEqual(results["proxied"].get("status"), 403, results)
        self.assertNetworkDenied(results["direct"], self.a)
        self.assertEqual(self.allowed.requests, [])

    @unittest.skipUnless(GIT, "git is not installed (the Debian guest has none)")
    def test_write_requests_to_an_allowed_destination_are_not_blocked(self):
        """srt filters by destination only: REST writes, mutations and pushes pass."""
        clone = self.run_dir / "tmp/clone"
        git = [GIT, "-C", str(clone), "-c", "user.name=probe", "-c", "user.email=probe@invalid"]
        unset = ["NO_PROXY", "no_proxy"]
        subprocess.run([GIT, "init", "-q", "--bare", str(self.input / "bare.git")], check=True)
        results = self.sandbox([
            {"op": "http", "name": "rest write", "proxy": True, "method": "POST",
             "url": self.url(self.a, "/repos/o/r/issues"), "body": '{"title":"probe"}'},
            {"op": "http", "name": "graphql mutation", "proxy": True, "method": "POST",
             "url": self.url(self.a, "/graphql"),
             "body": '{"query":"mutation { addComment(input:{}) { clientMutationId } }"}'},
            {"op": "run", "name": "clone", "argv": [GIT, "clone", "-q",
                                                  str(self.input / "bare.git"), str(clone)]},
            {"op": "run", "name": "commit", "argv": git + ["commit", "-q", "--allow-empty",
                                                        "-m", "probe"]},
            {"op": "run", "name": "http push", "unset": unset,
             "argv": git + ["push", self.url(self.a, "/o/r.git"), "HEAD:main"]},
            {"op": "run", "name": "file push", "argv": git + ["push", "origin", "HEAD:main"]},
        ], policy=self.policy(allowed=[f"127.0.0.1:{self.a}"],
                              # git's HTTP helper aborts without it; the runner never opens it.
                              extra_read=[OPENSSL_CONFIG]))
        self.assertEqual(results["rest write"]["status"], 201)
        self.assertEqual(results["graphql mutation"]["status"], 200)
        self.assertTrue(results["commit"]["ok"], results["commit"])
        self.assertFalse(results["http push"]["ok"], results["http push"])
        methods = [(method, path.split("?")[0]) for method, path, _ in self.allowed.requests]
        self.assertIn(("GET", "/o/r.git/info/refs"), methods, results["http push"])
        self.assertIn(("POST", "/repos/o/r/issues"), methods)
        self.assertIn(("POST", "/graphql"), methods)
        self.assertIn(("GET", "/o/r.git/info/refs"), methods)
        # The file remote is denied by the write boundary, not by the network.
        self.assertFalse(results["file push"]["ok"])
        self.assertIn("unable to create temporary object directory", results["file push"]["stderr"])
        refs = subprocess.run([GIT, "-C", str(self.input / "bare.git"), "for-each-ref"],
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(refs, "")

    @unittest.skipIf(LINUX, "macOS measurement; Linux keeps its own network namespace, "
                     "not measured")
    def test_local_binding_opens_every_loopback_port_directly(self):
        """allowLocalBinding: true would be a direct bypass; the runner keeps false."""
        results = self.sandbox([
            {"op": "tcp", "name": "direct other port", "host": "127.0.0.1", "port": self.b},
        ], policy=self.policy(local_binding=True))
        self.assertTrue(results["direct other port"]["ok"], results)
        self.assertIs(worker.build_policy(self.input, self.run_dir, self.root / "c")
                      ["network"]["allowLocalBinding"], False)


@unittest.skipUnless(sys.platform == "darwin", "macOS Keychain only")
class KeychainTests(RuntimeFixture):
    """Disposable file keychain with a fake item. The login keychain is never read."""

    def setUp(self):
        super().setUp()
        self.keychains = self.root / "keychains"
        self.keychains.mkdir()
        self.keychain = self.keychains / "fixture.keychain-db"
        self.search_list = self.user_search_list()
        subprocess.run([SECURITY, "create-keychain", "-p", "disposable", str(self.keychain)],
                       check=True, capture_output=True)
        self.addCleanup(subprocess.run, [SECURITY, "delete-keychain", str(self.keychain)],
                        capture_output=True)
        subprocess.run([SECURITY, "add-generic-password", "-a", "fixture", "-s",
                        "oss-research-fixture", "-w", self.sentinel, "-T", SECURITY,
                        str(self.keychain)], check=True, capture_output=True)

    @staticmethod
    def user_search_list():
        return subprocess.run([SECURITY, "list-keychains", "-d", "user"],
                              capture_output=True, text=True, check=True).stdout

    def lookup(self, name):
        return {"op": "run", "name": name, "stdout": True,
                "argv": [SECURITY, "find-generic-password", "-s", "oss-research-fixture",
                         "-w", str(self.keychain)]}

    def test_fixture_item_is_unreadable_only_while_its_file_is_read_denied(self):
        self.assertEqual(self.user_search_list(), self.search_list)
        denied = self.sandbox([self.lookup("denied")],
                              policy=self.policy(extra_read=SECURITY_READ))["denied"]
        self.assertFalse(denied["ok"], denied)
        self.assertNotIn(self.sentinel, denied["stdout"] + denied["stderr"])
        shutil.rmtree(self.run_dir)
        readable = self.sandbox([self.lookup("readable")],
                                policy=self.policy(extra_read=[str(self.keychains)]
                                                   + SECURITY_READ))["readable"]
        self.assertTrue(readable["ok"], readable)
        self.assertEqual(readable["stdout"].strip(), self.sentinel)
        self.assertEqual(self.user_search_list(), self.search_list)


class InstallTests(RuntimeFixture):
    def test_committed_lock_reproduces_the_pinned_tree_with_npm_ci(self):
        cache = Path(os.environ.get("OSS_RESEARCH_NPM_CACHE") or Path(PREFIX) / ".npm-cache")
        if not cache.is_dir():
            self.skipTest("no npm cache for an offline npm ci")
        prefix = self.root / "srt-ci"
        prefix.mkdir()
        for name in ("package.json", "package-lock.json"):
            shutil.copy(LOCK_FIXTURE / name, prefix / name)
        subprocess.run(["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
                        "--cache", str(cache)], cwd=prefix, check=True,
                       capture_output=True, text=True, timeout=300)
        self.assertEqual(worker.tree_sha256(prefix / "node_modules"), worker.SRT_TREE_SHA256)
        self.assertEqual(worker.verify_runtime(prefix, os.environ["PATH"])[1],
                         str(prefix.resolve() / "node_modules" / worker.SRT_PACKAGE
                             / "dist/cli.js"))


@unittest.skipUnless(CLAUDE, "claude is not on PATH")
class ClaudeStartupTests(RuntimeFixture):
    """Binary start only. No model request, credential, skill or auto mode."""

    def start(self, argument, drop_read=()):
        policy = worker.build_policy(self.input, self.run_dir, Path(CLAUDE), runtime=self.runtime)
        policy["network"]["allowedDomains"] = []
        policy["filesystem"]["allowRead"] = [
            path for path in policy["filesystem"]["allowRead"] if path not in drop_read]
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        policy_file = worker.prepare_run(self.run_dir, policy)
        environment = worker.build_environment(self.parent_environment(), self.run_dir)
        return worker.launch(worker.sandboxed(self.runtime, policy_file, [CLAUDE, argument]),
                             environment, self.input, 60)

    def test_version_and_help_start_under_the_runner_policy(self):
        version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin", "HOME": str(self.run_dir)},
                                 timeout=60).stdout
        exit_code, stdout, stderr = self.start("--version")
        self.assertEqual((exit_code, stdout), (0, version), stderr)
        exit_code, stdout, stderr = self.start("--help")
        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(stdout.startswith("Usage: claude"), stdout[:200])
        self.assertNotIn(self.sentinel, stdout + stderr)
        self.assertEqual(worker.parent_path_problems(self.run_dir), [])

    # bash, run inside srt: connect to srt's own proxy by the name "localhost"
    # (what srt puts in HTTPS_PROXY), the lookup a model request makes first.
    LOCALHOST_PROBE = ('p=${HTTPS_PROXY##*:}; p=${p%%/*}; case "$HTTPS_PROXY" in *localhost*) ;; '
                       '*) echo "PROXY-NOT-LOCALHOST"; exit 3;; esac; '
                       'exec 3<>"/dev/tcp/localhost/$p" && echo CONNECTED')

    def run_in_policy(self, argv, drop_read=()):
        policy = worker.build_policy(self.input, self.run_dir, Path(CLAUDE), runtime=self.runtime)
        policy["filesystem"]["allowRead"] = [
            path for path in policy["filesystem"]["allowRead"] if path not in drop_read]
        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)
        policy_file = worker.prepare_run(self.run_dir, policy)
        environment = worker.build_environment(self.parent_environment(), self.run_dir)
        return worker.launch(worker.sandboxed(self.runtime, policy_file, argv),
                             environment, self.input, 60)

    @unittest.skipUnless(LINUX, "Linux read set (SYSTEM_READ_LINUX) measurement")
    def test_each_linux_system_read_entry_is_needed(self):
        """Necessity of every system entry under the product policy (network kept,
        no model request): dropping one must break the start or the localhost
        lookup to srt's proxy. Paths outside the set stay hidden. Non-AI."""
        version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin", "HOME": str(self.run_dir)},
                                 timeout=60).stdout
        policy = worker.build_policy(self.input, self.run_dir, Path(CLAUDE), runtime=self.runtime)
        read = policy["filesystem"]["allowRead"]
        system = list(worker.system_read("linux"))
        entries = [*system, *worker.linux_link_targets(system),
                   *[path for path in read if path.endswith("/apply-seccomp")]]
        self.assertTrue(all(entry in read for entry in entries), (entries, read))

        def probes(drop=()):
            start = self.run_in_policy([CLAUDE, "--version"], drop)
            lookup = self.run_in_policy(["/usr/bin/bash", "-c", self.LOCALHOST_PROBE], drop)
            return {"start": start[0] == 0 and start[1] == version,
                    "localhost": lookup[0] == 0 and lookup[1].strip() == "CONNECTED"}

        matrix = {"full": probes()}
        for entry in entries:
            matrix[entry] = probes((entry,))
        # Absent, or an empty directory (a mount point bwrap creates): nothing to read.
        hidden = self.run_in_policy(["/usr/bin/bash", "-c", "shopt -s nullglob dotglob; "
                                     "for p in /etc/ssl/certs /etc/resolv.conf /etc/passwd "
                                     "/etc/nsswitch.conf /usr/share /home /root /var /srv; do "
                                     "if [ -d $p ]; then a=($p/*); [ ${#a[@]} -gt 0 ] && "
                                     "echo SEEN $p ${#a[@]} ${a[*]:0:3}; "
                                     "elif [ -e $p ]; then echo SEEN $p file; fi; done; echo DONE"])
        # /opt holds the pinned runtime: only the way to an allowed path may show.
        opt = self.run_in_policy(["/usr/bin/bash", "-c", "shopt -s nullglob dotglob; "
                                  "for d in /opt /opt/*; do for e in $d/*; do echo $e; done; done"])
        shown = opt[1].split()
        on_the_way = [path for path in shown
                      if not any(allowed == path or allowed.startswith(path + "/")
                                 for allowed in read)]
        # A symlink entry is kept to find its target (build_policy); its target is
        # what bwrap needs, so necessity is judged on the target.
        links = {entry: os.path.realpath(entry) for entry in system if os.path.islink(entry)}
        evidence = os.environ.get("OSS_RESEARCH_READSET_EVIDENCE")
        if evidence:
            Path(evidence).write_text(json.dumps({"entries": entries, "matrix": matrix,
                                                  "links_kept_for_their_target": links,
                                                  "hidden_probe": hidden[1].split("\n"),
                                                  "opt_shown": shown, "opt_not_on_the_way": on_the_way},
                                                 indent=1))
        self.assertEqual(matrix["full"], {"start": True, "localhost": True}, matrix)
        for entry in entries:
            if entry in links:
                self.assertIn(links[entry], entries)
                continue
            with self.subTest(entry=entry):
                self.assertFalse(all(matrix[entry].values()),
                                 f"{entry} is not needed by either probe: {matrix[entry]}")
        self.assertEqual(hidden[1].split(), ["DONE"], hidden)
        self.assertEqual(on_the_way, [], shown)

    @unittest.skipIf(LINUX, "macOS measurement (ICU data); Linux started with /usr/lib")
    def test_help_crashes_without_icu_data(self):
        exit_code, stdout, stderr = self.start("--help", drop_read=("/usr/share/icu",))
        self.assertNotEqual(exit_code, 0)
        self.assertIn("panic", stderr + stdout)


class FailClosedTests(RuntimeFixture):
    def srt(self, settings):
        policy = self.root / "srt-settings.json"
        if settings is not None:
            policy.write_text(settings)
        completed = subprocess.run(
            worker.sandboxed(self.runtime, policy, ECHO),
            env={"PATH": os.environ["PATH"], "HOME": str(self.root)},
            capture_output=True, text=True, timeout=60)
        return completed

    def test_missing_empty_corrupt_or_old_policy_never_runs_the_command(self):
        old = worker.build_policy(self.input, self.run_dir, self.root / "claude",
                                  runtime=self.runtime)
        del old["network"]["deniedDomains"]
        cases = {"missing": None, "empty": "", "corrupt": '{"filesystem": ',
                 "without deniedDomains": json.dumps(old)}
        for name, settings in cases.items():
            with self.subTest(name):
                completed = self.srt(settings)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertNotIn("RAN", completed.stdout)

    def test_unknown_policy_key_is_silently_ignored_not_rejected(self):
        """A misspelled or invented key is not an error, so it restricts nothing."""
        unknown = self.policy()
        unknown["network"]["allowedMethods"] = ["GET"]
        completed = self.srt(json.dumps(unknown))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "RAN\n")

    def test_unreadable_policy_never_runs_the_command(self):
        policy = self.root / "srt-settings.json"
        policy.write_text(json.dumps(self.policy()))
        policy.chmod(0)
        completed = self.srt(None)
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("RAN", completed.stdout)

    def test_valid_policy_runs_the_command(self):
        completed = self.srt(json.dumps(self.policy()))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "RAN\n")


class LaunchGateRuntimeTests(RuntimeFixture):
    def launch(self, prefix):
        (self.input / "task.txt").write_text("Summarize the corpus.")
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--profile", "claude-profile1",
             "--input-dir", str(self.input), "--prompt-file", str(self.input / "task.txt"),
             "--run-dir", str(self.run_dir), "--runtime-prefix", str(prefix)],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            capture_output=True, text=True, timeout=120)

    def test_pinned_runtime_verifies_but_the_launch_stays_closed(self):
        result = self.launch(PREFIX)
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn(f"srt {worker.SRT_VERSION} verified", result.stderr)
        if LINUX:
            # Closed earlier on Linux: a host problem or the unmeasured read set.
            self.assertTrue("cannot run it isolated" in result.stderr
                            or "system read set is not measured yet" in result.stderr,
                            result.stderr)
        else:
            self.assertIn("no credential binding for claude-profile1", result.stderr)
        self.assertFalse(self.run_dir.exists())

    def test_tampered_or_absent_runtime_is_blocked(self):
        copy = self.root / "srt-copy"
        shutil.copytree(PREFIX, copy, symlinks=True,
                        ignore=shutil.ignore_patterns(".npm-cache"))
        cli = copy / "node_modules" / worker.SRT_PACKAGE / "dist/cli.js"
        cli.write_text(cli.read_text() + "\n// tampered\n")
        tampered = self.launch(copy)
        self.assertEqual(tampered.returncode, worker.EXIT_BLOCKED)
        self.assertIn("tree differs", tampered.stderr)
        shutil.rmtree(copy / "node_modules")
        absent = self.launch(copy)
        self.assertEqual(absent.returncode, worker.EXIT_BLOCKED)
        self.assertIn("is not installed", absent.stderr)
        self.assertFalse(self.run_dir.exists())


if __name__ == "__main__":
    unittest.main()
