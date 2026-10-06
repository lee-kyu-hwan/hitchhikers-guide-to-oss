"""Check the credential path behind the closed gate, offline.

Every credential here is a fake sentinel in a temporary file, and every
`srt`/`claude` is a test-only fake that enforces nothing. No real token,
parent profile, Keychain, SSH, login or model endpoint is touched. Passing
tests show the runner's contract (what reaches the child, what is refused,
what is recorded), not that any token authenticates or belongs to an account.
"""

import importlib.machinery
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "bin/oss-research-worker"
# srt's socket paths stay short; Linux has no /private.
SHORT_TMP = "/tmp" if sys.platform == "linux" else "/private/tmp"
LOADER = importlib.machinery.SourceFileLoader("oss_research_worker_auth", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
worker = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(worker)
# The model endpoint as the platform's policy spells it (Linux adds :443).
MODEL_DOMAINS = list(worker.ALLOWED_DOMAINS_LINUX if sys.platform == "linux"
                     else worker.ALLOWED_DOMAINS)

PYTHON = sys.executable
FAKE_TOKEN = "sentinel-auth-token-" + "q" * 32
PARENT_TOKEN = "sentinel-parent-token-" + "p" * 32
ESCAPED_TOKEN = "".join(f"\\u{ord(c):04x}" for c in FAKE_TOKEN)


def write_executable(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def result_json(text="# Summary\n"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "permission_denials": [], "result": text})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osra-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        (self.input / "task.txt").write_text("summarize", encoding="utf-8")
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        write_executable(self.bin / "srt", (
            "import os,sys\n"
            "arguments=sys.argv[1:]\n"
            "assert arguments[0]=='--settings' and arguments[2]=='--'\n"
            "os.execv(arguments[3],arguments[3:])\n"
        ))

    def write_credential(self, profile="claude-profile1", content=FAKE_TOKEN + "\n",
                         base=None):
        directory = (base or self.root / "credentials") / profile
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        credential = directory / worker.CREDENTIAL_NAME
        credential.write_text(content)
        credential.chmod(0o600)
        return credential

    def credential(self):
        return worker.load_credential("claude-profile1", self.write_credential(),
                                      self.input, self.run_dir)

    def research(self, body="", stdout=None, credential=None, relay=None,
                 permission_mode=worker.DEFAULT_PERMISSION_MODE, prompt="prompt",
                 selection=None):
        """The post-gate path (run_research) with a fake srt and child."""
        stdout = result_json() if stdout is None else stdout
        write_executable(self.bin / "claude", (
            "import json,os,sys\n"
            "out=os.path.join(os.environ['HOME'],'..','output')\n"
            "open(os.path.join(out,'argv.json'),'w').write(json.dumps(sys.argv[1:]))\n"
            + body + f"sys.stdout.write({stdout!r})\n"
        ))
        parent = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                  worker.CREDENTIAL_VARIABLE: PARENT_TOKEN}
        # A live (credential) run needs an explicit selection; these fixture
        # values are test data, not a model choice.
        if selection is None and credential is not None:
            selection = worker.Selection("fixture-model", "high")
        succeeded = worker.run_research(
            [str(self.bin / "srt")], str(self.bin / "claude"), prompt, self.input,
            self.run_dir, "claude-profile1", 20, relay, parent, permission_mode, credential,
            selection=selection)
        manifest_file = self.run_dir / "result/manifest.json"
        manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else None
        return succeeded, manifest

    def test_manifest_binds_the_prompt_bytes_the_child_got(self):
        prompt = "Summarize the corpus.\r\nLine two: caf\u00e9\n"
        succeeded, manifest = self.research(prompt=prompt)
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["prompt_sha256"],
                         hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertRegex(manifest["prompt_sha256"], r"^[0-9a-f]{64}$")
        # The digest is of what the child got (its argv), not of a file read later.
        argv = json.loads((self.run_dir / "output/argv.json").read_text())
        self.assertEqual(hashlib.sha256(argv[-1].encode("utf-8")).hexdigest(), manifest["prompt_sha256"])

    def test_a_crlf_prompt_is_sent_and_bound_byte_for_byte(self):
        task = self.input / "task.txt"
        task.write_bytes(b"line one\r\nline two\r\n")
        _, prompt = worker.checked_input(self.input, task, self.root)
        self.assertEqual(prompt.encode("utf-8"), task.read_bytes())
        succeeded, manifest = self.research(prompt=prompt)
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["prompt_sha256"], hashlib.sha256(task.read_bytes()).hexdigest())
        argv = json.loads((self.run_dir / "output/argv.json").read_text())
        self.assertEqual(argv[-1], "line one\r\nline two\r\n")
        task.write_bytes(b"caf\xe9\n")  # not UTF-8
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            worker.checked_input(self.input, task, self.root)

    def test_the_prompt_digest_is_taken_from_the_checked_text_not_the_file(self):
        task = self.input / "task.txt"
        task.write_text("first version\n")
        _, prompt = worker.checked_input(self.input, task, self.root)
        task.write_text("changed after the check\n")  # a later change must not move it
        succeeded, manifest = self.research(prompt=prompt)
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["prompt_sha256"], worker.prompt_sha256("first version\n"))

    def result_files(self):
        return {path.name: path.read_bytes()
                for path in (self.run_dir / "result").iterdir()}

    def assert_not_recorded(self):
        """No file the parent wrote holds the value, in any form checked here."""
        for name, data in self.result_files().items():
            self.assertNotIn(FAKE_TOKEN.encode(), data, name)
            self.assertNotIn(ESCAPED_TOKEN.encode(), data, name)

    def assert_exposed(self, succeeded, manifest, where):
        self.assertFalse(succeeded)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn(f"credential-exposed: {where}", manifest["problems"])
        self.assertEqual(manifest["credential"]["exact_value_check"], "failed")
        files = self.result_files()
        self.assertEqual(set(files) - {"relay.log"}, {"manifest.json"}, files)
        self.assertNotIn("relay.log", files)
        self.assert_not_recorded()


class ClosedGateTests(Fixture):
    """The gate checks metadata only and still always blocks."""

    def fake_prefix(self):
        prefix = self.root / "srt-prefix"
        package = prefix / "node_modules" / worker.SRT_PACKAGE
        (package / "dist").mkdir(parents=True)
        (package / "dist/cli.js").write_text("// fake\n")
        (package / "package.json").write_text(json.dumps(
            {"name": worker.SRT_PACKAGE, "version": worker.SRT_VERSION}))
        packages = {"": {}}
        packages.update({f"node_modules/{name}": {"version": v, "integrity": i}
                         for name, (v, i) in worker.SRT_LOCKED.items()})
        (prefix / "package-lock.json").write_text(json.dumps({"packages": packages}))
        write_executable(self.bin / "node", "")
        original = worker.SRT_TREE_SHA256
        worker.SRT_TREE_SHA256 = worker.tree_sha256(prefix / "node_modules")
        self.addCleanup(setattr, worker, "SRT_TREE_SHA256", original)
        return prefix

    def test_gate_never_reads_the_token(self):
        prefix = self.fake_prefix()
        # Right size and metadata, but not a token: only a read would notice.
        credential = self.write_credential(content="two\nlines " + "z" * 30 + "\n")
        with mock.patch.object(worker, "load_credential",
                               side_effect=AssertionError("token read")), \
                mock.patch.object(worker.os, "read",
                                  side_effect=AssertionError("os.read")):
            with self.assertRaises(worker.Blocked) as caught:
                # The macOS gate order; the Linux one is LinuxClosedGateTests.
                worker.require_runtime("claude-profile1", prefix, str(self.bin),
                                       credential, (self.input, self.run_dir),
                                       platform="darwin")
        self.assertIn("no real launch has been measured", str(caught.exception))

    def test_main_blocks_with_a_good_credential_and_starts_nothing(self):
        prefix = self.fake_prefix()
        credential = self.write_credential()
        for mode in worker.PERMISSION_MODES:
            argv = ["worker", "--profile", "claude-profile1",
                    "--input-dir", str(self.input),
                    "--prompt-file", str(self.input / "task.txt"),
                    "--run-dir", str(self.run_dir), "--runtime-prefix", str(prefix),
                    "--credential-file", str(credential), "--permission-mode", mode]
            with mock.patch.object(worker, "load_credential") as load, \
                    mock.patch.object(worker, "run_research") as research, \
                    mock.patch.object(worker.subprocess, "Popen") as popen, \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {worker.CREDENTIAL_VARIABLE: PARENT_TOKEN}):
                self.assertEqual(worker.main(), worker.EXIT_BLOCKED)
            load.assert_not_called()
            research.assert_not_called()
            popen.assert_not_called()
            self.assertFalse(self.run_dir.exists())

    def test_cli_has_no_open_switch(self):
        source = SCRIPT.read_text(encoding="utf-8")
        options = set(worker.re.findall(r'add_argument\("(--[a-z-]+)"', source))
        self.assertEqual(options, {
            "--profile", "--input-dir", "--prompt-file", "--run-dir", "--runtime-prefix",
            "--credential-file", "--model", "--effort", "--permission-mode",
            "--execution-profile",
            "--web-domain", "--timeout", "--relay-repo",
            "--relay-request-budget", "--relay-max-response-bytes",
            "--relay-response-byte-budget", "--relay-timeout-seconds"})
        self.assertNotIn("os.environ.get(\"OSS", source)
        # main reads the environment once (parent = dict(os.environ)); every
        # child environment is built from allowlists over that copy.
        self.assertEqual(source.count("os.environ"), 1)

    def test_cli_error_never_prints_the_token(self):
        prefix = self.fake_prefix()
        credential = self.write_credential()
        credential.chmod(0o640)
        result = subprocess.run(
            [PYTHON, "-B", str(SCRIPT), "--profile", "claude-profile1",
             "--input-dir", str(self.input), "--prompt-file", str(self.input / "task.txt"),
             "--run-dir", str(self.run_dir), "--runtime-prefix", str(prefix),
             "--credential-file", str(credential)],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        self.assertFalse(self.run_dir.exists())


class LoadCredentialTests(Fixture):
    def assertBlocked(self, credential, message, profile="claude-profile1"):
        for check in (worker.checked_credential, worker.load_credential):
            with self.assertRaises(worker.Blocked) as caught:
                check(profile, credential, self.input, self.run_dir)
            self.assertIn(message, str(caught.exception))
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_value_is_read_once_and_never_shown(self):
        credential = self.write_credential()
        real_read = os.read
        reads = []

        def counting(descriptor, size):
            reads.append(size)
            return real_read(descriptor, size)

        with mock.patch.object(worker.os, "read", side_effect=counting):
            loaded = worker.load_credential("claude-profile1", credential,
                                            self.input, self.run_dir)
        self.assertEqual(len(reads), 1)
        self.assertEqual(loaded.reveal(), FAKE_TOKEN)
        for shown in (repr(loaded), str(loaded), f"{loaded}", repr([loaded])):
            self.assertNotIn(FAKE_TOKEN, shown)
        with self.assertRaises(TypeError):
            pickle.dumps(loaded)
        with self.assertRaises(TypeError):
            json.dumps(loaded)

    def test_hard_link_from_another_profile_is_refused(self):
        credential = self.write_credential()
        other = self.root / "credentials/claude-profile2"
        other.mkdir(mode=0o700)
        os.link(credential, other / worker.CREDENTIAL_NAME)
        self.assertBlocked(credential, "exactly one link")
        self.assertBlocked(other / worker.CREDENTIAL_NAME, "exactly one link",
                           profile="claude-profile2")

    def test_path_naming_the_other_profile_is_refused(self):
        credential = self.write_credential(base=self.root / "claude-profile2/nested")
        self.assertBlocked(credential, "only its own profile")

    def test_directory_swapped_for_a_link_is_refused(self):
        real = self.write_credential(base=self.root / "real")
        (self.root / "links").mkdir()
        (self.root / "links/claude-profile1").symlink_to(real.parent)
        self.assertBlocked(self.root / "links/claude-profile1/oauth-token",
                           "must not be symlinks")

    def test_fifo_is_refused_without_blocking(self):
        directory = self.root / "credentials/claude-profile1"
        directory.mkdir(parents=True, mode=0o700)
        os.mkfifo(directory / worker.CREDENTIAL_NAME, 0o600)
        self.assertBlocked(directory / worker.CREDENTIAL_NAME, "not a regular file")

    def test_oversized_file_is_refused_by_size(self):
        credential = self.write_credential(content="a" * (worker.CREDENTIAL_MAX_BYTES + 1))
        self.assertBlocked(credential, "single-line ASCII token")

    def test_file_that_grows_while_read_is_refused(self):
        credential = self.write_credential()
        real_read = os.read

        def growing(descriptor, size):
            data = real_read(descriptor, size)
            with open(credential, "a") as handle:
                handle.write("more")
            return data

        with mock.patch.object(worker.os, "read", side_effect=growing):
            with self.assertRaisesRegex(worker.Blocked, "changed while it was read"):
                worker.load_credential("claude-profile1", credential,
                                       self.input, self.run_dir)


class EnvironmentTests(Fixture):
    def test_only_the_child_gets_the_checked_value(self):
        credential = self.credential()
        parent = {"PATH": "/usr/bin", worker.CREDENTIAL_VARIABLE: PARENT_TOKEN,
                  "ANTHROPIC_API_KEY": PARENT_TOKEN}
        without = worker.build_environment(parent, self.run_dir)
        self.assertNotIn(worker.CREDENTIAL_VARIABLE, without)
        child = worker.build_environment(parent, self.run_dir, credential)
        self.assertEqual(set(child) - set(without), {worker.CREDENTIAL_VARIABLE})
        self.assertEqual(child[worker.CREDENTIAL_VARIABLE], FAKE_TOKEN)
        self.assertNotIn(PARENT_TOKEN, json.dumps(child))
        relay = worker.relay_environment({**parent, **child})
        self.assertEqual(set(relay), {"PATH", "PYTHONDONTWRITEBYTECODE"})
        policy = worker.build_policy(self.input, self.run_dir, self.bin / "claude")
        self.assertEqual(policy["network"]["allowedDomains"], MODEL_DOMAINS)


class SupervisedCredentialTests(Fixture):
    """run_research past the gate, driven directly with fake sentinels."""

    def test_child_sees_the_value_and_nothing_records_it(self):
        body = (f"assert os.environ[{worker.CREDENTIAL_VARIABLE!r}]=={FAKE_TOKEN!r}\n"
                f"assert {PARENT_TOKEN!r} not in json.dumps(dict(os.environ))\n")
        succeeded, manifest = self.research(body, credential=self.credential(),
                                            permission_mode="auto")
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["permission_mode"], "auto")
        self.assertEqual(manifest["credential"],
                         {"variable": worker.CREDENTIAL_VARIABLE, "exact_value_check": "clean"})
        argv = json.loads((self.run_dir / "output/argv.json").read_text())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertNotIn(FAKE_TOKEN, json.dumps(argv))
        self.assert_not_recorded()
        self.assertTrue((self.run_dir / "result/report.md").is_file())

    def test_without_a_credential_nothing_changes(self):
        body = f"assert {worker.CREDENTIAL_VARIABLE!r} not in os.environ\n"
        succeeded, manifest = self.research(body)
        self.assertTrue(succeeded, manifest)
        self.assertNotIn("credential", manifest)
        self.assertEqual(manifest["permission_mode"], "dontAsk")

    def test_value_on_stdout_fails_and_is_not_stored(self):
        self.assert_exposed(*self.research(
            stdout=result_json(f"token {FAKE_TOKEN}"), credential=self.credential()),
            "stdout")

    def test_escaped_value_in_the_result_fails_and_is_not_stored(self):
        stdout = result_json("x").replace('"x"', f'"{ESCAPED_TOKEN}"')
        self.assertNotIn(FAKE_TOKEN, stdout)
        succeeded, manifest = self.research(stdout=stdout, credential=self.credential())
        self.assert_exposed(succeeded, manifest, "report")
        self.assertIn("credential-exposed: stdout json", manifest["problems"])

    def test_value_on_stderr_fails(self):
        body = f"sys.stderr.write({FAKE_TOKEN!r})\n"
        self.assert_exposed(*self.research(body, credential=self.credential()), "stderr")

    def test_value_copied_to_output_fails_but_the_copy_stays(self):
        body = (f"open(os.path.join(out,'notes.md'),'w')"
                f".write('x'*3000000+{FAKE_TOKEN!r})\n")
        self.assert_exposed(*self.research(body, credential=self.credential()), "output")
        # The parent does not delete the child's copy; the run dir must be removed.
        self.assertIn(FAKE_TOKEN.encode(),
                      (self.run_dir / "output/notes.md").read_bytes())

    def test_value_as_a_name_or_link_name_is_redacted(self):
        body = (f"os.mkdir(os.path.join(out,'..','tmp',{FAKE_TOKEN!r}))\n"
                f"os.symlink('/etc',os.path.join(out,{FAKE_TOKEN!r}))\n")
        succeeded, manifest = self.research(body, credential=self.credential())
        self.assert_exposed(succeeded, manifest, "tmp")
        self.assertIn("credential-exposed: problem text", manifest["problems"])

    def test_value_in_the_relay_log_fails(self):
        ready = json.dumps({"event": "ready", "host": "127.0.0.1", "port": 9,
                            "base_url": "http://127.0.0.1:9", "sources": []})
        # Read from a file so the relay's own argv does not hold the value.
        leak = self.root / "leak.txt"
        leak.write_text(FAKE_TOKEN)
        code = (f"import sys,time\nsys.stderr.write(open({str(leak)!r}).read())\n"
                "sys.stderr.flush()\n"
                f"sys.stdout.write({ready + chr(10)!r});sys.stdout.flush()\ntime.sleep(60)\n")
        relay = worker.RelayLaunch([PYTHON, "-B", "-c", code], {"PATH": "/usr/bin:/bin"},
                                   [], "0" * 64)
        self.assert_exposed(*self.research(credential=self.credential(), relay=relay),
                            "relay stderr")

    def test_value_bound_for_the_relay_starts_nothing(self):
        credential = self.credential()
        marker = self.root / "relay-ran"
        relay = worker.RelayLaunch(
            [PYTHON, "-c", f"open({str(marker)!r},'w')"],
            {"PATH": "/usr/bin:/bin", "GH_TOKEN": FAKE_TOKEN}, [], "0" * 64)
        succeeded, manifest = self.research(credential=credential, relay=relay)
        self.assertFalse(succeeded)
        self.assertIn("credential-in-relay", manifest["failures"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.run_dir / "output/argv.json").exists())
        self.assert_not_recorded()

    def test_value_in_the_prompt_argv_starts_no_child(self):
        succeeded, manifest = self.research(credential=self.credential(),
                                            prompt=f"use {FAKE_TOKEN}")
        self.assertFalse(succeeded)
        self.assertIn("credential-in-argv", manifest["failures"])
        self.assertFalse((self.run_dir / "output/argv.json").exists())
        self.assert_not_recorded()

    def test_scan_past_its_file_bound_fails_closed(self):
        body = "[open(os.path.join(out,f'f{i}'),'w') for i in range(20)]\n"
        with mock.patch.object(worker, "EXPOSURE_MAX_FILES", 5):
            succeeded, manifest = self.research(body, credential=self.credential())
        self.assertFalse(succeeded)
        self.assertTrue(any(item.startswith("credential-scan-incomplete: ")
                            and item.endswith(" files") for item in manifest["problems"]),
                        manifest["problems"])
        self.assertEqual(set(self.result_files()), {"manifest.json"})

    def test_scan_past_its_byte_bound_fails_closed(self):
        body = "open(os.path.join(out,'big'),'w').write('x'*(3*1024*1024))\n"
        with mock.patch.object(worker, "EXPOSURE_MAX_BYTES", 1024 * 1024):
            succeeded, manifest = self.research(body, credential=self.credential())
        self.assertFalse(succeeded)
        self.assertTrue(any(item.endswith(" bytes") for item in manifest["problems"]),
                        manifest["problems"])
        self.assertEqual(set(self.result_files()), {"manifest.json"})

    def test_fifo_in_an_area_does_not_hang_the_scan(self):
        body = "os.mkfifo(os.path.join(out,'..','tmp','pipe'))\n"
        succeeded, manifest = self.research(body, credential=self.credential())
        self.assertTrue(succeeded, manifest)

    def test_socket_in_an_area_leaves_the_scan_complete(self):
        # Linux srt keeps its proxy sockets in run/tmp; they hold no file data.
        body = ("import socket\n"
                "s=socket.socket(socket.AF_UNIX)\n"
                "s.bind(os.path.join(out,'..','tmp','srt-proxy.sock'))\n")
        succeeded, manifest = self.research(body, credential=self.credential())
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["credential"]["exact_value_check"], "clean")

    def test_socket_named_after_the_value_is_still_exposed(self):
        credential = self.credential()
        area = self.root / "area"
        area.mkdir()
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        # AF_UNIX paths are short: bind by a relative name inside the area.
        cwd = os.getcwd()
        os.chdir(area)
        try:
            server.bind(credential.reveal()[:20])
        finally:
            os.chdir(cwd)
        scan = worker.ExposureScan(credential.reveal()[:20])
        self.assertEqual(scan.area(area), "exposed")

    def test_manifest_with_the_value_in_a_requested_field_is_not_written(self):
        # A model value that happens to hold the credential must not reach the
        # manifest either: the exact-value check covers every manifest field.
        credential = self.credential()
        succeeded, manifest = self.research(
            credential=credential, selection=worker.Selection(credential.reveal(), "high"))
        self.assertFalse(succeeded)
        self.assertIsNone(manifest)
        self.assertNotIn(credential.reveal(), json.dumps(
            {name: data.decode(errors="replace") for name, data in self.result_files().items()}))

    def test_credential_of_another_profile_is_blocked_before_anything(self):
        other = worker.load_credential(
            "claude-profile2", self.write_credential(profile="claude-profile2"),
            self.input, self.run_dir)
        policy_for, command_for = mock.Mock(), mock.Mock()
        calls = (
            lambda: worker.run_research(
                [str(self.bin / "srt")], str(self.bin / "claude"), "prompt", self.input,
                self.run_dir, "claude-profile1", 20, None, {"PATH": "/usr/bin"},
                "dontAsk", other),
            lambda: worker.run_supervised(
                self.input, self.run_dir, "claude-profile1", 20, None, policy_for,
                command_for, {"PATH": "/usr/bin"}, other, "dontAsk"),
        )
        for call in calls:
            with mock.patch.object(worker.subprocess, "Popen") as popen, \
                    mock.patch.object(worker, "prepare_run") as prepare, \
                    mock.patch.object(worker.Credential, "reveal") as reveal:
                with self.assertRaisesRegex(worker.Blocked, "bound to claude-profile2"):
                    call()
            popen.assert_not_called()
            prepare.assert_not_called()
            reveal.assert_not_called()
            policy_for.assert_not_called()
            command_for.assert_not_called()
            self.assertFalse(self.run_dir.exists())

    def test_unlistable_directory_makes_the_scan_incomplete(self):
        area = self.root / "area"
        (area / "sub").mkdir(parents=True)
        real = os.scandir

        def failing(path="."):
            if Path(path) == area / "sub":
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", side_effect=failing):
            self.assertEqual(worker.ExposureScan(FAKE_TOKEN).area(area),
                             "incomplete: unreadable")
        with mock.patch.object(os, "scandir",
                               side_effect=PermissionError(13, "denied")):
            self.assertEqual(worker.ExposureScan(FAKE_TOKEN).area(area),
                             "incomplete: unreadable")
        self.assertIsNone(worker.ExposureScan(FAKE_TOKEN).area(area))

    def test_unlistable_directory_fails_the_run_closed(self):
        body = ("d=os.path.join(out,'locked');os.mkdir(d)\n"
                "open(os.path.join(d,'f'),'w').write('x');os.chmod(d,0)\n")
        self.addCleanup(lambda: (self.run_dir / "output/locked").exists()
                        and os.chmod(self.run_dir / "output/locked", 0o700))
        succeeded, manifest = self.research(body, credential=self.credential())
        self.assertFalse(succeeded)
        self.assertIn("credential-scan-incomplete: output unreadable", manifest["problems"])
        self.assertEqual(set(self.result_files()), {"manifest.json"})

    def test_manifest_that_would_hold_the_value_is_not_written(self):
        credential = self.credential()
        worker.prepare_run(self.run_dir, worker.build_policy(
            self.input, self.run_dir, self.bin / "claude"))
        self.assertFalse(worker.collect(
            self.run_dir, "claude-profile1", 0, result_json(), "",
            relay={"sources": [{"path": FAKE_TOKEN}], "port": None},
            credential=credential))
        self.assertNotIn("manifest.json", self.result_files())
        self.assert_not_recorded()


if __name__ == "__main__":
    unittest.main()
