"""Backend adapters of the OSS research worker (issue #6, stage 1).

S1: with --backend claude (the default) the Claude argv, srt policy, child
environment and measured launch are byte for byte what main 74e49d6 built:
the sha256 constants below were taken from that revision (docs/development/
2026-10-11-6-research-backends/README.md) and the builders here must reproduce
them. S2: an unmeasured backend (codex, agy) is Blocked at the gate with the
credential unread and no run directory, and every adapter builder refuses to
build anything for it. The egress pin and the unit wrapper carry the run's
backend and pin only its names.

Every claude/srt/egress-pin here is a test-only fake; credential files hold
fake sentinels, never a real token.
"""

import contextlib
import hashlib
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

from tests import test_oss_research_egress_pin as egress_tests

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "bin/oss-research-worker"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


worker = load("oss_research_worker_for_backends", WORKER)
wrapper = load("oss_research_linux_run_for_backends", ROOT / "bin/oss-research-linux-run")
pin = egress_tests.pin

RUNTIME = ["/opt/node", "/opt/srt/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"]
POLICY_FILE = Path("/run/policy/srt-settings.json")
FAKE_TOKEN = "sentinel-token-" + "x" * 32
SELECTION = worker.Selection("claude-opus-5-5", "medium")
# sha256 of json.dumps(value, sort_keys=True) at main 74e49d6 (before backends).
# The Linux policies are hashed with the host-dependent parts fixed (host_free):
# linux_link_targets resolves /usr/bin/socat and the rest on the running host.
FROZEN = {
    "argv_read_only": "eaea845d13e1b4823f9afb6d8282b903953b6b0fddf86a443246bf25550aa268",
    "argv_read_only_selection": "c9895e337a92e350515cd1bfc09ac6a5effe231c385b22c312236d484a28558b",
    "argv_auto_selection": "9fa50ee37057cafe6d1b9dc6634f22416b050fe8ca7580c6e242cf2b9021cc31",
    "argv_corpus-extract": "9241254294db549b22407052d4176d3ff546e0da63cdab5a79dd82af9a1e893b",
    "argv_deep-research": "b33b52b69921986ee928d6f091c5158641513a819779f63e8ef4d3a1b1a785f2",
    "policy_darwin": "ce16b43a330890893bc41dd5b1adff1796e9e42bed48d1836fdf4083a1761266",
    "policy_darwin_relay": "ab9dfc392216135505c87d20195406f1cddb346d8d3627355d11c53152b1a4d0",
    "policy_darwin_noai": "5782da23d5ac5e0a0d6dfa84d43bf6a53dabc936fa65f87661613066446bda70",
    "policy_linux": "f54d04174a569b3749ea82cc4face4aff711a65a2de58dd985ae99c8696a034f",
    "policy_linux_relay": "ac1308060005e029cc17a1dff37e910a010d4ccffa7c051849a1144046568319",
    "policy_linux_noai": "6205e1e28ad15a2cab4cfacd2254bf6372224d784afea776b96707422c671296",
    "policy_linux_staged": "6319ce4b3a16893e4439d37db77c40b160dd2f1a8cbdd8c31fb52d0041c83389",
    "env_darwin": "f6de7a5e151c61e5da6d1443465e96156df348a94cdda625ae6bc7ccdc468c35",
    "env_darwin_cred": "e76bf1e9f9235a4286029e7c070aa608ba9f5940fb316772636259456481f8f7",
    "env_linux": "f6de7a5e151c61e5da6d1443465e96156df348a94cdda625ae6bc7ccdc468c35",
    "env_linux_cred": "e76bf1e9f9235a4286029e7c070aa608ba9f5940fb316772636259456481f8f7",
    # The eleven fields of the measured Linux launch (backend was added after).
    "measured_linux": "f944439cf6914249855e1d863f024e722b4a2c8811d2fee8430afd615fa6bc3d",
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def staged(profile):
    domains = ("example.org",) if profile == "deep-research" else ()
    return worker.StagedRun(profile, Path("/src"), domains)


class BackendTableTests(unittest.TestCase):
    def test_backends_are_claude_codex_agy_and_claude_is_the_default(self):
        self.assertEqual(tuple(worker.BACKENDS), ("claude", "codex", "agy"))
        self.assertEqual(worker.DEFAULT_BACKEND, "claude")
        self.assertIs(worker.BACKENDS["claude"], worker.CLAUDE)
        for name, backend in worker.BACKENDS.items():
            self.assertEqual((backend.name, backend.executable), (name, name))

    def test_claude_backend_is_the_module_constants(self):
        claude = worker.CLAUDE
        self.assertEqual(claude.credential_name, worker.CREDENTIAL_NAME)
        self.assertEqual(claude.credential_variable, worker.CREDENTIAL_VARIABLE)
        self.assertEqual(claude.credential_variable, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual((claude.config_variable, claude.config_subdir),
                         ("CLAUDE_CONFIG_DIR", "home/.claude"))
        self.assertEqual(claude.allowed_domains, worker.ALLOWED_DOMAINS)
        self.assertEqual(claude.allowed_domains_linux, worker.ALLOWED_DOMAINS_LINUX)
        self.assertEqual(claude.pinned_names, ("api.anthropic.com",))
        for field in worker.Backend._fields:
            self.assertIsNotNone(getattr(claude, field), field)
            self.assertEqual(claude.require(field), getattr(claude, field))

    def test_unmeasured_backends_have_only_a_name_and_an_executable(self):
        for backend in (worker.CODEX, worker.AGY):
            for field in worker.Backend._fields:
                if field in ("name", "executable"):
                    continue
                with self.subTest(backend=backend.name, field=field):
                    self.assertIsNone(getattr(backend, field))
                    with self.assertRaisesRegex(worker.Blocked, f"backend {backend.name} has no measured"):
                        backend.require(field)

    def test_measured_launch_and_launch_request_name_the_claude_backend(self):
        scope = worker.MEASURED_LAUNCHES["linux"]
        self.assertEqual(scope.backend, "claude")
        self.assertEqual(scope._fields[-1], "backend")
        self.assertEqual(digest(list(scope)[:11]), FROZEN["measured_linux"])
        self.assertEqual(worker.MeasuredLaunch._field_defaults, {"backend": "claude"})
        request = worker.LaunchRequest("read-only", "dontAsk", False, SELECTION, ())
        self.assertEqual(request.backend, "claude")
        self.assertEqual(request._replace(backend="codex").backend, "codex")


class ClaudePathUnchangedTests(unittest.TestCase):
    """S1: the Claude builders reproduce main's hashes, with and without backend=CLAUDE."""

    def test_argv_hashes_match_main(self):
        built = {
            "argv_read_only": worker.build_command(RUNTIME, POLICY_FILE, "/opt/claude", "prompt"),
            "argv_read_only_selection": worker.build_command(
                RUNTIME, POLICY_FILE, "/opt/claude", "prompt", "dontAsk", SELECTION),
            "argv_auto_selection": worker.build_command(
                RUNTIME, POLICY_FILE, "/opt/claude", "prompt", "auto", SELECTION),
        }
        for profile in ("corpus-extract", "deep-research"):
            built[f"argv_{profile}"] = worker.build_staged_command(
                RUNTIME, POLICY_FILE, "/opt/claude", "prompt", Path("/run"), staged(profile),
                "dontAsk", SELECTION)
        for key, argv in built.items():
            with self.subTest(key):
                self.assertEqual(digest(argv), FROZEN[key])
        explicit = worker.build_command(RUNTIME, POLICY_FILE, "/opt/claude", "prompt", "dontAsk",
                                        SELECTION, backend=worker.CLAUDE)
        self.assertEqual(explicit, built["argv_read_only_selection"])
        explicit = worker.build_staged_command(
            RUNTIME, POLICY_FILE, "/opt/claude", "prompt", Path("/run"), staged("deep-research"),
            "dontAsk", SELECTION, backend=worker.CLAUDE)
        self.assertEqual(explicit, built["argv_deep-research"])

    @staticmethod
    def host_free():
        """build_policy on Linux adds the host's resolved symlink targets and
        socat path; both are pinned here so the hash is the same on every host."""
        return (mock.patch.object(worker, "linux_link_targets", lambda paths: []),
                mock.patch.object(worker.os.path, "realpath", lambda path: str(path)))

    def test_policy_hashes_match_main(self):
        arguments = (Path("/input"), Path("/run"), Path("/opt/claude"))
        for platform in ("darwin", "linux"):
            targets, realpath = self.host_free()
            with targets, realpath:
                built = {
                    f"policy_{platform}": worker.build_policy(*arguments, None, platform=platform),
                    f"policy_{platform}_relay": worker.build_policy(*arguments, 4321,
                                                                    platform=platform),
                    f"policy_{platform}_noai": worker.build_policy(*arguments, None, model=False,
                                                                   platform=platform),
                }
                explicit = worker.build_policy(*arguments, None, platform=platform,
                                               backend=worker.CLAUDE)
            for key, policy in built.items():
                with self.subTest(key):
                    self.assertEqual(digest(policy), FROZEN[key])
            self.assertEqual(explicit, built[f"policy_{platform}"])
        targets, realpath = self.host_free()
        with targets, realpath:
            with_staged = worker.build_policy(*arguments, None, staged=staged("deep-research"),
                                              platform="linux")
            explicit = worker.build_policy(*arguments, None, staged=staged("deep-research"),
                                           platform="linux", backend=worker.CLAUDE)
        self.assertEqual(digest(with_staged), FROZEN["policy_linux_staged"])
        self.assertEqual(explicit, with_staged)

    def test_environment_hashes_match_main(self):
        parent = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/x", "GH_TOKEN": "s"}
        credential = worker.Credential("claude-profile2", FAKE_TOKEN)
        for platform in ("darwin", "linux"):
            plain = worker.build_environment(parent, Path("/run"), None, platform=platform)
            with_credential = worker.build_environment({"PATH": "/usr/bin:/bin"}, Path("/run"),
                                                       credential, platform=platform)
            with self.subTest(platform):
                self.assertEqual(digest(plain), FROZEN[f"env_{platform}"])
                self.assertEqual(digest(with_credential), FROZEN[f"env_{platform}_cred"])
                self.assertEqual(worker.build_environment(parent, Path("/run"), None,
                                                          platform=platform, backend=worker.CLAUDE),
                                 plain)
            self.assertEqual(plain["CLAUDE_CONFIG_DIR"], "/run/home/.claude")
            self.assertEqual(with_credential["CLAUDE_CODE_OAUTH_TOKEN"], FAKE_TOKEN)

    def test_manifest_names_the_backend(self):
        root = Path(tempfile.mkdtemp(prefix="osr-backends-", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        inputs, run_dir = root / "input", root / "run"
        inputs.mkdir()
        worker.prepare_run(run_dir, worker.build_policy(inputs, run_dir, root / "claude"))
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "permission_denials": [], "result": "OK",
                             "modelUsage": {"claude-opus-5-5": {}}})
        credential = worker.Credential("claude-profile1", FAKE_TOKEN)
        self.assertTrue(worker.collect(run_dir, "claude-profile1", 0, result, "",
                                       credential=credential, selection=SELECTION))
        manifest = json.loads((run_dir / "result/manifest.json").read_text())
        self.assertEqual(manifest["backend"], "claude")
        self.assertEqual(manifest["observed_models"], ["claude-opus-5-5"])
        self.assertEqual(manifest["credential"], {"variable": "CLAUDE_CODE_OAUTH_TOKEN",
                                                  "exact_value_check": "clean"})
        self.assertNotIn(FAKE_TOKEN, json.dumps(manifest))


class UnmeasuredBackendGateTests(unittest.TestCase):
    """S2: codex and agy are Blocked at the gate; the token stays unread; no run directory."""

    GOOD = worker.LaunchRequest("read-only", "dontAsk", False, SELECTION, ())

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-backends-", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        self.prompt = self.input / "task.txt"
        self.prompt.write_text("summarize", encoding="utf-8")
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "capture.json"
        for name in ("claude", "codex", "agy"):
            tool = self.bin / name
            tool.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                            f"open({str(self.capture)!r},'w').write(json.dumps(sys.argv))\n")
            tool.chmod(0o755)
        directory = self.root / "credentials/claude-profile2"
        directory.mkdir(parents=True, mode=0o700)
        self.credential = directory / worker.CREDENTIAL_NAME
        self.credential.write_text(FAKE_TOKEN + "\n")
        self.credential.chmod(0o600)

    def test_measured_launch_problems_name_the_backend_first(self):
        for name in ("codex", "agy"):
            problems = worker.measured_launch_problems(
                "linux", "claude-profile2", self.GOOD._replace(backend=name))
            self.assertEqual(problems, [f"backend {name} is not the measured claude"])
        self.assertEqual(worker.measured_launch_problems("linux", "claude-profile2", self.GOOD), [])

    def gate(self, platform, backend, launch):
        with mock.patch.object(worker, "verify_runtime", return_value=list(RUNTIME)), \
                mock.patch.object(worker, "platform_problems", return_value=[]), \
                mock.patch.object(worker, "checked_credential",
                                  wraps=worker.checked_credential) as checked, \
                mock.patch.object(worker, "load_credential",
                                  wraps=worker.load_credential) as load, \
                mock.patch.object(worker.subprocess, "Popen") as popen:
            with self.assertRaises(worker.Blocked) as blocked:
                worker.require_runtime("claude-profile2", self.root / "prefix", str(self.bin),
                                       self.credential, (self.input, self.run_dir),
                                       platform=platform, launch=launch, backend=backend)
        popen.assert_not_called()
        self.assertEqual(load.call_count, 0)
        self.assertNotIn(FAKE_TOKEN, str(blocked.exception))
        self.assertFalse(self.run_dir.exists())
        return str(blocked.exception), checked.call_count

    def test_unmeasured_backend_is_blocked_with_the_token_unread(self):
        for name in ("codex", "agy"):
            backend = worker.BACKENDS[name]
            for platform in ("linux", "darwin"):
                with self.subTest(backend=name, platform=platform):
                    message, _ = self.gate(platform, backend, self.GOOD._replace(backend=name))
                    self.assertIn("isolated launch is closed", message)
                    if platform == "linux":
                        self.assertIn(f"backend {name} is not the measured claude", message)
        # On Linux the unmeasured read set closes the gate before the credential
        # file is even looked at.
        _, looked = self.gate("linux", worker.CODEX, self.GOOD._replace(backend="codex"))
        self.assertEqual(looked, 0)

    def test_request_and_adapter_must_name_the_same_backend(self):
        message, looked = self.gate("linux", worker.CLAUDE, self.GOOD._replace(backend="codex"))
        self.assertIn("names backend codex, not claude", message)
        self.assertEqual(looked, 0)
        message, _ = self.gate("linux", worker.CODEX, self.GOOD)
        self.assertIn("names backend claude, not codex", message)

    def test_main_hands_the_backend_to_the_gate_and_makes_no_run_dir(self):
        seen = {}

        def gate(*arguments, **options):
            seen.update(options)
            raise worker.Blocked("stop here")
        base = ["worker", "--profile", "claude-profile2", "--input-dir", str(self.input),
                "--prompt-file", str(self.prompt), "--run-dir", str(self.run_dir),
                "--credential-file", str(self.credential),
                "--model", "claude-opus-5-5", "--effort", "medium"]
        for extra, expected in (([], "claude"), (["--backend", "claude"], "claude"),
                                (["--backend", "codex"], "codex"), (["--backend", "agy"], "agy")):
            with self.subTest(extra=extra):
                seen.clear()
                environment = {k: v for k, v in os.environ.items() if k != worker.UNIT_ENV_MARKER}
                with mock.patch.object(sys, "argv", base + extra), \
                        mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(worker, "require_runtime", side_effect=gate), \
                        mock.patch.object(worker, "relay_launch") as relay, \
                        mock.patch.object(worker, "run_research") as research, \
                        mock.patch.object(sys, "stderr"):
                    self.assertEqual(worker.main(), worker.EXIT_BLOCKED)
                self.assertEqual(seen["launch"].backend, expected)
                self.assertIs(seen["backend"], worker.BACKENDS[expected])
                relay.assert_not_called()
                research.assert_not_called()
                self.assertFalse(self.run_dir.exists())
                self.assertFalse(self.capture.exists())

    def test_cli_takes_the_three_backends_and_blocks_the_unmeasured_ones(self):
        environment = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root / "home"),
                       "CLAUDE_CODE_OAUTH_TOKEN": "sentinel-must-not-reach-child"}
        base = [sys.executable, "-B", str(WORKER), "--profile", "claude-profile2",
                "--input-dir", str(self.input), "--prompt-file", str(self.prompt),
                "--run-dir", str(self.run_dir), "--credential-file", str(self.credential),
                "--model", "claude-opus-5-5", "--effort", "medium"]
        for extra, code in ((["--backend", "codex"], worker.EXIT_BLOCKED),
                            (["--backend", "agy"], worker.EXIT_BLOCKED),
                            (["--backend", "claude"], worker.EXIT_BLOCKED),
                            ([], worker.EXIT_BLOCKED),
                            (["--backend", "gemini"], worker.EXIT_BAD_INPUT),
                            (["--backend", ""], worker.EXIT_BAD_INPUT)):
            with self.subTest(extra=extra):
                result = subprocess.run(base + extra, cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertFalse(self.run_dir.exists())
                self.assertFalse(self.capture.exists())
                self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
                if code == worker.EXIT_BAD_INPUT:
                    self.assertIn("--backend", result.stderr)

    def test_adapters_without_measurements_build_nothing(self):
        parent = {"PATH": "/usr/bin:/bin"}
        for backend in (worker.CODEX, worker.AGY):
            with self.subTest(backend.name):
                with self.assertRaisesRegex(worker.Blocked, "no measured argv"):
                    worker.build_command(RUNTIME, POLICY_FILE, "/opt/x", "prompt", backend=backend)
                with self.assertRaisesRegex(worker.Blocked, "no measured argv"):
                    worker.build_staged_command(RUNTIME, POLICY_FILE, "/opt/x", "prompt",
                                                Path("/run"), staged("corpus-extract"),
                                                backend=backend)
                with self.assertRaisesRegex(worker.Blocked, "no measured config"):
                    worker.build_environment(parent, Path("/run"), backend=backend)
                with self.assertRaisesRegex(worker.Blocked, "no measured allowed domains"):
                    worker.build_policy(Path("/input"), Path("/run"), Path("/opt/x"),
                                        platform="linux", backend=backend)
                result = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                     "permission_denials": [], "result": "OK",
                                     "modelUsage": {"some-model": {}}})
                problems, report = worker.result_problems(0, result, backend)
                self.assertIn(f"backend {backend.name} has no measured result parser", problems)
                self.assertIsNone(report)
                self.assertIsNone(worker.observed_models(result, backend))
                with mock.patch.object(worker, "run_supervised") as supervised:
                    with self.assertRaisesRegex(worker.Blocked, "no measured argv"):
                        worker.run_research(RUNTIME, "/opt/x", "prompt", self.input, self.run_dir,
                                            "claude-profile2", 20, None, parent,
                                            selection=SELECTION, backend=backend)
                supervised.assert_not_called()
                self.assertFalse(self.run_dir.exists())
        # The non-AI probe policy needs no backend endpoint: the collection
        # entries build it with the default backend and model=False.
        policy = worker.build_policy(Path("/input"), Path("/run"), Path("/opt/x"), model=False,
                                     platform="linux", backend=worker.CODEX)
        self.assertEqual(policy["network"]["allowedDomains"], [])


class EgressPinBackendTests(unittest.TestCase):
    """The pin resolves GitHub plus the run's backend endpoint, nothing else."""

    def test_names_per_backend(self):
        self.assertEqual(pin.PINNED_NAMES, ("api.github.com", "api.anthropic.com"))
        self.assertEqual(pin.pinned_names("claude"), pin.PINNED_NAMES)
        self.assertEqual(pin.pinned_names("none"), ("api.github.com",))
        self.assertEqual(pin.BACKEND_NAMES, ("claude", "codex", "agy", "none"))
        for name in ("codex", "agy"):
            with self.assertRaisesRegex(pin.PinError, f"no egress names have been measured for backend {name}"):
                pin.pinned_names(name)
        for name in ("gemini", "", None, 3):
            with self.subTest(name), self.assertRaisesRegex(pin.PinError, "backend must be one of"):
                pin.pinned_names(name)

    def test_default_apply_is_the_claude_pin_and_records_the_backend(self):
        fixture = egress_tests.Fixture(self)
        result = fixture.apply()
        self.assertEqual(result["backend"], "claude")
        manifest = json.loads(fixture.env.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["backend"], "claude")
        self.assertEqual(manifest["schema_version"], "1.1.0")
        self.assertEqual(sorted(manifest["pins"]), ["api.anthropic.com", "api.github.com"])
        self.assertEqual({(ip, 443) for ip in egress_tests.GITHUB + egress_tests.ANTHROPIC},
                         fixture.nft.elements)
        self.assertTrue(pin.verify(fixture.env, "run-1")["ok"])

    def test_none_pins_github_alone(self):
        fixture = egress_tests.Fixture(self)
        result = pin.apply(fixture.env, "run-1", 600, fixture.resolver.address, "none")
        self.assertEqual((result["status"], result["backend"]), ("pinned", "none"))
        manifest = json.loads(fixture.env.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["pins"], {"api.github.com": egress_tests.GITHUB})
        self.assertEqual({(ip, 443) for ip in egress_tests.GITHUB}, fixture.nft.elements)
        self.assertEqual(fixture.resolver.counts, {"api.github.com": 2})
        hosts = fixture.hosts.read_text(encoding="utf-8")
        self.assertNotIn("api.anthropic.com", hosts)
        check = pin.verify(fixture.env, "run-1")
        self.assertEqual((check["ok"], check["problems"]), (True, []))

    def test_unmeasured_backend_is_refused_before_anything_changes(self):
        fixture = egress_tests.Fixture(self)
        before = fixture.hosts.read_text(encoding="utf-8")
        for name in ("codex", "agy", "gemini"):
            with self.subTest(name), self.assertRaises(pin.PinError):
                pin.apply(fixture.env, "run-1", 600, fixture.resolver.address, name)
        self.assertEqual(fixture.nft.calls, [])
        self.assertEqual(fixture.resolver.counts, {})
        self.assertFalse(fixture.env.manifest_path.exists())
        self.assertEqual(fixture.hosts.read_text(encoding="utf-8"), before)

    def test_manifest_backend_is_checked_against_its_pins(self):
        fixture = egress_tests.Fixture(self)
        fixture.apply()
        path = fixture.env.manifest_path
        original = json.loads(path.read_text(encoding="utf-8"))
        for value, text in (("none", "pins must name exactly api.github.com"),
                            ("codex", "manifest backend refused"),
                            ("gemini", "manifest backend refused"),
                            (None, "manifest backend refused")):
            with self.subTest(value):
                data = dict(original, backend=value)
                path.write_text(json.dumps(data), encoding="utf-8")
                problems = pin.verify(fixture.env, "run-1")["problems"]
                self.assertTrue(any(text in item for item in problems), problems)
        data = dict(original)
        del data["backend"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(pin.verify(fixture.env, "run-1")["ok"])

    def test_cli_takes_the_backend_for_apply_only(self):
        parser = pin.build_parser()
        options = parser.parse_args(["apply", "--run-id", "r", "--ttl", "600",
                                     "--resolver", "127.0.0.1"])
        self.assertEqual(options.backend, "claude")
        options = parser.parse_args(["apply", "--run-id", "r", "--ttl", "600",
                                     "--resolver", "127.0.0.1", "--backend", "none"])
        self.assertEqual(options.backend, "none")
        with mock.patch.object(sys, "stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["apply", "--run-id", "r", "--ttl", "600",
                               "--resolver", "127.0.0.1", "--backend", "gemini"])
        with mock.patch.object(sys, "stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["verify", "--backend", "claude"])
        env = pin.Env(require_root=False, nft=lambda args: self.fail("nft must not run"))
        with contextlib.redirect_stdout(None):
            self.assertEqual(2, pin.main(["apply", "--run-id", "r", "--ttl", "600",
                                          "--resolver", "127.0.0.1", "--backend", "codex"], env=env))


FAKE_PIN = r"""
import json, pathlib, sys
here = pathlib.Path(__file__).parent
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
run_id = sys.argv[sys.argv.index("--run-id") + 1]
backend = sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else None
echo = (here / "echo-backend").read_text().strip() if (here / "echo-backend").exists() else backend
expires = (here / "expires").read_text().strip()
if sys.argv[1] == "apply":
    print(json.dumps({"status": "pinned", "run_id": run_id, "backend": echo, "expires_at": expires,
                      "manifest_sha256": "sha256:" + "a" * 64, "nft_elements": ["1.2.3.4 . 443"]}))
elif sys.argv[1] == "verify":
    print(json.dumps({"ok": True, "problems": [], "run_id": run_id, "expires_at": expires,
                      "manifest_sha256": "sha256:" + "a" * 64}))
else:
    print(json.dumps({"status": "cleared", "closed": True}))
"""


class WrapperBackendTests(unittest.TestCase):
    """The unit wrapper reads the worker's --backend and opens the pin for it."""

    UNIT = "oss-research-0123456789abcdef"

    def test_worker_backend_follows_the_worker_argument(self):
        base = ["--profile", "claude-profile2", "--model", "claude-opus-5-5", "--effort", "medium"]
        self.assertEqual(wrapper.worker_backend(worker, "worker", base), "claude")
        self.assertEqual(wrapper.worker_backend(worker, "worker", base + ["--backend", "claude"]), "claude")
        self.assertEqual(wrapper.worker_backend(worker, "worker", base + ["--backend", "codex"]), "codex")
        self.assertEqual(wrapper.worker_backend(worker, "worker", ["--backend=agy", *base]), "agy")
        for entry in ("collect", "collect-issues", "candidate-reads", "verify"):
            self.assertEqual(wrapper.worker_backend(worker, entry, base + ["--backend", "codex"]), "none")
        for bad in (base + ["--backend"], base + ["--backend", "gemini"], ["--backend="],
                    base + ["--backend", "codex", "--backend", "claude"]):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "--backend"):
                wrapper.worker_backend(worker, "worker", bad)
        self.assertEqual(wrapper.NO_MODEL_BACKEND, "none")
        self.assertEqual(sorted(wrapper.worker_backend(worker, "worker", ["--backend", name])
                                for name in worker.BACKENDS), sorted(worker.BACKENDS))

    def test_verify_entry_takes_the_wrappers_own_backend(self):
        """PR #21 left the verification entry at none, so a measurement harness run
        (model-once, model-auto, model-staged) under --egress pin had no model
        endpoint. The wrapper's own --backend names it, for that entry only."""
        inner = ["model-once", "/o", "/c/claude-profile2/oauth-token", "claude-opus-5-5", "medium", "300"]
        self.assertEqual(wrapper.worker_backend(worker, "verify", inner), "none")
        self.assertEqual(wrapper.worker_backend(worker, "verify", inner, None), "none")
        for name in (*worker.BACKENDS, "none"):
            self.assertEqual(wrapper.worker_backend(worker, "verify", inner, name), name)
        # The inner's own arguments never name it: only the wrapper option does.
        self.assertEqual(wrapper.worker_backend(worker, "verify", inner + ["--backend", "claude"]), "none")
        for bad in ("gemini", "", "Claude", "claude,codex"):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "--backend must be one of"):
                wrapper.worker_backend(worker, "verify", inner, bad)
        # Any other entry refuses the wrapper option: the worker names its backend in its
        # own arguments, and the non-AI entries run no model.
        base = ["--profile", "claude-profile2", "--model", "claude-opus-5-5", "--effort", "medium"]
        with self.assertRaisesRegex(ValueError, "verify only.*reads it from the worker arguments"):
            wrapper.worker_backend(worker, "worker", base, "claude")
        for entry in ("collect", "collect-issues", "candidate-reads"):
            with self.subTest(entry), self.assertRaisesRegex(ValueError, "verify only.*runs no model"):
                wrapper.worker_backend(worker, entry, ["--out-dir", "/o"], "none")
            self.assertEqual(wrapper.worker_backend(worker, entry, ["--out-dir", "/o"]), "none")

    def wrapper_main(self, *options):
        """main past the trust checks (mocked), up to launch (recorded, not run)."""
        launches = []

        def fake_launch(worker_module, entry, seconds, egress, source, python, environment,
                        arguments, resolver, backend=None):
            launches.append({"entry": entry, "egress": egress, "arguments": arguments,
                             "backend": backend})
            return {"unit": self.UNIT, "entry": entry, "egress": egress, "backend": backend,
                    "exit_code": 0, "failures": []}

        argv = ["linux-run", "--entry", "verify", "--timeout", "10", *options, "--", "model-once", "/o"]
        with mock.patch.object(wrapper.sys, "platform", "linux"), \
                mock.patch.object(wrapper.os, "geteuid", return_value=wrapper.TRUSTED_OWNER), \
                mock.patch.object(wrapper.sys, "argv", argv), \
                mock.patch.object(wrapper, "bootstrap", return_value=(Path("/src"), [])), \
                mock.patch.object(wrapper, "research_user_problems", return_value=[]), \
                mock.patch.object(wrapper, "load_worker", return_value=worker), \
                mock.patch.object(wrapper, "unit_environment", return_value={"PATH": "/usr/bin"}), \
                mock.patch.object(wrapper, "acquire_run_lock"), \
                mock.patch.object(wrapper, "clock_guard", return_value={"action": "none"}), \
                mock.patch.object(wrapper, "clock_after_run", return_value={}), \
                mock.patch.object(wrapper, "launch", side_effect=fake_launch), \
                contextlib.redirect_stdout(None), mock.patch("sys.stderr"):
            try:
                code = wrapper.main()
            except SystemExit as stop:
                code = stop.code
        return code, launches

    def test_main_hands_the_verify_backend_to_the_launch_or_refuses_it(self):
        code, launches = self.wrapper_main("--backend", "claude")
        self.assertEqual((code, len(launches)), (0, 1))
        self.assertEqual(launches[0]["backend"], "claude")
        self.assertEqual(launches[0]["arguments"], ["model-once", "/o"])
        code, launches = self.wrapper_main()
        self.assertEqual((code, [l["backend"] for l in launches]), (0, ["none"]))
        for bad in (("--backend", "gemini"), ("--backend", ""), ("--backend",)):
            with self.subTest(bad):
                code, launches = self.wrapper_main(*bad)
                self.assertEqual((code, launches), (wrapper.EXIT_BAD_INPUT, []))
        # The worker entry with the wrapper option: refused as input, nothing launched.
        argv = ["linux-run", "--entry", "worker", "--timeout", "10", "--backend", "claude", "--",
                "--profile", "claude-profile2"]
        with mock.patch.object(wrapper.sys, "platform", "linux"), \
                mock.patch.object(wrapper.os, "geteuid", return_value=wrapper.TRUSTED_OWNER), \
                mock.patch.object(wrapper.sys, "argv", argv), \
                mock.patch.object(wrapper, "bootstrap", return_value=(Path("/src"), [])), \
                mock.patch.object(wrapper, "research_user_problems", return_value=[]), \
                mock.patch.object(wrapper, "load_worker", return_value=worker), \
                mock.patch.object(wrapper, "unit_environment", return_value={"PATH": "/usr/bin"}), \
                mock.patch.object(wrapper, "launch", side_effect=AssertionError("launched")), \
                mock.patch("sys.stderr"):
            self.assertEqual(wrapper.main(), wrapper.EXIT_BAD_INPUT)

    def test_open_egress_pins_the_given_backend(self):
        root = Path(tempfile.mkdtemp(prefix="osr-backends-pin-")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tool = root / "egress-pin"
        tool.write_text(FAKE_PIN)
        import time
        (root / "expires").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime(time.time() + 3000)))
        python = Path(sys.executable)
        for backend in ("claude", "none", "codex"):
            with self.subTest(backend):
                (root / "calls.jsonl").unlink(missing_ok=True)
                evidence, problems, record = wrapper.open_egress(
                    tool, python, self.UNIT, 100, "127.0.0.53", backend=backend)
                self.assertEqual(problems, [])
                self.assertEqual(evidence["run_id"], self.UNIT)
                self.assertEqual(record["backend"], backend)
                self.assertEqual(record["apply"]["backend"], backend)
                argv = json.loads((root / "calls.jsonl").read_text().splitlines()[0])
                self.assertEqual(argv, ["apply", "--run-id", self.UNIT, "--ttl", "140",
                                        "--resolver", "127.0.0.53", "--backend", backend])
        # A tool that pinned another backend than asked is no evidence.
        (root / "echo-backend").write_text("claude\n")
        (root / "calls.jsonl").unlink(missing_ok=True)
        evidence, problems, record = wrapper.open_egress(tool, python, self.UNIT, 100, "127.0.0.53",
                                                         backend="none")
        self.assertIsNone(evidence)
        self.assertEqual(problems, ["egress-apply-failed: exit 0"])
        self.assertEqual([json.loads(line)[0] for line in (root / "calls.jsonl").read_text().splitlines()],
                         ["apply"])


if __name__ == "__main__":
    unittest.main()
