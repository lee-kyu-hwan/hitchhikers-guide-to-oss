"""Check the OSS worker's launch gates, policy and result checks offline.

Every `claude`/`srt` here is a test-only fake. The fakes enforce nothing, so a
passing test proves the runner's contract, never the real sandbox boundary.
The real srt boundary is observed in test_oss_research_worker_runtime.py.
Credential files here hold fake sentinels, never a real token.
"""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "bin/oss-research-worker"
LOADER = importlib.machinery.SourceFileLoader("oss_research_worker", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
worker = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(worker)

DOCUMENTED_KEYS = {
    "filesystem": {"denyRead", "allowRead", "allowWrite", "denyWrite"},
    "network": {"allowedDomains", "deniedDomains", "allowUnixSockets",
                "allowAllUnixSockets", "allowLocalBinding"},
}
SENTINEL = "sentinel-must-not-reach-child"


FAKE_TOKEN = "sentinel-token-" + "x" * 32


def fake_runtime_prefix(root):
    """A prefix shaped like the pinned install; its tree is not the measured one."""
    prefix = root / "srt-prefix"
    package = prefix / "node_modules" / worker.SRT_PACKAGE
    (package / "dist").mkdir(parents=True)
    (package / "dist/cli.js").write_text("// fake\n")
    (package / "package.json").write_text(json.dumps(
        {"name": worker.SRT_PACKAGE, "version": worker.SRT_VERSION}))
    packages = {"": {}}
    packages.update({f"node_modules/{name}": {"version": version, "integrity": integrity}
                     for name, (version, integrity) in worker.SRT_LOCKED.items()})
    (prefix / "package-lock.json").write_text(json.dumps({"packages": packages}))
    return prefix


def write_credential(root, profile="claude-profile1", content=FAKE_TOKEN + "\n"):
    directory = root / "credentials" / profile
    directory.mkdir(parents=True, mode=0o700)
    credential = directory / worker.CREDENTIAL_NAME
    credential.write_text(content)
    credential.chmod(0o600)
    return credential


def write_executable(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="oss-research-worker-test-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "parent-home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh/id_ed25519").write_text(SENTINEL)
        (self.home / ".config/gh").mkdir(parents=True)
        (self.home / ".config/gh/hosts.yml").write_text(f"oauth_token: {SENTINEL}\n")
        self.input = self.root / "input"
        self.input.mkdir()
        self.prompt = self.input / "task.txt"
        self.prompt.write_text("Read the local corpus and summarize it.", encoding="utf-8")
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "capture.json"
        write_executable(self.bin / "claude", (
            "import json,os,sys\n"
            f"open({str(self.capture)!r},'w').write(json.dumps("
            "{'argv':sys.argv[1:],'env':dict(os.environ)}))\n"
        ))

    def launch(self, path, prompt=None, run_dir=None, extra=()):
        extra = tuple(extra)
        environment = {
            "PATH": f"{path}:/usr/bin:/bin", "HOME": str(self.home),
            "GH_TOKEN": SENTINEL, "SSH_AUTH_SOCK": str(self.root / "agent.sock"),
            "CLAUDE_CODE_OAUTH_TOKEN": SENTINEL,
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--profile", "claude-profile1",
             "--input-dir", str(self.input),
             "--prompt-file", str(prompt or self.prompt),
             "--run-dir", str(run_dir or self.run_dir), *extra],
            cwd=self.root, env=environment, capture_output=True, text=True,
        )


class LaunchGateTests(Fixture):
    """Run the real CLI; it must stop before any runtime or provider starts."""

    def test_missing_runtime_blocks_without_unisolated_fallback(self):
        result = self.launch(self.bin)
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn("srt (sandbox runtime) is not installed", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.run_dir.exists())

    def test_srt_on_path_is_never_used(self):
        marker = self.root / "srt-ran"
        write_executable(self.bin / "srt", f"open({str(marker)!r},'w')\n")
        result = self.launch(self.bin)
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn("no --runtime-prefix given", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.run_dir.exists())

    def test_unmeasured_runtime_tree_is_blocked(self):
        marker = self.root / "srt-ran"
        prefix = fake_runtime_prefix(self.root)
        write_executable(self.bin / "node", f"open({str(marker)!r},'w')\n")
        result = self.launch(self.bin, extra=("--runtime-prefix", str(prefix)))
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertIn("tree differs from the measured", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.run_dir.exists())

    def test_timeout_must_be_positive_and_finite(self):
        for value in ("0", "-1", "nan", "inf", "soon"):
            result = self.launch(self.bin, extra=("--timeout", value))
            self.assertEqual(result.returncode, 2, value)
            self.assertIn("--timeout", result.stderr)
            self.assertFalse(self.run_dir.exists())

    def test_prompt_outside_input_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("Read only")
        result = self.launch(self.bin, prompt=outside)
        self.assertEqual(result.returncode, worker.EXIT_BAD_INPUT)
        self.assertFalse(self.run_dir.exists())

    def test_symlink_inside_input_is_rejected(self):
        (self.input / "keys").symlink_to(self.home / ".ssh")
        result = self.launch(self.bin)
        self.assertEqual(result.returncode, worker.EXIT_BAD_INPUT)
        self.assertIn("symlink", result.stderr)

    def test_existing_or_overlapping_run_dir_is_rejected(self):
        self.run_dir.mkdir()
        self.assertEqual(self.launch(self.bin).returncode, worker.EXIT_BAD_INPUT)
        nested = self.launch(self.bin, run_dir=self.input / "run")
        self.assertEqual(nested.returncode, worker.EXIT_BAD_INPUT)

    def test_lock_with_other_dependency_versions_is_blocked(self):
        prefix = fake_runtime_prefix(self.root)
        lock = json.loads((prefix / "package-lock.json").read_text())
        lock["packages"]["node_modules/zod"]["version"] = "3.99.0"
        (prefix / "package-lock.json").write_text(json.dumps(lock))
        with self.assertRaisesRegex(worker.Blocked, "measured dependency versions"):
            worker.verify_runtime(prefix, str(self.bin))
        (prefix / "package-lock.json").write_text("{")
        with self.assertRaisesRegex(worker.Blocked, "lock or package file is unusable"):
            worker.verify_runtime(prefix, str(self.bin))

    def test_committed_lock_fixture_is_the_pinned_set(self):
        fixture = Path(__file__).resolve().parent / "fixtures/oss_research_worker/runtime"
        lock = json.loads((fixture / "package-lock.json").read_text())
        locked = {name.removeprefix("node_modules/"): (entry["version"], entry["integrity"])
                  for name, entry in lock["packages"].items() if name}
        self.assertEqual(locked, worker.SRT_LOCKED)
        self.assertEqual(json.loads((fixture / "package.json").read_text())["dependencies"],
                         {worker.SRT_PACKAGE: worker.SRT_VERSION})

    def test_measured_runtime_still_needs_a_credential_and_a_measured_launch(self):
        prefix = fake_runtime_prefix(self.root)
        write_executable(self.bin / "node", "")
        measured = worker.tree_sha256(prefix / "node_modules")
        original = worker.SRT_TREE_SHA256
        worker.SRT_TREE_SHA256 = measured
        self.addCleanup(setattr, worker, "SRT_TREE_SHA256", original)
        self.assertEqual(worker.verify_runtime(prefix, str(self.bin)),
                         [str(self.bin / "node"),
                          str(prefix.resolve() / "node_modules" / worker.SRT_PACKAGE
                              / "dist/cli.js")])
        # The macOS gate order; the Linux one is in test_oss_research_worker_linux.
        with self.assertRaisesRegex(worker.Blocked, "no credential binding for claude-profile1"):
            worker.require_runtime("claude-profile1", prefix, str(self.bin), platform="darwin")
        credential = write_credential(self.root)
        with self.assertRaises(worker.Blocked) as caught:
            worker.require_runtime("claude-profile1", prefix, str(self.bin), credential,
                                   platform="darwin")
        self.assertIn("no real launch has been measured", str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_input_containing_parent_home_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "parent HOME"):
            worker.checked_input(self.root, self.prompt, self.home)


class CredentialTests(Fixture):
    """The gate checks the file's metadata; load_credential (behind the gate)
    also reads the content. Every content here is a fake sentinel."""

    def assertBlocked(self, credential, message, profile="claude-profile1",
                      check=worker.checked_credential):
        with self.assertRaises(worker.Blocked) as caught:
            check(profile, credential, self.input, self.run_dir)
        self.assertIn(message, str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_private_file_bound_to_the_profile_is_accepted(self):
        worker.checked_credential("claude-profile1", write_credential(self.root),
                                  self.input, self.run_dir)

    def test_file_of_the_other_profile_is_refused(self):
        credential = write_credential(self.root, profile="claude-profile2")
        self.assertBlocked(credential, "<dir>/claude-profile1/oauth-token")

    def test_loose_permissions_are_refused(self):
        credential = write_credential(self.root)
        credential.chmod(0o644)
        self.assertBlocked(credential, "mode 600")
        credential.chmod(0o600)
        credential.parent.chmod(0o755)
        self.assertBlocked(credential, "mode 700")

    def test_symlinked_credential_is_refused(self):
        real = write_credential(self.root)
        link_dir = self.root / "links/claude-profile1"
        link_dir.mkdir(parents=True, mode=0o700)
        (link_dir / "oauth-token").symlink_to(real)
        self.assertBlocked(link_dir / "oauth-token", "must not be symlinks")

    def test_malformed_or_missing_content_is_refused(self):
        for content in ("", "short\n", FAKE_TOKEN + "\n" + FAKE_TOKEN + "\n",
                        FAKE_TOKEN + " trailing\n", "토큰" * 20):
            with self.subTest(content=len(content)):
                credential = write_credential(self.root, content=content)
                self.assertBlocked(credential, "single-line ASCII token",
                                   check=worker.load_credential)
                shutil.rmtree(self.root / "credentials")
        self.assertBlocked(self.root / "none/claude-profile1/oauth-token", "unusable")

    def test_credential_inside_input_is_refused(self):
        directory = self.input / "claude-profile1"
        directory.mkdir(mode=0o700)
        (directory / "oauth-token").write_text(FAKE_TOKEN)
        (directory / "oauth-token").chmod(0o600)
        self.assertBlocked(directory / "oauth-token", "outside the input and run")

    def test_cli_blocks_without_printing_the_token(self):
        prefix = fake_runtime_prefix(self.root)
        credential = write_credential(self.root)
        credential.chmod(0o644)
        result = self.launch(self.bin, extra=(
            "--runtime-prefix", str(prefix), "--credential-file", str(credential)))
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED)
        self.assertNotIn(FAKE_TOKEN, result.stderr + result.stdout)
        self.assertFalse(self.run_dir.exists())


class ContractTests(Fixture):
    def test_environment_is_an_allowlist(self):
        parent = {
            "PATH": "/usr/bin", "LANG": "ko_KR.UTF-8", "TERM": "xterm",
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "GH_TOKEN": SENTINEL, "GITHUB_TOKEN": SENTINEL,
            "SSH_AUTH_SOCK": "/tmp/agent.sock", "GIT_SSH_COMMAND": "ssh -i key",
            "CLAUDE_CODE_OAUTH_TOKEN": SENTINEL, "ANTHROPIC_API_KEY": SENTINEL,
            "AWS_PROFILE": "work", "ORCA_TERMINAL_HANDLE": "term_x",
            "ORCA_SOCKET": "/tmp/orca.sock",
        }
        environment = worker.build_environment(parent, self.run_dir)
        self.assertEqual(set(environment), {
            "PATH", "LANG", "TERM", "HOME", "TMPDIR", "CLAUDE_CONFIG_DIR"})
        self.assertEqual(environment["HOME"], str(self.run_dir / "home"))
        self.assertEqual(environment["TMPDIR"], str(self.run_dir / "tmp"))
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"],
                         str(self.run_dir / "home/.claude"))
        self.assertNotIn(SENTINEL, json.dumps(environment))
        self.assertNotIn(str(self.home), json.dumps(environment))

    def test_policy_denies_by_default_with_documented_keys(self):
        claude = self.bin / "claude"
        # The measured macOS policy; the Linux one is in test_oss_research_worker_linux.
        policy = worker.build_policy(self.input, self.run_dir, claude, platform="darwin")
        self.assertEqual(set(policy), set(DOCUMENTED_KEYS))
        for section, keys in DOCUMENTED_KEYS.items():
            self.assertEqual(set(policy[section]), keys)
        files = policy["filesystem"]
        self.assertEqual(files["denyRead"], ["/"])
        self.assertEqual(files["allowRead"], [*worker.SYSTEM_READ, str(self.input),
                                              str(self.run_dir), str(claude)])
        self.assertEqual(worker.SYSTEM_READ, ("/usr/share/icu", "/etc", "/private/etc/hosts"))
        for system in worker.SYSTEM_READ:
            self.assertFalse(system.startswith(("/Users", "/private/tmp", "/private/var/folders",
                                                "/Library", "/Volumes")), system)
        self.assertEqual(files["allowWrite"],
                         [str(self.run_dir / n) for n in ("home", "tmp", "output")])
        for protected in ("policy", "result"):
            self.assertIn(str(self.run_dir / protected), files["denyWrite"])
        network = policy["network"]
        self.assertEqual(network["allowedDomains"], ["api.anthropic.com"])
        self.assertEqual(network["deniedDomains"], [])
        self.assertEqual(network["allowUnixSockets"], [])
        self.assertIs(network["allowAllUnixSockets"], False)
        self.assertIs(network["allowLocalBinding"], False)

    def test_selection_is_passed_through_only_when_given(self):
        policy = self.run_dir / "policy/srt-settings.json"
        runtime = ["/opt/node", "/opt/cli.js"]
        plain = worker.build_command(runtime, policy, "/opt/claude", "prompt")
        self.assertNotIn("--model", plain)
        self.assertNotIn("--effort", plain)
        chosen = worker.build_command(runtime, policy, "/opt/claude", "prompt",
                                      selection=worker.Selection("user-model", "xhigh"))
        at = chosen.index("--model")
        self.assertEqual(chosen[at:at + 4], ["--model", "user-model", "--effort", "xhigh"])
        self.assertLess(at, chosen.index("--permission-mode"))
        self.assertEqual([a for a in chosen if a not in ("--model", "user-model", "--effort",
                                                         "xhigh")], plain)
        for staged in (worker.StagedRun("deep-research", self.root),
                       worker.StagedRun("corpus-extract", self.root)):
            with self.subTest(staged.profile):
                argv = worker.build_staged_command(
                    runtime, policy, "/opt/claude", "prompt", self.run_dir, staged,
                    selection=worker.Selection("user-model", "low"))
                at = argv.index("--model")
                self.assertEqual(argv[at:at + 4], ["--model", "user-model", "--effort", "low"])
                self.assertLess(at, argv.index("--permission-mode"))
                self.assertEqual(argv[-2:], ["--", "prompt"])

    def test_selection_values_are_checked(self):
        self.assertEqual(worker.EFFORT_LEVELS, ("low", "medium", "high", "xhigh", "max"))
        for model in ("opus", "claude-opus-5-5", "claude-opus-5-5[1m]", "claude-sonnet-5",
                      "vendor.model:v1", "fable", "x" * 128):
            worker.Selection(model, "high").check()
        for model in ("", "-p", "--model", "a b", "x\ny", "x\ty", "x\0y", "x\x7f",
                      "x" * 129, "모델", None):
            with self.subTest(model=model), self.assertRaises(ValueError):
                worker.Selection(model, "high").check()
        for effort in ("", "HIGH", "auto", "ultra", None):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                worker.Selection("opus", effort).check()

    def test_command_wraps_claude_in_runtime_with_read_tools(self):
        policy = self.run_dir / "policy/srt-settings.json"
        argv = worker.build_command(["/opt/node", "/opt/cli.js"], policy,
                                    "/opt/claude", "prompt")
        self.assertEqual(argv[:6], ["/opt/node", "/opt/cli.js", "--settings", str(policy),
                                    "--", "/opt/claude"])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        for required in ("--safe-mode", "--restricted", "--strict-mcp-config",
                         "--no-chrome", "--disable-slash-commands"):
            self.assertIn(required, argv)
        self.assertEqual(argv[-2:], ["--", "prompt"])

    def test_permission_mode_is_an_explicit_choice_defaulting_to_dont_ask(self):
        policy = self.run_dir / "policy/srt-settings.json"
        self.assertEqual(worker.PERMISSION_MODES, ("dontAsk", "auto"))
        for mode in worker.PERMISSION_MODES:
            argv = worker.build_command(["/opt/srt"], policy, "/opt/claude", "prompt", mode)
            self.assertEqual(argv.count("--permission-mode"), 1)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
            # Only the mode changes: the tool surface stays the same.
            default = worker.build_command(["/opt/srt"], policy, "/opt/claude", "prompt")
            self.assertEqual([a for a in argv if a != mode],
                             [a for a in default if a != "dontAsk"])
        for mode in ("default", "bypassPermissions", "acceptEdits", "plan", "Auto", ""):
            with self.assertRaisesRegex(ValueError, "permission mode"):
                worker.build_command(["/opt/srt"], policy, "/opt/claude", "prompt", mode)

    def test_cli_accepts_only_dont_ask_and_auto_is_refused_as_input(self):
        # #4 item 1 (2026-10-11): auto is refused at the CLI (exit 2), while
        # build_command keeps both modes for the measurement harness.
        for mode in ("bypassPermissions", "default", "plan", "auto"):
            result = self.launch(self.bin, extra=("--permission-mode", mode))
            self.assertEqual(result.returncode, 2, mode)
            self.assertIn("--permission-mode", result.stderr)
            self.assertFalse(self.run_dir.exists())
        self.assertFalse(self.capture.exists())
        result = self.launch(self.bin, extra=("--permission-mode", "dontAsk"))
        self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.run_dir.exists())


class SelectionGateTests(Fixture):
    """The CLI takes both values or neither and stays behind the closed gate."""

    def test_both_or_neither_and_checked_values(self):
        for extra, code in ((("--model", "opus"), worker.EXIT_BAD_INPUT),
                            (("--effort", "high"), worker.EXIT_BAD_INPUT),
                            (("--model", "-p", "--effort", "high"), worker.EXIT_BAD_INPUT),
                            (("--model", "opus", "--effort", "ultra"), 2),
                            (("--model", "opus", "--effort", "high"), worker.EXIT_BLOCKED)):
            with self.subTest(extra):
                result = self.launch(self.bin, extra=extra)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertFalse(self.capture.exists())
                self.assertFalse(self.run_dir.exists())

    def test_open_gate_without_selection_starts_nothing(self):
        # If the gate were opened, main still refuses to start a model run
        # without both values: nothing starts and no default model is used.
        prompt = self.input / "task.txt"
        argv = ["worker", "--profile", "claude-profile1", "--input-dir", str(self.input),
                "--prompt-file", str(self.prompt), "--run-dir", str(self.run_dir),
                "--relay-repo", "octo/demo"]
        opened = (["/opt/node", "/opt/cli.js"], str(self.bin / "claude"), None)
        with mock.patch.object(worker, "require_runtime", return_value=opened), \
                mock.patch.object(worker, "relay_launch") as relay, \
                mock.patch.object(worker, "run_research") as research, \
                mock.patch.object(worker.subprocess, "Popen") as popen, \
                mock.patch.object(worker.sys, "argv", argv), mock.patch("sys.stderr"):
            self.assertEqual(worker.main(), worker.EXIT_BLOCKED)
        for never in (relay, research, popen):
            never.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_blocked_from_the_run_itself_is_exit_3(self):
        argv = ["worker", "--profile", "claude-profile1", "--input-dir", str(self.input),
                "--prompt-file", str(self.prompt), "--run-dir", str(self.run_dir),
                "--model", "claude-opus-5-5[1m]", "--effort", "xhigh"]
        opened = (["/opt/node", "/opt/cli.js"], str(self.bin / "claude"), None)
        with mock.patch.object(worker, "require_runtime", return_value=opened), \
                mock.patch.object(worker, "run_research",
                                  side_effect=worker.Blocked("refused inside")) as research, \
                mock.patch.object(worker.sys, "argv", argv), mock.patch("sys.stderr"):
            self.assertEqual(worker.main(), worker.EXIT_BLOCKED)
        selection = research.call_args.args[-1]
        self.assertEqual(selection, worker.Selection("claude-opus-5-5[1m]", "xhigh"))

    def test_live_run_without_selection_is_blocked_before_anything(self):
        credential = worker.load_credential("claude-profile1", write_credential(self.root),
                                            self.input, self.run_dir)
        with mock.patch.object(worker, "run_supervised") as supervised:
            with self.assertRaisesRegex(worker.Blocked, "explicit --model and --effort"):
                worker.run_research(["/opt/node", "/opt/cli.js"], str(self.bin / "claude"),
                                    "prompt", self.input, self.run_dir, "claude-profile1", 20,
                                    None, {"PATH": "/usr/bin"}, credential=credential)
        supervised.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_manifest_keeps_requested_and_observed_apart(self):
        worker.prepare_run(self.run_dir, worker.build_policy(self.input, self.run_dir,
                                                             self.bin / "claude"))
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "permission_denials": [], "result": "# ok\n",
                             "modelUsage": {"reported-model-a": {}, "reported-model-b": {}}})
        self.assertTrue(worker.collect(self.run_dir, "claude-profile1", 0, result, "",
                                       selection=worker.Selection("user-model", "max")))
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        self.assertEqual((manifest["requested_model"], manifest["requested_effort"]),
                         ("user-model", "max"))
        self.assertEqual(manifest["observed_models"], ["reported-model-a", "reported-model-b"])

    def test_no_observed_models_without_a_report(self):
        worker.prepare_run(self.run_dir, worker.build_policy(self.input, self.run_dir,
                                                             self.bin / "claude"))
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "permission_denials": [], "result": "# ok\n"})
        worker.collect(self.run_dir, "claude-profile1", 0, result, "",
                       selection=worker.Selection("user-model", "max"))
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        self.assertNotIn("observed_models", manifest)
        self.assertEqual(manifest["requested_model"], "user-model")


class CollectTests(Fixture):
    """Drive launch/collect with a pass-through fake runtime (no isolation)."""

    def run_fake(self, stdout, exit_code=0, extra="", timeout=30, tamper=None):
        write_executable(self.bin / "srt", (
            "import os,sys\n"
            "arguments=sys.argv[1:]\n"
            "assert arguments[0]=='--settings' and arguments[2]=='--'\n"
            "os.execv(arguments[3],arguments[3:])\n"
        ))
        write_executable(self.bin / "claude", (
            "import os,sys\n" + extra +
            f"sys.stdout.write({stdout!r})\nraise SystemExit({exit_code})\n"
        ))
        policy = worker.prepare_run(
            self.run_dir, worker.build_policy(self.input, self.run_dir, self.bin / "claude"))
        command = worker.build_command(
            [str(self.bin / "srt")], policy, str(self.bin / "claude"), "prompt")
        environment = worker.build_environment({"PATH": "/usr/bin:/bin"}, self.run_dir)
        exit_code, out, err = worker.launch(command, environment, self.input, timeout)
        if tamper:
            tamper()
        succeeded = worker.collect(self.run_dir, "claude-profile1", exit_code, out, err)
        manifest_file = self.run_dir / "result/manifest.json"
        if not manifest_file.is_file() or manifest_file.is_symlink():
            return succeeded, None
        return succeeded, json.loads(manifest_file.read_text())

    @staticmethod
    def result(**overrides):
        value = {"type": "result", "subtype": "success", "is_error": False,
                 "permission_denials": [], "result": "# Summary\n"}
        value.update(overrides)
        return json.dumps(value)

    def test_valid_result_is_recorded_but_not_executed(self):
        marker = self.root / "report-ran"
        text = f"#!/bin/sh\ntouch {marker}\n"
        succeeded, manifest = self.run_fake(self.result(result=text))
        self.assertTrue(succeeded, manifest)
        self.assertEqual(manifest["status"], "succeeded")
        self.assertEqual(manifest["exit_code"], 0)
        report = self.run_dir / "result/report.md"
        self.assertEqual(report.read_text(), text)
        self.assertFalse(os.access(report, os.X_OK))
        self.assertFalse(marker.exists())
        self.assertEqual((self.run_dir / "policy/srt-settings.json").stat().st_mode & 0o777,
                         0o400)

    def assert_failed(self, succeeded, manifest, problem):
        self.assertFalse(succeeded)
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(any(problem in item for item in manifest["problems"]),
                        manifest["problems"])
        self.assertFalse((self.run_dir / "result/report.md").exists())

    def test_nonzero_exit_fails_even_with_success_json(self):
        self.assert_failed(*self.run_fake(self.result(), exit_code=1), "exit code 1")

    def test_invalid_json_fails(self):
        self.assert_failed(*self.run_fake("not json"), "not a JSON result")

    def test_error_result_fails(self):
        self.assert_failed(*self.run_fake(self.result(is_error=True)),
                           "not a success")

    def test_permission_denial_fails_even_with_exit_zero(self):
        denied = self.result(permission_denials=[{"tool_name": "Bash"}])
        self.assert_failed(*self.run_fake(denied), "permission_denials")

    def test_symlink_in_writable_area_fails(self):
        extra = (f"os.symlink({str(self.home)!r},"
                 f"{str(self.run_dir / 'output/escape')!r})\n")
        self.assert_failed(*self.run_fake(self.result(), extra=extra),
                           "symlink in output")

    def test_writable_area_root_replaced_by_symlink_fails(self):
        outside = self.root / "outside-empty"
        outside.mkdir()

        def swap():
            (self.run_dir / "output").rmdir()
            (self.run_dir / "output").symlink_to(outside)

        self.assert_failed(*self.run_fake(self.result(), tamper=swap),
                           "writable area output")

    def test_missing_writable_area_fails(self):
        self.assert_failed(
            *self.run_fake(self.result(), tamper=(self.run_dir / "tmp").rmdir),
            "writable area tmp")

    def test_redirected_result_directory_records_nothing(self):
        outside = self.root / "outside-result"
        outside.mkdir()

        def swap():
            (self.run_dir / "result").rmdir()
            (self.run_dir / "result").symlink_to(outside)

        succeeded, manifest = self.run_fake(self.result(), tamper=swap)
        self.assertFalse(succeeded)
        self.assertIsNone(manifest)
        self.assertEqual(list(outside.iterdir()), [])

    def test_replaced_policy_file_records_nothing(self):
        policy = self.run_dir / "policy/srt-settings.json"

        def swap():
            policy.unlink()
            policy.symlink_to(self.home / ".ssh/id_ed25519")

        succeeded, manifest = self.run_fake(self.result(), tamper=swap)
        self.assertFalse(succeeded)
        self.assertIsNone(manifest)
        self.assertEqual(list((self.run_dir / "result").iterdir()), [])

    def test_prefilled_result_directory_records_nothing(self):
        planted = self.run_dir / "result/report.md"
        succeeded, manifest = self.run_fake(
            self.result(), tamper=lambda: planted.write_text("planted"))
        self.assertFalse(succeeded)
        self.assertIsNone(manifest)
        self.assertEqual(planted.read_text(), "planted")

    def test_timeout_kills_process_group_and_fails(self):
        pid_file = self.root / "grandchild.pid"
        extra = (
            "import subprocess,time\n"
            "child=subprocess.Popen(['sleep','30'])\n"
            f"open({str(pid_file)!r},'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        succeeded, manifest = self.run_fake(self.result(), extra=extra, timeout=2)
        self.assert_failed(succeeded, manifest, "no exit code")
        grandchild = int(pid_file.read_text())
        waited = subprocess.run(["ps", "-p", str(grandchild), "-o", "stat="],
                                capture_output=True, text=True).stdout.strip()
        self.assertTrue(waited in ("", "Z"), waited)


if __name__ == "__main__":
    unittest.main()
