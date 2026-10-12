"""The Codex backend of the OSS research worker (issue #6, stage 2), offline.

Codex CLI 0.162.1 in its tool-less text mode: the exec argv, the run-only
CODEX_HOME and fixed variables, the srt policy (chatgpt.com:443, the CA file,
the login copy unwritable), the ChatGPT login file (shape, refusals, freshness),
the parent's copy of it in the run and its removal, the JSONL result parser and
the observed model. Until a real run is recorded the gate keeps codex closed.

Every codex/srt here is a test-only fake and every login file holds fake
sentinels (unsigned JWT-shaped strings), never a real token.
"""

import base64
import calendar
import hashlib
import importlib.machinery
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "bin/oss-research-worker"
SHORT_TMP = "/tmp" if sys.platform == "linux" else "/private/tmp"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


worker = load("oss_research_worker_for_codex", WORKER)

RUNTIME = ["/opt/node", "/opt/srt/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"]
POLICY_FILE = Path("/run/policy/srt-settings.json")
SELECTION = worker.Selection("gpt-6.1-sol", "medium")
PROFILE = "codex-profile1"


def b64(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def fake_jwt(label: str, exp: int) -> str:
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp, 'sentinel': label})}.sentinel-{label}-sig"


def stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".123456789Z"


def login(now=None, **changes):
    """A ChatGPT login file of Codex's shape with fake sentinel tokens."""
    now = time.time() if now is None else now
    tokens = {"id_token": fake_jwt("id", int(now) + 3600),
              "access_token": fake_jwt("access", int(now) + 10 * 24 * 3600),
              "refresh_token": "sentinel-refresh-" + "r" * 40,
              "account_id": "sentinel-account"}
    document = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens,
                "last_refresh": stamp(now - 3600)}
    document.update(changes)
    return document


def jsonl(*events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def good_stream(text="OK"):
    return jsonl({"type": "thread.started", "thread_id": "t-1"},
                 {"type": "turn.started"},
                 {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "x"}},
                 {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                                     "text": text}},
                 {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}})


SERVER_MODEL = ("2026-10-13T00:00:00.000000Z  INFO codex_core::session: server reported model "
                "gpt-6.1-sol (matches requested model)\n")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osrc-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        (self.input / "task.txt").write_text("summarize", encoding="utf-8")
        self.run_dir = self.root / "run"
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def write_login(self, document=None, profile=PROFILE, name="auth.json", text=None):
        directory = self.root / "credentials" / profile
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / name
        path.write_text(text if text is not None else json.dumps(document or login(), indent=2))
        path.chmod(0o600)
        return path

    def load(self, path=None):
        return worker.load_credential(PROFILE, path or self.write_login(), self.input,
                                      self.run_dir, backend=worker.CODEX)


class CodexAdapterTests(unittest.TestCase):
    def test_the_backend_fields(self):
        codex = worker.CODEX
        self.assertEqual((codex.name, codex.executable), ("codex", "codex"))
        self.assertEqual((codex.credential_name, codex.credential_file, codex.credential_variable),
                         ("auth.json", "auth.json", None))
        self.assertEqual((codex.config_variable, codex.config_subdir), ("CODEX_HOME", "home/.codex"))
        self.assertIsNone(codex.allowed_domains)  # Linux only
        self.assertEqual(codex.allowed_domains_linux, ("chatgpt.com:443",))
        self.assertEqual(codex.pinned_names, ("chatgpt.com",))
        self.assertEqual(codex.profiles, ("codex-profile1",))
        self.assertEqual(dict(codex.environment), {
            "CODEX_EXEC_SERVER_URL": "none",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "RUST_LOG": "error,opentelemetry_sdk=off,opentelemetry_otlp=off,codex_core::session=info"})
        self.assertEqual(codex.system_read_linux, ("/etc/ssl/certs/ca-certificates.crt",))
        self.assertEqual(codex.config_unwritable, ("tmp",))
        self.assertEqual((worker.CLAUDE.config_unwritable, worker.AGY.config_unwritable), ((), ()))
        self.assertEqual(worker.ALL_PROFILES, ("claude-profile1", "claude-profile2", "codex-profile1"))

    def test_argv_is_the_tool_less_exec(self):
        argv = worker.build_command(RUNTIME, POLICY_FILE, "/opt/codex", "-- Reply OK", "dontAsk",
                                    SELECTION, backend=worker.CODEX)
        self.assertEqual(argv[:5], [*RUNTIME, "--settings", str(POLICY_FILE), "--"])
        child = argv[5:]
        expected = ["/opt/codex"]
        for feature in ("apps", "plugins", "image_generation", "goals", "sleep_tool", "shell_snapshot",
                        "unbounded_connection_retries", "hooks", "shell_tool", "unified_exec",
                        "view_image"):
            expected += ["--disable", feature]
        expected += ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-rules",
                     "--ignore-user-config", "--sandbox", "read-only", "--color", "never"]
        for setting in ('web_search="disabled"', "agents.enabled=false", "cloud.skills.enabled=false",
                        "skills.bundled.enabled=false", "skills.include_instructions=false",
                        "analytics.enabled=false", "project_doc_max_bytes=0",
                        "tools.experimental_request_user_input.enabled=false",
                        "check_for_update_on_startup=false"):
            expected += ["-c", setting]
        expected += ["--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="medium"',
                     "--", "-- Reply OK"]
        self.assertEqual(child, expected)
        # Never the sandbox bypass, an approval flag or another sandbox mode.
        joined = " ".join(child)
        for absent in ("dangerously", "--ask-for-approval", "workspace-write", "full-access",
                       "--search", "--enable"):
            self.assertNotIn(absent, joined)

    def test_argv_refusals(self):
        with self.assertRaisesRegex(ValueError, "tool-less mode"):
            worker.build_command(RUNTIME, POLICY_FILE, "/opt/codex", "p", "auto", SELECTION,
                                 backend=worker.CODEX)
        with self.assertRaisesRegex(worker.Blocked, "explicit --model and --effort"):
            worker.build_command(RUNTIME, POLICY_FILE, "/opt/codex", "p", backend=worker.CODEX)
        # Codex's own levels beyond the runner's (ultra, none, ...) are refused as input.
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            worker.build_command(RUNTIME, POLICY_FILE, "/opt/codex", "p", "dontAsk",
                                 worker.Selection("gpt-6.1-sol", "max")._replace(effort="ultra"),
                                 backend=worker.CODEX)
        # Staged profiles stay Claude's (build_staged_command is not Codex's).
        with self.assertRaisesRegex(worker.Blocked, "no measured argv"):
            worker.build_staged_command(RUNTIME, POLICY_FILE, "/opt/codex", "p", Path("/run"),
                                        worker.StagedRun("corpus-extract", Path("/src")),
                                        selection=SELECTION, backend=worker.CODEX)

    def test_environment_is_the_run_codex_home_and_no_credential_variable(self):
        parent = {"PATH": "/x/bin", "LANG": "C.UTF-8", "HOME": "/home/me", "CODEX_HOME": "/home/me/.codex",
                  "CODEX_API_KEY": "sentinel-paid", "OPENAI_API_KEY": "sentinel-paid",
                  "CODEX_ACCESS_TOKEN": "sentinel-pat", "RUST_LOG": "trace",
                  "SSL_CERT_FILE": "/elsewhere"}
        credential = worker.Credential(PROFILE, "{}", ("sentinel-secret-" + "s" * 20,))
        environment = worker.build_environment(parent, Path("/run"), credential, platform="linux",
                                               backend=worker.CODEX)
        self.assertEqual(environment, {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/run/home", "TMPDIR": "/run/tmp",
            "CODEX_HOME": "/run/home/.codex", "CODEX_EXEC_SERVER_URL": "none",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "RUST_LOG": "error,opentelemetry_sdk=off,opentelemetry_otlp=off,codex_core::session=info"})
        # CODEX_HOME is not under TMPDIR (Codex refuses its arg0 helpers there).
        self.assertFalse(Path(environment["CODEX_HOME"]).is_relative_to(environment["TMPDIR"]))

    def test_policy_opens_chatgpt_the_ca_file_and_never_the_login_copy_for_writing(self):
        with mock.patch.object(worker, "linux_link_targets", lambda paths: []), \
                mock.patch.object(worker.os.path, "realpath", lambda path: str(path)):
            policy = worker.build_policy(Path("/input"), Path("/run"), Path("/opt/codex"), None,
                                         platform="linux", backend=worker.CODEX)
            claude = worker.build_policy(Path("/input"), Path("/run"), Path("/opt/codex"), None,
                                         platform="linux")
            relay = worker.build_policy(Path("/input"), Path("/run"), Path("/opt/codex"), 4321,
                                        platform="linux", backend=worker.CODEX)
            probe = worker.build_policy(Path("/input"), Path("/run"), Path("/opt/codex"), None,
                                        model=False, platform="linux", backend=worker.CODEX)
        self.assertEqual(policy["network"]["allowedDomains"], ["chatgpt.com:443"])
        self.assertEqual(relay["network"]["allowedDomains"], ["chatgpt.com:443", "127.0.0.1:4321"])
        self.assertEqual(policy["filesystem"]["denyWrite"],
                         ["/run/policy", "/run/result", "/run/home/.codex/tmp",
                          "/run/home/.codex/auth.json"])
        read = policy["filesystem"]["allowRead"]
        self.assertEqual(read, [*worker.SYSTEM_READ_LINUX, "/etc/ssl/certs/ca-certificates.crt",
                                "/input", "/opt/codex"])
        # Only the network, the CA file and the login copy differ from Claude's policy.
        self.assertEqual(policy["filesystem"]["allowWrite"], claude["filesystem"]["allowWrite"])
        self.assertEqual(policy["socatPath"], claude["socatPath"])
        self.assertEqual(policy["network"]["deniedResolvedAddresses"],
                         claude["network"]["deniedResolvedAddresses"])
        # The non-AI probe policy (model=False) opens no endpoint and protects no login.
        self.assertEqual(probe["network"]["allowedDomains"], [])
        self.assertEqual(probe["filesystem"]["denyWrite"],
                         ["/run/policy", "/run/result", "/run/home/.codex/tmp"])
        with self.assertRaisesRegex(worker.Blocked, "no measured allowed domains"):
            worker.build_policy(Path("/input"), Path("/run"), Path("/opt/codex"), None,
                                platform="darwin", backend=worker.CODEX)


class CodexCredentialTests(Fixture):
    def test_a_login_file_loads_with_its_three_tokens_as_secrets(self):
        document = login()
        credential = self.load(self.write_login(document))
        self.assertEqual(credential.profile, PROFILE)
        self.assertEqual(json.loads(credential.reveal()), document)
        tokens = document["tokens"]
        self.assertEqual(set(credential.secrets()),
                         {tokens["id_token"], tokens["access_token"], tokens["refresh_token"]})
        for token in credential.secrets():
            self.assertNotIn(token, repr(credential) + str(credential))
        self.assertEqual(set(credential.facts), {"access_expires_at", "last_refresh_at"})
        # auth_mode may be absent (older files), OPENAI_API_KEY may be absent too.
        document = login()
        del document["auth_mode"], document["OPENAI_API_KEY"]
        self.write_login(document).unlink()
        self.assertTrue(self.load(self.write_login(document)).secrets())

    def refused(self, path, pattern):
        with self.assertRaisesRegex(worker.Blocked, pattern) as caught:
            self.load(path)
        message = str(caught.exception)
        for marker in ("sentinel-refresh", "sentinel-paid", "sentinel-pat", "eyJ"):
            self.assertNotIn(marker, message)
        path.unlink()
        return message

    def test_paid_or_other_logins_and_malformed_files_are_refused_without_values(self):
        cases = (
            (login(OPENAI_API_KEY="sentinel-paid-" + "k" * 30), "API key"),
            (login(auth_mode="apikey"), "auth_mode is not chatgpt"),
            (login(personal_access_token="sentinel-pat-" + "p" * 30), "personal_access_token"),
            (login(agent_identity="sentinel-pat-" + "a" * 30), "agent_identity"),
            (login(bedrock_api_key={"key": "sentinel-paid"}), "bedrock_api_key"),
            (login(tokens=None, last_refresh=stamp(time.time()) + " " * 200), "no tokens"),
            (login(tokens="sentinel-refresh-" + "t" * 200), "no tokens"),
            (login(last_refresh="yesterday"), "last_refresh"),
            (login(last_refresh=None), "last_refresh"),
        )
        for document, pattern in cases:
            with self.subTest(pattern):
                self.refused(self.write_login(document), pattern)
        for key in worker.CODEX_TOKENS:
            for bad in (None, "", "short", "has space " + "x" * 30, 7):
                document = login()
                document["tokens"][key] = bad
                with self.subTest(key=key, bad=bad):
                    self.refused(self.write_login(document), f"tokens.{key}")
        document = login()
        document["tokens"]["access_token"] = "sentinel-not-a-jwt-" + "x" * 30
        self.refused(self.write_login(document), "no expiry")
        document = login()
        document["tokens"]["refresh_token"] = document["tokens"]["id_token"]
        self.refused(self.write_login(document), "two tokens are the same")
        for text in ("not json " * 40, json.dumps([login()]), '{"a": 1, "a": 2}' + " " * 200,
                     json.dumps(login()).replace('"chatgpt"', '"chatgpt", "auth_mode": "chatgpt"')):
            with self.subTest(text=text[:20]):
                self.refused(self.write_login(text=text), "not a Codex ChatGPT login file")

    def test_the_file_checks_are_the_claude_ones_with_codex_names(self):
        path = self.write_login()
        with self.assertRaisesRegex(worker.Blocked, "must be <dir>/codex-profile1/auth.json"):
            worker.load_credential(PROFILE, path.with_name("oauth-token"), backend=worker.CODEX)
        with self.assertRaisesRegex(worker.Blocked, "not a codex profile"):
            worker.checked_credential("claude-profile2", path, backend=worker.CODEX)
        with self.assertRaisesRegex(worker.Blocked, "not a claude profile"):
            worker.checked_credential(PROFILE, path)
        path.chmod(0o640)
        with self.assertRaisesRegex(worker.Blocked, "mode 600"):
            self.load(path)
        path.chmod(0o600)
        link = path.parent / "second"
        os.link(path, link)
        with self.assertRaisesRegex(worker.Blocked, "exactly one link"):
            self.load(path)
        link.unlink()
        path.write_text("{}")
        with self.assertRaisesRegex(worker.Blocked, "not a Codex ChatGPT login file"):
            self.load(path)
        path.write_text("x" * (worker.CODEX_CREDENTIAL_BYTES[1] + 1))
        with self.assertRaisesRegex(worker.Blocked, "not a Codex ChatGPT login file"):
            self.load(path)
        # Another backend's profile name above the file is refused (claude's too).
        nested = self.root / "credentials/claude-profile1/x" / PROFILE
        nested.mkdir(parents=True, mode=0o700)
        other = nested / "auth.json"
        other.write_text(json.dumps(login()))
        other.chmod(0o600)
        with self.assertRaisesRegex(worker.Blocked, "name only its own profile"):
            self.load(other)
        with self.assertRaisesRegex(worker.Blocked, "outside the input and run"):
            worker.load_credential(PROFILE, self.write_login(), path.parent, backend=worker.CODEX)

    def test_freshness_refuses_a_login_codex_would_refresh_within_the_run(self):
        now = time.time()
        fresh = self.load(self.write_login(login(now)))
        self.assertEqual(worker.codex_credential_problems(fresh, 300, now), [])
        expiring = login(now)
        expiring["tokens"]["access_token"] = fake_jwt("access", int(now) + 300 + 300 + 30)
        path = self.write_login(expiring)
        self.assertIn("expires before the run could end",
                      " ".join(worker.codex_credential_problems(self.load(path), 300, now)))
        path.unlink()
        old = self.load(self.write_login(login(now, last_refresh=stamp(now - 8 * 24 * 3600 + 200))))
        self.assertIn("too old", " ".join(worker.codex_credential_problems(old, 300, now)))
        (self.root / "credentials" / PROFILE / "auth.json").unlink()
        future = self.load(self.write_login(login(now, last_refresh=stamp(now + 3600))))
        self.assertIn("in the future", " ".join(worker.codex_credential_problems(future, 300, now)))


class CodexResultTests(unittest.TestCase):
    def test_a_completed_text_turn_is_the_report(self):
        problems, text = worker.result_problems(0, good_stream("OK"), worker.CODEX)
        self.assertEqual((problems, text), ([], "OK"))
        # Transient reconnect notices and the WebSocket -> HTTPS notice are not failures.
        stream = jsonl({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                       {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"},
                       {"type": "item.completed", "item": {"id": "i", "type": "error", "message":
                        worker.CODEX_FALLBACK_NOTICE + " unexpected status"}},
                       {"type": "item.completed", "item": {"id": "j", "type": "agent_message",
                                                           "text": "first"}},
                       {"type": "item.completed", "item": {"id": "k", "type": "agent_message",
                                                           "text": "last"}},
                       {"type": "turn.completed", "usage": {}})
        self.assertEqual(worker.result_problems(0, stream, worker.CODEX), ([], "last"))

    def test_failures(self):
        start = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
        message = {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "OK"}}
        done = {"type": "turn.completed", "usage": {}}
        cases = (
            (jsonl(*start, {"type": "item.started", "item": {"id": "c", "type": "command_execution",
                                                             "command": "cat /etc/passwd"}},
                   message, done), "a tool item: command_execution"),
            (jsonl(*start, {"type": "item.completed", "item": {"id": "w", "type": "web_search"}},
                   message, done), "a tool item: web_search"),
            (jsonl(*start, {"type": "item.completed", "item": {"id": "e", "type": "error", "message":
                   "model rerouted: gpt-6.1-sol -> gpt-5.2 (HighRiskCyberActivity)"}}, message, done),
             "an error item: model rerouted"),
            (jsonl(*start, {"type": "item.completed", "item": {"id": "e", "type": "error",
                                                               "message": "boom"}}, message, done),
             "an error item: see stdout"),
            (jsonl(*start, {"type": "turn.failed", "error": {"message": "x"}}), "the turn did not complete"),
            (jsonl(*start, message), "the turn did not complete"),
            (jsonl(*start, message, done, done), "the turn did not complete"),
            (jsonl(*start, done, message), "events follow the completed turn"),
            (jsonl(start[1], message, done), "stdout does not start exactly one thread"),
            (jsonl(*start, done), "result text is empty"),
            (jsonl(*start, {"type": "item.completed", "item": {"id": "m", "type": "agent_message",
                                                               "text": "  "}}, done), "result text is empty"),
            (good_stream() + "not json\n", "stdout is not Codex JSONL"),
            (good_stream() + "[1]\n", "stdout is not Codex JSONL"),
            (json.dumps({"type": "result", "subtype": "success", "is_error": False,
                         "permission_denials": [], "result": "OK"}), "stdout does not start exactly one thread"),
        )
        for stream, expected in cases:
            with self.subTest(expected):
                problems, _ = worker.result_problems(0, stream, worker.CODEX)
                self.assertIn(expected, problems)
        problems, _ = worker.result_problems(1, good_stream(), worker.CODEX)
        self.assertEqual(problems, ["exit code 1"])
        self.assertEqual(worker.result_problems(None, good_stream(), worker.CODEX),
                         (["no exit code: the supervisor stopped the child"], None))

    def test_observed_model_comes_from_the_session_log_or_a_reroute(self):
        self.assertEqual(worker.observed_models(good_stream(), worker.CODEX, SERVER_MODEL),
                         ["gpt-6.1-sol"])
        self.assertIsNone(worker.observed_models(good_stream(), worker.CODEX, ""))
        self.assertIsNone(worker.observed_models(good_stream(), worker.CODEX))
        mismatch = ("WARN codex_core::session: server reported model gpt-5.2 while requested "
                    "model was gpt-6.1-sol\n")
        rerouted = jsonl({"type": "item.completed", "item": {"id": "e", "type": "error", "message":
                          "model rerouted: gpt-6.1-sol -> gpt-5.2 (HighRiskCyberActivity)"}})
        self.assertEqual(worker.observed_models(rerouted, worker.CODEX, mismatch), ["gpt-5.2"])
        self.assertEqual(worker.observed_models(rerouted, worker.CODEX, SERVER_MODEL),
                         ["gpt-5.2", "gpt-6.1-sol"])
        # The Claude shape is not read for codex, nor the Codex log for claude.
        claude = json.dumps({"modelUsage": {"claude-opus-5-5": {}}})
        self.assertIsNone(worker.observed_models(claude, worker.CODEX, ""))
        self.assertIsNone(worker.observed_models(good_stream(), worker.CLAUDE, SERVER_MODEL))


class CodexRunTests(Fixture):
    """run_research for codex with a fake srt (execs the child) and a fake codex."""

    def setUp(self):
        super().setUp()
        write = lambda path, body: (path.write_text("#!/usr/bin/env python3\n" + body),
                                    path.chmod(0o755))
        write(self.bin / "srt", "import os,sys\na=sys.argv[1:]\n"
                                "assert a[0]=='--settings' and a[2]=='--'\nos.execv(a[3],a[3:])\n")
        self.write = write

    def research(self, body="", stdout=None, stderr=SERVER_MODEL, document=None, seconds=20,
                 credential="load", staged=None, profile=PROFILE):
        stdout = good_stream() if stdout is None else stdout
        self.write(self.bin / "codex", (
            "import json,os,sys\n"
            "home=os.environ['CODEX_HOME']\n"
            "out=os.path.join(os.environ['HOME'],'..','output')\n"
            "seen={'argv':sys.argv[1:],'env':dict(os.environ),"
            "'login':__import__('hashlib').sha256(open(os.path.join(home,'auth.json'),'rb').read()).hexdigest(),"
            "'mode':oct(os.stat(os.path.join(home,'auth.json')).st_mode & 0o777)}\n"
            "open(os.path.join(out,'seen.json'),'w').write(json.dumps(seen))\n"
            + body + f"sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\n"))
        if credential == "load":
            credential = self.load(self.write_login(document))
        parent = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "CODEX_API_KEY": "sentinel-paid"}
        with mock.patch("sys.platform", "linux"):
            succeeded = worker.run_research(
                [str(self.bin / "srt")], str(self.bin / "codex"), "Reply with exactly OK", self.input,
                self.run_dir, profile, seconds, None, parent, "dontAsk", credential, staged,
                SELECTION, backend=worker.CODEX)
        manifest_file = self.run_dir / "result/manifest.json"
        manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else None
        return succeeded, manifest, credential

    def seen(self):
        return json.loads((self.run_dir / "output/seen.json").read_text())

    def test_a_run_gives_the_child_the_login_then_removes_it(self):
        succeeded, manifest, credential = self.research()
        self.assertTrue(succeeded, manifest)
        seen = self.seen()
        self.assertEqual(seen["login"], hashlib.sha256(credential.reveal().encode()).hexdigest())
        self.assertEqual(seen["mode"], "0o600")
        self.assertEqual(seen["env"]["CODEX_HOME"], str(self.run_dir / "home/.codex"))
        self.assertNotIn("CODEX_API_KEY", seen["env"])
        self.assertEqual(seen["argv"][-1], "Reply with exactly OK")
        self.assertFalse((self.run_dir / "home/.codex/auth.json").exists())
        self.assertTrue((self.run_dir / "home/.codex/tmp").is_dir())  # made by the parent
        self.assertIn(str(self.run_dir / "home/.codex/tmp"),
                      json.loads((self.run_dir / "policy/srt-settings.json").read_text())["filesystem"]["denyWrite"])
        self.assertEqual(manifest["backend"], "codex")
        self.assertEqual(manifest["credential"], {"file": "home/.codex/auth.json",
                                                  "exact_value_check": "clean"})
        self.assertEqual(manifest["observed_models"], ["gpt-6.1-sol"])
        self.assertEqual(manifest["requested_model"], "gpt-6.1-sol")
        self.assertEqual((self.run_dir / "result/report.md").read_text(), "OK")
        policy = json.loads((self.run_dir / "policy/srt-settings.json").read_text())
        self.assertIn(str(self.run_dir / "home/.codex/auth.json"), policy["filesystem"]["denyWrite"])
        everything = "".join(path.read_text(errors="replace")
                             for path in (self.run_dir / "result").iterdir())
        for secret in credential.secrets():
            self.assertNotIn(secret, everything)

    def test_no_server_model_line_means_nothing_observed(self):
        succeeded, manifest, _ = self.research(stderr="")
        self.assertTrue(succeeded, manifest)
        self.assertNotIn("observed_models", manifest)

    def test_a_copied_token_anywhere_fails_the_run(self):
        body_home = ("t=json.load(open(os.path.join(home,'auth.json')))['tokens']['refresh_token']\n"
                     "open(os.path.join(os.environ['HOME'],'copy.txt'),'w').write(t)\n")
        succeeded, manifest, _ = self.research(body=body_home)
        self.assertFalse(succeeded)
        self.assertIn("credential-exposed: home", manifest["problems"])
        self.assertFalse((self.run_dir / "result/report.md").exists())
        shutil.rmtree(self.run_dir)
        body_out = ("t=json.load(open(os.path.join(home,'auth.json')))['tokens']['access_token']\n"
                    "sys.stderr.write(t)\n")
        succeeded, manifest, _ = self.research(body=body_out)
        self.assertFalse(succeeded)
        self.assertIn("credential-exposed: stderr", manifest["problems"])
        self.assertFalse((self.run_dir / "result/stderr.log").exists())

    def test_a_rewritten_login_fails_the_run(self):
        body = "open(os.path.join(home,'auth.json'),'a').write(' ')\n"
        succeeded, manifest, _ = self.research(body=body)
        self.assertFalse(succeeded)
        self.assertIn("credential-file-changed", manifest["failures"])
        self.assertFalse((self.run_dir / "home/.codex/auth.json").exists())

    def test_refused_before_any_directory(self):
        now = time.time()
        stale = login(now)
        stale["tokens"]["access_token"] = fake_jwt("access", int(now) + 60)
        cases = (({"document": stale}, "expires before the run could end"),
                 ({"credential": None}, "needs its ChatGPT login file"),
                 ({"staged": worker.StagedRun("corpus-extract", Path("/src"))}, "no staged profile"))
        for options, pattern in cases:
            with self.subTest(pattern):
                with self.assertRaisesRegex(worker.Blocked, pattern):
                    self.research(**options)
                self.assertFalse(self.run_dir.exists())
                (self.root / "credentials" / PROFILE / "auth.json").unlink(missing_ok=True)


class CodexGateTests(Fixture):
    """Until a real run is recorded the gate keeps codex closed, token unread."""

    def request(self, **changes):
        fields = dict(execution_profile="read-only", permission_mode="dontAsk", relay=False,
                      selection=SELECTION, unit_problems=(), backend="codex")
        fields.update(changes)
        return worker.LaunchRequest(**fields)

    def test_no_codex_launch_is_measured_yet(self):
        self.assertEqual(worker.MEASURED_BACKEND_LAUNCHES, {})
        problems = worker.measured_launch_problems("linux", PROFILE, self.request())
        self.assertEqual(problems[0], "backend codex is not the measured claude")

    def test_a_recorded_codex_launch_matches_only_itself(self):
        scope = worker.MEASURED_LAUNCHES["linux"]._replace(
            cli_sha256="0" * 64, profile=PROFILE, model="gpt-6.1-sol", effort="medium", backend="codex")
        with mock.patch.dict(worker.MEASURED_BACKEND_LAUNCHES, {("codex", "linux"): scope}):
            self.assertEqual(worker.measured_launch_problems("linux", PROFILE, self.request()), [])
            self.assertEqual(worker.measured_scope("linux", "claude"), worker.MEASURED_LAUNCHES["linux"])
            for change, text in (({"relay": True}, "relay"), ({"permission_mode": "auto"}, "permission mode"),
                                 ({"execution_profile": "corpus-extract"}, "execution profile"),
                                 ({"selection": worker.Selection("gpt-5.5", "medium")}, "model"),
                                 ({"selection": worker.Selection("gpt-6.1-sol", "high")}, "effort"),
                                 ({"unit_problems": ("x",)}, "x")):
                with self.subTest(text):
                    problems = worker.measured_launch_problems("linux", PROFILE, self.request(**change))
                    self.assertTrue(any(text in item for item in problems), problems)
            self.assertIn("profile codex-profile2 is not the measured codex-profile1",
                          worker.measured_launch_problems("linux", "codex-profile2", self.request()))

    def test_gate_blocks_codex_with_the_login_unread(self):
        path = self.write_login()
        with mock.patch.object(worker, "verify_runtime", return_value=list(RUNTIME)), \
                mock.patch.object(worker, "platform_problems", return_value=[]), \
                mock.patch.object(worker, "read_credential") as read, \
                mock.patch.object(worker.subprocess, "Popen") as popen:
            for platform in ("linux", "darwin"):
                with self.subTest(platform), self.assertRaisesRegex(worker.Blocked, "launch is closed"):
                    worker.require_runtime(PROFILE, self.root / "prefix", str(self.bin), path,
                                           (self.input, self.run_dir), platform=platform,
                                           launch=self.request(), backend=worker.CODEX)
        read.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_cli_pairs_profiles_with_backends(self):
        path = self.write_login()
        base = [sys.executable, "-B", str(WORKER), "--input-dir", str(self.input),
                "--prompt-file", str(self.input / "task.txt"), "--run-dir", str(self.run_dir),
                "--credential-file", str(path), "--model", "gpt-6.1-sol", "--effort", "medium"]
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home")}
        for extra, code, text in (
                (["--backend", "codex", "--profile", "claude-profile2"], 2, "not a codex profile"),
                (["--profile", PROFILE], 2, "not a claude profile"),
                (["--backend", "codex", "--profile", "codex-profile2"], 2, "--profile"),
                (["--backend", "codex", "--profile", PROFILE], 3, "blocked")):
            with self.subTest(extra=extra):
                result = subprocess.run(base + extra, cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(text, result.stderr)
                self.assertFalse(self.run_dir.exists())
                self.assertNotIn("sentinel-refresh", result.stdout + result.stderr)


HARNESS = ROOT / "share/linux/model_once.sh"
VERDICT = ROOT / "share/linux/model_once_verdict.py"
INNER = ROOT / "tests/fixtures/oss_research_worker/linux/unit_inner.py"
CASES = ROOT / "tests/fixtures/oss_research_worker/linux/codex_cases.sh"


def load_inner():
    """unit_inner imports the supervisor test module from the repository root."""
    import contextlib
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with contextlib.chdir(ROOT):
        return load("oss_research_unit_inner_codex", INNER)


class CodexHarnessScriptTests(unittest.TestCase):
    """model_once.sh codex-once / codex-analysis and codex_cases.sh: input checks
    and the argv, before any sudo (the guest run itself is the measurement)."""

    def run_script(self, *args):
        return subprocess.run(["bash", str(HARNESS), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_modes_and_refusals(self):
        for script in (HARNESS, CASES):
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
        common = ("sentinel", "/src", "/srt", "/node/bin")
        result = self.run_script(*common, "pin", "normal", "330", "300", "codex-once")
        self.assertEqual((result.returncode, result.stdout.strip()),
                         (2, "FAIL a codex sentinel run is --egress none only "
                             "(a fake login never reaches chatgpt.com)"))
        result = self.run_script(*common, "none", "normal", "330", "300", "codex-analysis")
        self.assertEqual((result.returncode, result.stdout.strip()),
                         (2, "FAIL P0-prompt-argument (codex-analysis needs PROMPT)"))
        result = self.run_script(*common, "none", "normal", "330", "300", "codex-other")
        self.assertEqual((result.returncode, result.stdout.strip()), (2, "FAIL bad MODE"))
        usage = subprocess.run(["bash", str(CASES)], capture_output=True, text=True, timeout=60)
        self.assertEqual(usage.returncode, 2)
        text = CASES.read_text()
        self.assertIn("TAR_SHA=bf3ca2719f1143b1440fbd85c149379e696c782c50ad7991f8bfcec60b2600c1", text)
        inner = load_inner()
        self.assertIn(f"BIN_SHA={inner.CODEX_SHA256}", text)

    def argv_for(self, mode, egress="pin"):
        text = HARNESS.read_text()
        start = text.index('if [ "$PRODUCT" = yes ]; then\n  # The product path')
        end = text.index('[ "$MODE" = model-auto ] && CMD+=')
        script = text[start:end] + 'printf "%s\\n" "${CMD[@]}"\n'
        environment = {"MODE": mode, "PRODUCT": "no", "CODEX": "yes", "STAGED": "", "PY": "/usr/bin/python3",
                       "WRAPPER": "/src/bin/oss-research-linux-run", "EGRESS": egress, "TIMEOUT": "330",
                       "OUT": "/tmp/osr-m-out-AAAA", "CRED": "/run/oss-research-credentials/r/codex-profile1/auth.json",
                       "MODEL": "gpt-6.1-sol", "EFFORT": "medium", "INNER": "300", "PATH": os.environ["PATH"]}
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_argv_names_the_codex_backend(self):
        head = ["/usr/bin/python3", "-I", "-B", "/src/bin/oss-research-linux-run", "--entry", "verify",
                "--egress", "pin", "--backend", "codex", "--timeout", "330", "--"]
        tail = ["/tmp/osr-m-out-AAAA", "/run/oss-research-credentials/r/codex-profile1/auth.json",
                "gpt-6.1-sol", "medium", "300"]
        self.assertEqual(self.argv_for("codex-once"), head + ["codex-once", *tail])
        self.assertEqual(self.argv_for("codex-analysis"),
                         head + ["codex-analysis", *tail, "/tmp/osr-m-out-AAAA/prompt.txt"])
        text = HARNESS.read_text()
        for line in ('PDIR="$CDIR/codex-profile1"; CRED="$PDIR/auth.json"; MODEL=gpt-6.1-sol; CRED_MIN=200; CRED_MAX=65536',
                     '[ -n "$CODEX" ] && CLAUDE_DIR=/opt/oss-research/codex-0.162.1'):
            self.assertEqual(text.count(line), 1, line)


class CodexVerdictTests(unittest.TestCase):
    def setUp(self):
        self.verdict = load("oss_research_model_once_verdict_codex", VERDICT)
        self.root = Path(tempfile.mkdtemp(prefix="osrc-v-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def record(self, **inner_changes):
        inner = {"mode": "codex-once", "succeeded": True, "manifest_written": True, "status": "succeeded",
                 "exit_code": 0, "failures": [], "problems": [], "profile": PROFILE,
                 "permission_mode": "dontAsk", "requested_model": "gpt-6.1-sol", "requested_effort": "medium",
                 "observed_models": ["gpt-6.1-sol"], "backend": "codex", "login_copy_left": False,
                 "credential": {"file": "home/.codex/auth.json", "exact_value_check": "clean"},
                 "result_meta": {"item_types": ["reasoning", "agent_message"],
                                 "server_model_lines": ["gpt-6.1-sol"]},
                 "report_text": "OK"}
        inner.update(inner_changes)
        pin = {name: {"exit": 0} for name in ("apply", "verify", "clear")}
        return json.dumps({"unit": "oss-research-0123456789abcdef", "exit_code": 0, "failures": [],
                           "egress_pin": pin, "stdout": json.dumps(inner) + "\n"})

    def test_a_good_codex_record_passes_and_each_counterexample_fails(self):
        rows = dict(self.verdict.judge(self.record(), "token"))
        self.assertEqual(rows, {name: "PASS" for name in (
            "R1-record-present", "R2-unit-named", "R3-inner-summary-present", "R4-contract",
            "R5-exposure-check-clean", "R6-login-copy-removed", "V1-manifest-succeeded",
            "V2-fixed-answer-OK", "V3-text-items-only", "V4-requested-model-observed",
            "V5-pin-apply-verify-clear")})
        analysis = dict(self.verdict.judge(self.record(mode="codex-analysis", report_text=None), "token"))
        self.assertNotIn("V2-fixed-answer-OK", analysis)
        self.assertTrue(all(status == "PASS" for status in analysis.values()))
        for change, failed in (
                ({"requested_model": "gpt-5.5"}, "R4-contract"), ({"backend": "claude"}, "R4-contract"),
                ({"profile": "claude-profile2"}, "R4-contract"),
                ({"credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"}},
                 "R5-exposure-check-clean"),
                ({"login_copy_left": True}, "R6-login-copy-removed"),
                ({"problems": ["x"]}, "V1-manifest-succeeded"), ({"report_text": "OK!"}, "V2-fixed-answer-OK"),
                ({"result_meta": {"item_types": ["command_execution", "agent_message"],
                                  "server_model_lines": ["gpt-6.1-sol"]}}, "V3-text-items-only"),
                ({"observed_models": None}, "V4-requested-model-observed"),
                ({"observed_models": ["gpt-5.2"]}, "V4-requested-model-observed"),
                ({"result_meta": {"item_types": ["agent_message"], "server_model_lines": []}},
                 "V4-requested-model-observed")):
            with self.subTest(failed=failed, change=change):
                self.assertEqual(dict(self.verdict.judge(self.record(**change), "token"))[failed], "FAIL")
        # A sentinel run (--egress none, the login is fake) is judged on R4-R6 only.
        sentinel = dict(self.verdict.judge(self.record(succeeded=False, status="failed"), "sentinel"))
        self.assertEqual(set(sentinel), {"R1-record-present", "R2-unit-named", "R3-inner-summary-present",
                                         "R4-contract", "R5-exposure-check-clean", "R6-login-copy-removed"})

    def test_scan_looks_for_each_token_of_a_login_file(self):
        directory = self.root / "codex-profile1"
        directory.mkdir()
        credential = directory / "auth.json"
        document = login()
        credential.write_text(json.dumps(document))
        area = self.root / "area"
        area.mkdir()
        (area / "plain.txt").write_text("nothing here")
        self.assertEqual(self.verdict.scan(credential, [area], worker), "clean")
        for key in ("id_token", "access_token", "refresh_token"):
            with self.subTest(key):
                (area / "leak.txt").write_text("x" + document["tokens"][key] + "x")
                self.assertEqual(self.verdict.scan(credential, [area], worker), "exposed")
                (area / "leak.txt").unlink()
        credential.write_text("{}")
        self.assertTrue(self.verdict.scan(credential, [area], worker).startswith("incomplete"))
        # A Claude token file keeps its one-line rule.
        claude = self.root / "oauth-token"
        claude.write_text("sentinel-claude-" + "c" * 30 + "\n")
        self.assertEqual(self.verdict.scan(claude, [area], worker), "clean")


class CodexInnerTests(Fixture):
    """unit_inner.py codex-once / codex-analysis / codex-probe without a guest."""

    @classmethod
    def setUpClass(cls):
        cls.inner = load_inner()

    def test_contract_and_prompt_refusals_come_before_any_read(self):
        inner = self.inner
        with mock.patch.object(inner, "codex_binary") as binary, mock.patch("sys.stdout"):
            for model, effort, seconds in (("gpt-5.5", "medium", 300), ("gpt-6.1-sol", "high", 300),
                                           ("gpt-6.1-sol", "medium", 301), ("gpt-6.1-sol", "medium", float("nan"))):
                self.assertEqual(inner.codex_once(self.root, Path("/x"), model, effort, seconds),
                                 worker.EXIT_BAD_INPUT)
            for prompt in (self.root / "missing.txt", self.root):
                self.assertEqual(inner.codex_once(self.root, Path("/x"), "gpt-6.1-sol", "medium", 300, prompt),
                                 worker.EXIT_BAD_INPUT)
            self.assertEqual(inner.codex_probe(self.root, "unknown"), worker.EXIT_BAD_INPUT)
        binary.assert_not_called()
        self.assertEqual(inner.CODEX_CONTRACT, {"model": "gpt-6.1-sol", "effort": "medium", "max_seconds": 300.0})

    def test_codex_once_takes_the_product_path_with_the_codex_backend(self):
        inner = self.inner
        path = self.write_login()
        out = self.root / "out"
        out.mkdir()
        seen = {}

        def fake_research(runtime, cli, prompt, input_dir, run_dir, profile, seconds, relay, parent,
                          mode, credential, staged, selection, backend):
            seen.update(cli=cli, prompt=prompt, profile=profile, seconds=seconds, relay=relay, mode=mode,
                        credential=credential, staged=staged, selection=selection, backend=backend)
            (run_dir / "result").mkdir(parents=True)
            (run_dir / "result/stdout.json").write_text(good_stream())
            (run_dir / "result/stderr.log").write_text(SERVER_MODEL)
            (run_dir / "result/report.md").write_text("OK")
            (run_dir / "result/manifest.json").write_text(json.dumps({
                "status": "succeeded", "backend": "codex", "profile": PROFILE, "exit_code": 0, "failures": [],
                "problems": [], "permission_mode": "dontAsk", "requested_model": "gpt-6.1-sol",
                "requested_effort": "medium", "observed_models": ["gpt-6.1-sol"],
                "credential": {"file": "home/.codex/auth.json", "exact_value_check": "clean"}}))
            return True

        printed = io.StringIO()
        with mock.patch.object(inner, "codex_binary", return_value=(["/n", "/s"], "/opt/codex")), \
                mock.patch.object(inner.worker, "run_research", side_effect=fake_research), \
                mock.patch("sys.stdout", printed):
            code = inner.codex_once(out, path, "gpt-6.1-sol", "medium", 300)
        self.assertEqual(code, 0)
        self.assertIs(seen["backend"], inner.worker.CODEX)
        self.assertEqual((seen["profile"], seen["seconds"], seen["relay"], seen["mode"], seen["staged"]),
                         (PROFILE, 300, None, "dontAsk", None))
        self.assertEqual(seen["prompt"], inner.MODEL_ONCE_PROMPT)
        self.assertEqual(seen["selection"], inner.worker.Selection("gpt-6.1-sol", "medium"))
        self.assertEqual(set(seen["credential"].secrets()), {
            value for key, value in json.loads(path.read_text())["tokens"].items() if key != "account_id"})
        summary = json.loads(printed.getvalue())
        self.assertEqual(summary["mode"], "codex-once")
        self.assertEqual(summary["backend"], "codex")
        self.assertIs(summary["login_copy_left"], False)
        self.assertEqual(summary["result_meta"]["item_types"], ["reasoning", "agent_message"])
        self.assertEqual(summary["result_meta"]["server_model_lines"], ["gpt-6.1-sol"])
        self.assertEqual(summary["report_text"], "OK")
        for secret in seen["credential"].secrets():
            self.assertNotIn(secret, printed.getvalue())
        record = json.dumps({"unit": "oss-research-0123456789abcdef", "exit_code": 0, "failures": [],
                             "egress_pin": {name: {"exit": 0} for name in ("apply", "verify", "clear")},
                             "stdout": printed.getvalue()})
        verdict = load("oss_research_model_once_verdict_codex_inner", VERDICT)
        self.assertTrue(all(status == "PASS" for _, status in verdict.judge(record, "token")))

    def test_prompt_input_facts(self):
        items = [{"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "<permissions instructions>\n`sandbox_mode` is `read-only`"}]},
                 {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "osr probe prompt"}]}]
        facts = self.inner.prompt_input_facts(json.dumps(items))
        self.assertEqual(facts, {"parsed": True, "items": 2, "tags": ["<permissions instructions>"],
                                 "skills": False, "multi_agent": False, "canary": False, "apps": False,
                                 "sandbox_read_only": True, "approval_never": False, "prompt_last": True})
        items[0]["content"].append({"type": "input_text", "text": "<skills_instructions> x "
                                    + self.inner.CODEX_CANARY})
        facts = self.inner.prompt_input_facts(json.dumps(items))
        self.assertTrue(facts["skills"] and facts["canary"])
        self.assertEqual(self.inner.prompt_input_facts("not json"), {"parsed": False})
        # The probe's codex argv is the product's switches around a non-exec subcommand.
        argv = self.inner.codex_config_argv(["debug", "prompt-input"])
        self.assertEqual(argv[:2], ["--disable", "apps"])
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertEqual(argv[argv.index("debug"):argv.index("debug") + 2], ["debug", "prompt-input"])


if __name__ == "__main__":
    unittest.main()
