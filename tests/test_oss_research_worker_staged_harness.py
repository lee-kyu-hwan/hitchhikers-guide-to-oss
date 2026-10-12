"""The staged measurement harness (issue #6 follow-up, #4 item 2 / 8.2), offline.

share/linux/model_once.sh gains the modes staged-corpus and staged-deep,
tests/fixtures/oss_research_worker/linux/unit_inner.py the mode model-staged,
share/linux/model_once_verdict.py judges that summary (S1-staged-unchanged and
S2-profile-requested in place of V2) and share/linux/verify_pickup.py gets the
--staged PROFILE contract. Everything here is offline: no model, no VM, no
sudo; claude, srt and the product run are mocked or never reached, and the
credential files hold a fake sentinel. The guest run of these modes is a
separate, approved measurement.
"""

import contextlib
import hashlib
import importlib.machinery
import importlib.util
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

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "share/linux/model_once.sh"
VERDICT = ROOT / "share/linux/model_once_verdict.py"
PICKUP = ROOT / "share/linux/verify_pickup.py"
INNER = ROOT / "tests/fixtures/oss_research_worker/linux/unit_inner.py"
SHA = "a" * 64
FAKE_TOKEN = "sentinel-staged-" + "s" * 40


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


verdict = load("oss_research_model_once_verdict_staged", VERDICT)


def load_inner():
    """unit_inner imports the supervisor test module from the repository root."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with contextlib.chdir(ROOT):
        return load("oss_research_unit_inner_staged", INNER)


class ModelOnceScriptTests(unittest.TestCase):
    """model_once.sh: the MODE table, the PROMPT requirement and the argv it builds.

    The script itself needs the guest (sudo, systemd); only its input checks run
    here, before any of that, and the command block is evaluated on its own."""

    def run_script(self, *args):
        return subprocess.run(["bash", str(HARNESS), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def test_syntax_mode_table_and_prompt_requirement(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(HARNESS)], capture_output=True).returncode, 0)
        common = ("sentinel", "/src", "/srt", "/node/bin", "none", "normal", "330", "300")
        bad = self.run_script(*common, "staged-foo")
        self.assertEqual((bad.returncode, bad.stdout.strip()), (2, "FAIL bad MODE"))
        for mode in ("staged-corpus", "staged-deep", "analysis"):
            with self.subTest(mode):
                result = self.run_script(*common, mode)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout.strip(), f"FAIL P0-prompt-argument ({mode} needs PROMPT)")
        text = HARNESS.read_text()
        self.assertEqual(text.count('case "$MODE" in model-once|model-auto|worker|worker-dry|analysis|'
                                    'analysis-dry|staged-corpus|staged-deep) ;;'), 1)
        # The staged modes are measurement (verify entry) runs, not the product path.
        self.assertEqual(text.count('PRODUCT=no; case "$MODE" in worker|worker-dry|analysis|analysis-dry) '
                                    'PRODUCT=yes;; esac'), 1)

    def command_block(self):
        text = HARNESS.read_text()
        staged_line = re.search(r'^STAGED="".*$', text, re.M).group(0)
        start = text.index('if [ -n "$STAGED" ]; then\n  # The checked prompt')
        end = text.index('[ "$MODE" = model-auto ] && CMD+=')
        block = text[start:end]
        # The prompt copy needs sudo; the argv does not. Keep the structure, drop the copy.
        block = re.sub(r'  sudo install -m 0600 -o ossresearch -g ossresearch "\$PROMPT" "\$OUT/prompt\.txt".*\n'
                       r'  copied=.*\n  \[ "\$copied" = "\$PROMPT_SHA" \].*\n  pass "W3-prompt-copied.*\n',
                       '  :\n', block)
        self.assertNotIn("sudo", block.split('if [ "$PRODUCT" = yes ]; then')[0])
        return staged_line + "\n" + block

    def argv_for(self, mode, egress="pin"):
        script = (self.command_block()
                  + '\nprintf "%s\\n" "${CMD[@]}"\n')
        environment = {"MODE": mode, "PRODUCT": "no", "PY": "/usr/bin/python3",
                       "WRAPPER": "/src/bin/oss-research-linux-run", "EGRESS": egress, "TIMEOUT": "330",
                       "OUT": "/tmp/osr-m-out-AAAA", "CRED": "/run/oss-research-credentials/r/claude-profile2/oauth-token",
                       "MODEL": "claude-opus-5-5", "EFFORT": "medium", "INNER": "300", "PROMPT_SHA": SHA,
                       "WORKER_PROMPT": "Reply with exactly the word OK and nothing else.",
                       "PATH": os.environ["PATH"]}
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_staged_modes_build_the_model_staged_argv_with_the_claude_backend(self):
        head = ["/usr/bin/python3", "-I", "-B", "/src/bin/oss-research-linux-run", "--entry", "verify",
                "--egress", "pin", "--backend", "claude", "--timeout", "330", "--"]
        tail = ["/tmp/osr-m-out-AAAA", "/run/oss-research-credentials/r/claude-profile2/oauth-token",
                "claude-opus-5-5", "medium", "300"]
        for mode, profile in (("staged-corpus", "corpus-extract"), ("staged-deep", "deep-research")):
            with self.subTest(mode):
                self.assertEqual(self.argv_for(mode),
                                 head + ["model-staged", tail[0], profile, *tail[1:], "/tmp/osr-m-out-AAAA/prompt.txt"])
        # model-once and model-auto keep their argv and also name the claude backend
        # (PR #21 had left the verify entry's pin at none: no model endpoint under pin).
        for mode in ("model-once", "model-auto"):
            with self.subTest(mode):
                self.assertEqual(self.argv_for(mode), head + [mode, *tail])
        self.assertEqual(self.argv_for("model-once", egress="none")[6:10],
                         ["--egress", "none", "--backend", "claude"])


class UnitInnerStagedTests(unittest.TestCase):
    """unit_inner.py model-staged: argument table, refusals before any read, and the
    product path it takes with the staged profile (run_research mocked)."""

    @classmethod
    def setUpClass(cls):
        cls.inner = load_inner()
        cls.worker = cls.inner.worker

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-staged-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.out = self.root / "out"
        self.out.mkdir(mode=0o700)
        profile_dir = self.root / "creds/claude-profile2"
        profile_dir.mkdir(parents=True, mode=0o700)
        profile_dir.chmod(0o700)
        self.credential = profile_dir / "oauth-token"
        self.credential.write_text(FAKE_TOKEN + "\n")
        self.credential.chmod(0o600)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Extract the patterns of these PRs.\n")

    def staged(self, profile="corpus-extract", model="claude-opus-5-5", effort="medium", seconds=300.0,
               prompt=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.inner.model_staged(self.out, profile, self.credential, model, effort, seconds,
                                           self.prompt if prompt is None else prompt)
        lines = stdout.getvalue().strip().splitlines()
        return code, json.loads(lines[-1]) if lines else None

    def test_mode_table_and_argument_count(self):
        env = {name: value for name, value in os.environ.items() if name != self.worker.UNIT_ENV_MARKER}
        argv = ["unit_inner.py", "model-staged", str(self.out), "corpus-extract", str(self.credential),
                "claude-opus-5-5", "medium", "300", str(self.prompt)]
        with mock.patch.object(self.inner.sys, "argv", argv[:-1]), \
                mock.patch.dict(self.inner.os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as stop:
                self.inner.main()
        self.assertIn("model-staged OUT_DIR PROFILE CREDENTIAL MODEL EFFORT SECONDS PROMPT", str(stop.exception))
        with mock.patch.object(self.inner.sys, "argv", argv + ["extra"]), \
                mock.patch.dict(self.inner.os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                self.inner.main()
        # The right count, outside the unit wrapper: refused before the mode runs.
        with mock.patch.object(self.inner.sys, "argv", argv), \
                mock.patch.dict(self.inner.os.environ, env, clear=True), \
                mock.patch.object(self.inner, "model_staged", side_effect=AssertionError("ran")):
            with self.assertRaises(SystemExit) as stop:
                self.inner.main()
        self.assertIn("not inside the unit wrapper", str(stop.exception))
        self.assertFalse((self.out / "input").exists())

    def test_refuses_outside_the_contract_before_any_read(self):
        with mock.patch.object(self.inner, "measured_launch", side_effect=AssertionError("launched")):
            for profile in ("read-only", "corpus", "", "deep-research "):
                with self.subTest(profile):
                    code, summary = self.staged(profile=profile)
                    self.assertEqual(code, self.worker.EXIT_BAD_INPUT)
                    self.assertEqual(summary["refused"], "profile outside the measurement contract")
                    self.assertEqual(summary["profiles"], ["corpus-extract", "deep-research"])
            link = self.root / "link.txt"
            link.symlink_to(self.prompt)
            empty = self.root / "empty.txt"
            empty.write_bytes(b"")
            big = self.root / "big.txt"
            big.write_bytes(b"x" * (self.worker.MAX_PROMPT_BYTES + 1))
            for prompt in (link, empty, big, self.root / "absent.txt", self.root):
                with self.subTest(prompt=prompt.name):
                    code, summary = self.staged(prompt=prompt)
                    self.assertEqual(code, self.worker.EXIT_BAD_INPUT)
                    self.assertIn("prompt is not a regular file", summary["refused"])
        # The measurement contract (model, effort, seconds) is model_once's, refused the same way.
        for changes in (dict(model="claude-sonnet-5-5"), dict(effort="high"), dict(seconds=0.0),
                        dict(seconds=301.0), dict(seconds=float("nan"))):
            with self.subTest(changes):
                code, summary = self.staged(**changes)
                self.assertEqual(code, self.worker.EXIT_BAD_INPUT)
                self.assertEqual(summary["refused"], "outside the measurement contract")
        self.assertEqual(sorted(self.out.iterdir()), [])

    def fake_run_research(self, succeeded=True, problems=(), changed_sha=None, output=("extract-1.json",)):
        calls = []

        def run_research(runtime, cli, prompt, input_dir, run_dir, profile, seconds, relay, parent,
                         permission_mode, credential, staged, selection):
            calls.append(dict(runtime=runtime, cli=cli, prompt=prompt, input_dir=input_dir, run_dir=run_dir,
                              profile=profile, seconds=seconds, relay=relay, parent=parent,
                              permission_mode=permission_mode, credential=credential, staged=staged,
                              selection=selection))
            result = run_dir / "result"
            result.mkdir(parents=True)
            for area in self.worker.WRITABLE_AREAS:
                (run_dir / area).mkdir()
            for name in output:
                (run_dir / "output" / name).write_text("{}")
            record = {"profile": staged.profile, "source_root": str(staged.source_root),
                      "web_domains": list(staged.web_domains),
                      "files": [{"source": None, "staged": "settings.json", "sha256": SHA},
                                {"source": "share/staged/x", "staged": "skills/x/SKILL.md", "sha256": SHA}]}
            report = b"# extraction\n"
            manifest = {"status": "succeeded" if succeeded else "failed", "backend": "claude",
                        "profile": profile, "exit_code": 0, "failures": [], "problems": list(problems),
                        "policy_sha256": SHA, "permission_mode": permission_mode,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "requested_model": selection.model, "requested_effort": selection.effort,
                        "observed_models": ["claude-opus-5-5"], "execution_profile": staged.profile,
                        "staged": record, "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN",
                                                         "exact_value_check": "clean"},
                        "report_sha256": hashlib.sha256(report).hexdigest()}
            (result / "manifest.json").write_text(json.dumps(manifest))
            (result / "stdout.json").write_text(json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                 "permission_denials": [], "result": "done", "modelUsage": {"claude-opus-5-5": {}}}))
            if succeeded:
                (result / "report.md").write_bytes(report)
            return succeeded
        return run_research, calls

    def test_takes_the_product_path_with_the_staged_profile(self):
        run_research, calls = self.fake_run_research()
        runtime = ["/opt/node", "/opt/srt/cli.js"]
        with mock.patch.object(self.inner, "measured_launch", return_value=(runtime, "/opt/claude/claude")), \
                mock.patch.object(self.worker, "run_research", side_effect=run_research):
            code, summary = self.staged(profile="deep-research")
        self.assertEqual(code, 0, summary)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual((call["runtime"], call["cli"]), (runtime, "/opt/claude/claude"))
        self.assertEqual(call["prompt"], self.prompt.read_text())
        self.assertEqual(call["input_dir"], (self.out / "input").resolve())
        self.assertEqual((self.out / "input/task.txt").read_bytes(), self.prompt.read_bytes())
        self.assertEqual(call["run_dir"], self.out / "run")
        self.assertEqual((call["profile"], call["seconds"], call["relay"], call["permission_mode"]),
                         ("claude-profile2", 300.0, None, "dontAsk"))
        self.assertEqual(call["credential"].reveal(), FAKE_TOKEN)
        self.assertEqual(call["selection"], self.worker.Selection("claude-opus-5-5", "medium"))
        staged = call["staged"]
        self.assertIsInstance(staged, self.worker.StagedRun)
        self.assertEqual((staged.profile, staged.web_domains), ("deep-research", ()))
        # The reviewed source checkout the worker runs from (skills/ beside bin/).
        self.assertEqual(staged.source_root, ROOT)
        self.assertEqual(summary["mode"], "model-staged")
        self.assertEqual((summary["succeeded"], summary["status"], summary["manifest_written"]),
                         (True, "succeeded", True))
        self.assertEqual((summary["requested_profile"], summary["execution_profile"]),
                         ("deep-research", "deep-research"))
        self.assertEqual(summary["staged"]["profile"], "deep-research")
        self.assertEqual([entry["staged"] for entry in summary["staged"]["files"]],
                         ["settings.json", "skills/x/SKILL.md"])
        self.assertEqual(summary["staged_changed"], [])
        self.assertEqual(summary["output_files"], {"count": 1, "files": [{"path": "extract-1.json", "bytes": 2}]})
        self.assertEqual(summary["result_meta"], {"type": "result", "subtype": "success", "is_error": False,
                                                  "num_turns": 3, "permission_denials": 0,
                                                  "model_usage_keys": ["claude-opus-5-5"]})
        self.assertEqual(summary["report_text"], "# extraction\n")
        self.assertNotIn("done", json.dumps(summary))  # never the model's text
        self.assertNotIn(FAKE_TOKEN, json.dumps(summary))
        # The judge takes this summary as a complete token run.
        record = {"unit": "oss-research-0123456789abcdef", "entry": "verify", "egress": "pin", "backend": "claude",
                  "exit_code": 0, "failures": [], "stdout": json.dumps(summary) + "\n",
                  "egress_pin": {"apply": {"exit": 0}, "verify": {"exit": 0}, "clear": {"exit": 0}}}
        self.assertEqual(set(dict(verdict.judge(json.dumps(record), "token")).values()), {"PASS"})

    def test_a_changed_staged_file_or_a_failed_run_is_reported_and_exit_1(self):
        run_research, calls = self.fake_run_research(
            succeeded=False, problems=["staged-changed: settings.json", "exit code 1"], output=())
        with mock.patch.object(self.inner, "measured_launch", return_value=(["/n", "/s"], "/c/claude")), \
                mock.patch.object(self.worker, "run_research", side_effect=run_research):
            code, summary = self.staged()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["staged_changed"], ["staged-changed: settings.json"])
        self.assertEqual(summary["problems"], ["staged-changed: settings.json", "exit code 1"])
        self.assertEqual(summary["output_files"], {"count": 0, "files": []})
        self.assertNotIn("report_text", summary)
        self.assertEqual(calls[0]["staged"].profile, "corpus-extract")

    def test_input_holding_claude_configuration_or_an_installed_copy_is_blocked(self):
        run_research, calls = self.fake_run_research()
        with mock.patch.object(self.inner, "measured_launch", return_value=(["/n", "/s"], "/c/claude")), \
                mock.patch.object(self.worker, "run_research", side_effect=run_research), \
                mock.patch.object(self.worker, "source_checkout", return_value=None):
            with self.assertRaisesRegex(self.worker.Blocked, "reviewed source checkout"):
                self.staged()
        self.assertEqual(calls, [])
        self.assertFalse((self.out / "input").exists())
        self.assertFalse((self.out / "run").exists())


class VerdictStagedTests(unittest.TestCase):
    """judge on a model-staged summary: S1 and S2 in place of V2; model-once unchanged."""

    UNIT = "oss-research-0123456789abcdef"

    def summary(self, mode="model-staged", **changes):
        inner = {"mode": mode, "succeeded": True, "manifest_written": True, "status": "succeeded",
                 "exit_code": 0, "failures": [], "problems": [], "profile": "claude-profile2",
                 "permission_mode": "dontAsk", "requested_model": "claude-opus-5-5",
                 "requested_effort": "medium", "observed_models": ["claude-opus-5-5"],
                 "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                 "policy_sha256": SHA, "report_sha256": SHA, "prompt_sha256": SHA,
                 "result_meta": {"type": "result", "subtype": "success", "is_error": False,
                                 "num_turns": 3, "permission_denials": 0,
                                 "model_usage_keys": ["claude-opus-5-5"]},
                 "report_text": "# extraction\n"}
        if mode == "model-staged":
            inner.update({"requested_profile": "corpus-extract", "execution_profile": "corpus-extract",
                          "staged": {"profile": "corpus-extract", "web_domains": [],
                                     "files": [{"source": None, "staged": "settings.json", "sha256": SHA},
                                               {"source": "share/staged/a", "staged": "agents/a.md",
                                                "sha256": SHA}]},
                          "staged_changed": [], "output_files": {"count": 1, "files": []}})
        else:
            inner["report_text"] = "OK"
        inner.update(changes)
        return inner

    def record(self, inner):
        pin = {"apply": {"exit": 0}, "verify": {"exit": 0}, "clear": {"exit": 0}}
        return json.dumps({"unit": self.UNIT, "entry": "verify", "egress": "pin", "backend": "claude",
                           "exit_code": 0, "failures": [], "stdout": json.dumps(inner) + "\n",
                           "egress_pin": pin})

    def statuses(self, inner, kind="token"):
        return dict(verdict.judge(self.record(inner), kind))

    def test_a_complete_staged_token_record_passes_on_s1_s2_without_v2(self):
        judged = self.statuses(self.summary())
        self.assertEqual(set(judged.values()), {"PASS"}, judged)
        self.assertIn("S1-staged-unchanged", judged)
        self.assertIn("S2-profile-requested", judged)
        self.assertNotIn("V2-fixed-answer-OK", judged)
        self.assertEqual(sorted(judged), sorted(["R1-record-present", "R2-unit-named", "R3-inner-summary-present",
                                                 "R4-contract", "R5-exposure-check-clean", "V1-manifest-succeeded",
                                                 "S1-staged-unchanged", "S2-profile-requested",
                                                 "V3-no-permission-denials", "V4-requested-model-observed",
                                                 "V5-pin-apply-verify-clear"]))
        deep = self.summary(requested_profile="deep-research", execution_profile="deep-research",
                            staged={"profile": "deep-research", "files": [
                                {"source": None, "staged": "settings.json", "sha256": SHA}]})
        self.assertEqual(set(self.statuses(deep).values()), {"PASS"})
        # A sentinel staged run: the same R checks, nothing past R5.
        sentinel = self.statuses(self.summary(status="failed", succeeded=False), "sentinel")
        self.assertEqual(set(sentinel.values()), {"PASS"})
        self.assertNotIn("S1-staged-unchanged", sentinel)

    def test_every_bad_staged_fact_fails(self):
        base = self.summary()
        bad_files = {
            "S1-staged-unchanged": [dict(staged=None), dict(staged={"profile": "corpus-extract", "files": []}),
                                    dict(staged={"profile": "corpus-extract",
                                                 "files": [{"staged": "agents/a.md", "sha256": SHA}]}),
                                    dict(staged={"profile": "corpus-extract",
                                                 "files": [{"staged": "settings.json", "sha256": "xyz"}]}),
                                    dict(staged_changed=["staged-changed: settings.json"]),
                                    dict(staged_changed=None)],
            "S2-profile-requested": [dict(requested_profile="read-only", execution_profile="read-only"),
                                     dict(execution_profile="deep-research"),
                                     dict(execution_profile=None),
                                     dict(staged=dict(base["staged"], profile="deep-research")),
                                     dict(requested_profile=None)],
            "R4-contract": [dict(permission_mode="auto"), dict(profile="claude-profile1")],
            "V1-manifest-succeeded": [dict(problems=["staged-changed: settings.json"], status="failed")],
            "V4-requested-model-observed": [dict(observed_models=["claude-sonnet-5-5"])],
        }
        for check, cases in bad_files.items():
            for changes in cases:
                with self.subTest(check=check, changes=changes):
                    self.assertEqual(self.statuses(self.summary(**changes))[check], "FAIL")
        # The answer text is never judged for a staged run.
        self.assertEqual(set(self.statuses(self.summary(report_text="anything at all")).values()), {"PASS"})

    def test_model_once_judgement_is_unchanged_and_the_cli_follows(self):
        once = self.statuses(self.summary(mode="model-once"))
        self.assertEqual(set(once.values()), {"PASS"}, once)
        self.assertIn("V2-fixed-answer-OK", once)
        self.assertNotIn("S1-staged-unchanged", once)
        self.assertEqual(self.statuses(self.summary(mode="model-once", report_text="OK, done"))["V2-fixed-answer-OK"],
                         "FAIL")
        # Another mode's summary is no model run summary at all.
        other = self.statuses(self.summary(mode="model-auto"))
        self.assertEqual(other["R3-inner-summary-present"], "FAIL")
        root = Path(tempfile.mkdtemp(prefix="osr-staged-cli-")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        good, bad = root / "good.json", root / "bad.json"
        good.write_text(self.record(self.summary()))
        bad.write_text(self.record(self.summary(staged_changed=["staged-changed: settings.json"])))
        run = lambda path: subprocess.run([sys.executable, "-I", "-B", str(VERDICT), "judge", str(path), "token"],
                                          capture_output=True, text=True, timeout=60)
        self.assertEqual(run(good).returncode, 0, run(good).stdout)
        self.assertIn("PASS S1-staged-unchanged", run(good).stdout)
        self.assertEqual(run(bad).returncode, 1)
        self.assertIn("FAIL S1-staged-unchanged", run(bad).stdout)


class PickupStagedTests(unittest.TestCase):
    """verify_pickup.py --staged PROFILE on crafted bundles (the default result set)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-staged-pickup-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.count = 0

    def bundle(self, profile="corpus-extract", **changes):
        self.count += 1
        result = self.root / f"result-{self.count}"
        result.mkdir()
        report = b"# extraction\n"
        manifest = {"status": "succeeded", "backend": "claude", "profile": "claude-profile2", "exit_code": 0,
                    "failures": [], "problems": [], "policy_sha256": SHA, "permission_mode": "dontAsk",
                    "prompt_sha256": SHA, "requested_model": "claude-opus-5-5", "requested_effort": "medium",
                    "observed_models": ["claude-opus-5-5"], "execution_profile": profile,
                    "staged": {"profile": profile, "source_root": "/opt/oss-research/src-x", "web_domains": [],
                               "files": [{"source": None, "staged": "settings.json", "sha256": SHA},
                                         {"source": "share/staged/skills/deep-research/SKILL.md",
                                          "staged": "skills/deep-research/SKILL.md", "sha256": SHA}]},
                    "credential": {"variable": "CLAUDE_CODE_OAUTH_TOKEN", "exact_value_check": "clean"},
                    "report_sha256": hashlib.sha256(report).hexdigest()}
        manifest.update(changes)
        (result / "manifest.json").write_text(json.dumps(manifest))
        (result / "report.md").write_bytes(report)
        (result / "stdout.json").write_text("{}")
        archive = self.root / f"result-{self.count}.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(result, arcname=".")
        return archive

    def verify(self, archive, *tail):
        self.count += 1
        dest = self.root / f"out-{self.count}"
        completed = subprocess.run([sys.executable, "-I", "-B", str(PICKUP), str(archive), str(1 << 20),
                                    str(dest), *tail], capture_output=True, text=True, timeout=60)
        try:
            seen = json.loads(completed.stdout)
        except ValueError:
            seen = None
        return completed.returncode, seen, dest

    def test_the_staged_contract_binds_the_profile_and_the_default_still_passes(self):
        archive = self.bundle()
        code, seen, _ = self.verify(archive, "--staged", "corpus-extract")
        self.assertEqual((code, seen["problems"]), (0, []), seen)
        self.assertEqual((seen["contract"], seen["execution_profile"], seen["status"]),
                         ("staged", "corpus-extract", "succeeded"))
        self.assertEqual(sorted(seen["files"]), ["manifest.json", "report.md", "stdout.json"])
        # Decision: the default contract takes the same files and a succeeded manifest,
        # so it accepts a staged bundle too, without binding the profile.
        code, seen, _ = self.verify(archive)
        self.assertEqual((code, seen["problems"]), (0, []), seen)
        self.assertNotIn("contract", seen)
        deep = self.bundle(profile="deep-research")
        self.assertEqual(self.verify(deep, "--staged", "deep-research")[0], 0)
        code, seen, _ = self.verify(deep, "--staged", "corpus-extract")
        self.assertEqual(code, 1)
        self.assertEqual(seen["problems"], ["manifest execution_profile is 'deep-research', not 'corpus-extract'",
                                            "staged record profile is 'deep-research', not 'corpus-extract'"])

    def test_every_unbound_staged_manifest_fails(self):
        cases = {
            "read-only run": (dict(execution_profile=None, staged=None), "execution_profile"),
            "record missing": (dict(staged=None), "staged record is missing"),
            "no pins": ({"staged": {"profile": "corpus-extract", "files": []}}, "sha256 pins"),
            "bad pin": ({"staged": {"profile": "corpus-extract",
                                    "files": [{"staged": "settings.json", "sha256": "nothex"}]}}, "sha256 pins"),
            "no settings": ({"staged": {"profile": "corpus-extract",
                                        "files": [{"staged": "agents/a.md", "sha256": SHA}]}}, "lacks settings.json"),
            "auto": (dict(permission_mode="auto"), "permission_mode"),
            "exit": (dict(exit_code=1), "exit_code"),
            "problems": (dict(problems=["staged-changed: settings.json"]), "problems"),
            "credential": (dict(credential={"exact_value_check": "failed"}), "credential check is not clean"),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(label):
                code, seen, _ = self.verify(self.bundle(**changes), "--staged", "corpus-extract")
                self.assertEqual(code, 1, seen)
                self.assertTrue(any(expected in problem for problem in seen["problems"]), seen)
        # A failed manifest fails the default check first and the staged ones too.
        code, seen, _ = self.verify(self.bundle(status="failed"), "--staged", "corpus-extract")
        self.assertEqual(code, 1)
        self.assertEqual(seen["problems"][0], "manifest status is 'failed'")

    def test_bad_usage_unpacks_nothing(self):
        archive = self.bundle()
        for tail in (("--staged",), ("--staged", "read-only"), ("--staged", "corpus-extract", "x"),
                     ("--staged", "corpus-extract", "--auto-observation")):
            with self.subTest(tail):
                code, seen, dest = self.verify(archive, *tail)
                self.assertNotEqual(code, 0)
                self.assertIsNone(seen)
                self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
