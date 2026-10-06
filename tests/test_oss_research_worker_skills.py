"""Check the staged research-skill runs (corpus-extract, deep-research) offline.

No model, API, login, token or install is used. Every `srt`/`claude` in the
offline tests is a test-only fake that enforces nothing, so those tests prove
the argv, policy and manifest contract only. StagedBoundaryTests runs a non-AI
probe inside the pinned srt when OSS_RESEARCH_SRT_PREFIX names it:

    OSS_RESEARCH_SRT_PREFIX=/path/to/srt-0.0.77 \
      python3 -B -m unittest tests.test_oss_research_worker_skills -v

Building a deep-research argv is not a skill run: whether Claude loads the
staged skill and workflow, and what the children get, needs a live run.
"""

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


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin/oss-research-worker"
LOADER = importlib.machinery.SourceFileLoader("oss_research_worker_skills", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
worker = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(worker)
# The model endpoint as the platform's policy spells it (Linux adds :443).
MODEL_DOMAINS = list(worker.ALLOWED_DOMAINS_LINUX if sys.platform == "linux"
                     else worker.ALLOWED_DOMAINS)

PREFIX = os.environ.get("OSS_RESEARCH_SRT_PREFIX")
PYTHON = str(Path(sys.executable).resolve())
LINUX = sys.platform == "linux"
SHORT_TMP = "/tmp" if LINUX else "/private/tmp"
BASE_PREFIX = str(Path(sys.base_prefix).resolve())
# Linux: the system python's library is under /usr/lib (SYSTEM_READ_LINUX).
FIXTURE_READ = ([PYTHON] + ([] if BASE_PREFIX.startswith("/usr") else [BASE_PREFIX])
                if LINUX else [BASE_PREFIX, "/opt/homebrew"])
# Linux denials (measured on the Debian guest): read-only mounts, pinned directories (EBUSY)
# and EPERM. EACCES is not counted; file modes give it outside srt too.
DENIED = ({"Operation not permitted", "Read-only file system", "Device or resource busy",
           "Invalid cross-device link",  # rename across srt's bind mounts (measured)
           # A denied path that does not exist yet: srt puts an empty read-only
           # stub there while the sandbox runs (README), so an O_EXCL create sees
           # it. staged_problems then checks nothing was left behind.
           "File exists"}
          if LINUX else {"Operation not permitted"})
SENTINEL = "sentinel-must-not-reach-staged-child"
RUNTIME = ["/opt/node", "/opt/cli.js"]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_executable(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


def result_json(text="# Summary\n"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "permission_denials": [], "result": text})


def copy_sources(root, profile="deep-research"):
    """A source tree holding only the reviewed files a profile stages."""
    for source in worker.STAGED_PROFILES[profile].sources:
        target = root / source.source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / source.source, target)
    return root


def option(argv, name):
    return argv[argv.index(name) + 1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osrs-", dir=SHORT_TMP)).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.input = self.root / "input"
        self.input.mkdir()
        (self.input / "task.txt").write_text("summarize", encoding="utf-8")
        self.run_dir = self.root / "run"
        self.sources = copy_sources(self.root / "source")
        self.policy_file = self.run_dir / "policy/srt-settings.json"

    def staged(self, profile="deep-research", domains=(), source_root=None):
        return worker.StagedRun(profile, source_root or self.sources, tuple(domains))

    def prepare(self, profile="deep-research", domains=()):
        policy = worker.build_policy(self.input, self.run_dir, self.root / "claude",
                                     staged=self.staged(profile, domains))
        policy_file = worker.prepare_run(self.run_dir, policy)
        return policy, policy_file


class ProfileTests(Fixture):
    def test_profiles_are_explicit_and_read_only_is_the_default(self):
        self.assertEqual(worker.EXECUTION_PROFILES,
                         ("read-only", "corpus-extract", "deep-research"))
        self.assertEqual(worker.DEFAULT_EXECUTION_PROFILE, "read-only")
        self.assertEqual(set(worker.STAGED_PROFILES), {"corpus-extract", "deep-research"})
        with self.assertRaisesRegex(ValueError, "execution profile"):
            self.staged("read-only").check()
        with self.assertRaisesRegex(ValueError, "execution profile"):
            self.staged("everything").check()

    def test_read_only_argv_is_unchanged(self):
        argv = worker.build_command(RUNTIME, self.policy_file, "/opt/claude", "prompt")
        self.assertEqual(argv, [
            *RUNTIME, "--settings", str(self.policy_file), "--", "/opt/claude",
            "--print", "--output-format", "json", "--no-session-persistence",
            "--safe-mode", "--restricted", "--strict-mcp-config", "--no-chrome",
            "--disable-slash-commands", "--setting-sources", "",
            "--settings", '{"switchModelsOnFlag": false}',
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
            "--disallowedTools",
            "Bash,PowerShell,WebFetch,WebSearch,Edit,Write,NotebookEdit,Agent,Task",
            "--max-turns", "20", "--", "prompt"])

    def test_read_only_policy_is_unchanged(self):
        claude = self.root / "claude"
        self.assertEqual(worker.build_policy(self.input, self.run_dir, claude),
                         worker.build_policy(self.input, self.run_dir, claude, staged=None))
        self.assertEqual(worker.build_policy(self.input, self.run_dir, claude)
                         ["filesystem"]["denyWrite"],
                         [str(self.run_dir / "policy"), str(self.run_dir / "result")])

    def test_deep_research_argv(self):
        for mode in worker.PERMISSION_MODES:
            argv = worker.build_staged_command(
                RUNTIME, self.policy_file, "/opt/claude", "prompt", self.run_dir,
                self.staged(domains=("docs.python.org", "peps.python.org")), mode)
            self.assertEqual(argv[:6], [*RUNTIME, "--settings", str(self.policy_file),
                                        "--", "/opt/claude"])
            self.assertEqual(option(argv, "--permission-mode"), mode)
            self.assertEqual(option(argv, "--permission-prompts"), "none")
            self.assertEqual(option(argv, "--tools"), "Read,Skill,Workflow,WebSearch,WebFetch")
            self.assertEqual(option(argv, "--allowedTools").split(" "), [
                "Read", "Skill(deep-research)", "Workflow(deep-research)", "WebSearch",
                "WebFetch(domain:docs.python.org)", "WebFetch(domain:peps.python.org)"])
            self.assertEqual(option(argv, "--disallowedTools"),
                             "Bash,PowerShell,Edit,Write,NotebookEdit")
            self.assertEqual(option(argv, "--setting-sources"), "")
            claude_args = argv[5:]
            self.assertEqual(option(claude_args, "--settings"),
                             str(self.run_dir / "home/.claude/settings.json"))
            # One --settings for Claude: the staged file carries switchModelsOnFlag.
            self.assertEqual(claude_args.count("--settings"), 1)
            for kept in ("--restricted", "--strict-mcp-config", "--no-chrome",
                         "--no-session-persistence", "--print"):
                self.assertIn(kept, argv)
            for dropped in ("--safe-mode", "--disable-slash-commands", "--agents",
                            "--add-dir", "--mcp-config", "--plugin-dir"):
                self.assertNotIn(dropped, argv)
            self.assertEqual(argv[-2:], ["--", "prompt"])

    def test_deep_research_without_domains_allows_no_web_fetch(self):
        argv = worker.build_staged_command(RUNTIME, self.policy_file, "/opt/claude",
                                           "prompt", self.run_dir, self.staged())
        self.assertEqual(option(argv, "--allowedTools"),
                         "Read Skill(deep-research) Workflow(deep-research) WebSearch")
        policy = worker.build_policy(self.input, self.run_dir, self.root / "claude",
                                     staged=self.staged())
        self.assertEqual(policy["network"]["allowedDomains"], MODEL_DOMAINS)

    def test_corpus_extract_argv(self):
        argv = worker.build_staged_command(RUNTIME, self.policy_file, "/opt/claude",
                                           "prompt", self.run_dir,
                                           self.staged("corpus-extract"), "auto")
        self.assertEqual(option(argv, "--tools"), "Read,Glob,Grep,Agent,Write")
        self.assertEqual(option(argv, "--allowedTools").split(" "),
                         ["Read", "Glob", "Grep", "Agent(corpus-extractor)", "Write"])
        self.assertEqual(option(argv, "--disallowedTools"),
                         "Bash,PowerShell,WebFetch,WebSearch,Edit,NotebookEdit,Skill,Workflow")
        self.assertEqual(option(argv, "--agents"),
                         str(self.run_dir / "policy/claude-agents.json"))
        self.assertEqual(option(argv, "--add-dir"), str(self.run_dir / "output"))
        self.assertIn("--disable-slash-commands", argv)
        self.assertNotIn("--safe-mode", argv)
        self.assertEqual(option(argv, "--permission-mode"), "auto")

    def test_permission_mode_is_still_only_dont_ask_or_auto(self):
        for mode in ("bypassPermissions", "acceptEdits", "default", "plan", ""):
            with self.assertRaisesRegex(ValueError, "permission mode"):
                worker.build_staged_command(RUNTIME, self.policy_file, "/opt/claude",
                                            "prompt", self.run_dir, self.staged(), mode)

    def test_agent_definition_is_read_only_and_parent_owned(self):
        agents = worker.STAGED_PROFILES["corpus-extract"].agents
        self.assertEqual(list(agents), ["corpus-extractor"])
        self.assertEqual(agents["corpus-extractor"]["tools"], ["Read"])
        self.assertNotIn("model", agents["corpus-extractor"])
        self.assertIsNone(worker.STAGED_PROFILES["deep-research"].agents)


class WebDomainTests(Fixture):
    def test_domains_are_an_explicit_list(self):
        self.assertEqual(self.staged(domains=("docs.python.org",)).check().web_domains,
                         ("docs.python.org",))
        self.assertEqual(self.staged().check().web_domains, ())

    def test_github_family_and_malformed_domains_are_refused(self):
        for domain in ("github.com", "api.github.com", "gist.github.com",
                       "raw.githubusercontent.com", "githubusercontent.com",
                       "user.github.io", "github.githubassets.com", "ghcr.io",
                       "codeload.github.com", "x.github.dev"):
            with self.subTest(domain), self.assertRaisesRegex(ValueError, "GitHub"):
                self.staged(domains=(domain,)).check()
        for domain in ("localhost", "127.0.0.1", "example.com:443", "https://example.com",
                       "*.example.com", "Example.com", "example.com/path", "",
                       "-bad.example.com", "exa mple.com", "example.com."):
            with self.subTest(domain), self.assertRaisesRegex(ValueError, "web domain"):
                self.staged(domains=(domain,)).check()

    def test_duplicate_or_too_many_domains_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            self.staged(domains=("a.example.com", "a.example.com")).check()
        too_many = tuple(f"d{n}.example.com" for n in range(worker.MAX_WEB_DOMAINS + 1))
        with self.assertRaisesRegex(ValueError, "at most"):
            self.staged(domains=too_many).check()

    def test_only_deep_research_takes_domains(self):
        with self.assertRaisesRegex(ValueError, "deep-research profile only"):
            self.staged("corpus-extract", ("docs.python.org",)).check()

    def test_domains_reach_the_policy_and_not_the_relay_port(self):
        policy = worker.build_policy(self.input, self.run_dir, self.root / "claude", 4100,
                                     staged=self.staged(domains=("docs.python.org",)))
        self.assertEqual(policy["network"]["allowedDomains"],
                         [*MODEL_DOMAINS, "docs.python.org", "127.0.0.1:4100"])


class StagingTests(Fixture):
    def test_repository_sources_match_the_pins(self):
        for profile in worker.STAGED_PROFILES:
            for source in worker.STAGED_PROFILES[profile].sources:
                self.assertEqual(sha256(REPO / source.source), source.sha256, source.source)

    def test_sources_are_copied_hashed_and_recorded(self):
        _, policy_file = self.prepare()
        record = worker.stage_research_config(self.run_dir, self.staged())
        home = self.run_dir / "home/.claude"
        self.assertEqual(sha256(home / "skills/deep-research/SKILL.md"),
                         sha256(REPO / "share/staged/skills/deep-research/SKILL.md"))
        self.assertEqual(sha256(home / "workflows/deep-research.js"),
                         sha256(REPO / "share/staged/workflows/deep-research.js"))
        self.assertEqual(record["profile"], "deep-research")
        self.assertEqual([entry["staged"] for entry in record["files"]],
                         ["skills/deep-research/SKILL.md", "workflows/deep-research.js",
                          "settings.json"])
        for entry in record["files"]:
            path = home / entry["staged"]
            self.assertEqual(sha256(path), entry["sha256"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        manifest = self.run_dir / "policy/staged-manifest.json"
        self.assertEqual(json.loads(manifest.read_text()), record)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o400)
        for name in worker.PROTECTED_CONFIG_DIRECTORIES:
            self.assertTrue((home / name).is_dir(), name)
        self.assertEqual(worker.staged_problems(self.run_dir, record), [])

    def test_settings_repeat_the_command_rules_and_disable_hooks(self):
        self.prepare()
        worker.stage_research_config(self.run_dir, self.staged(domains=("docs.python.org",)))
        settings = json.loads((self.run_dir / "home/.claude/settings.json").read_text())
        self.assertEqual(settings, {
            "permissions": {
                "allow": ["Read", "Skill(deep-research)", "Workflow(deep-research)",
                          "WebSearch", "WebFetch(domain:docs.python.org)"],
                "deny": ["Bash", "PowerShell", "Edit", "Write", "NotebookEdit"]},
            "disableAllHooks": True, "switchModelsOnFlag": False})

    def test_corpus_extract_writes_the_agent_file_and_no_skill(self):
        self.prepare("corpus-extract")
        record = worker.stage_research_config(self.run_dir, self.staged("corpus-extract"))
        agents = self.run_dir / "policy/claude-agents.json"
        self.assertEqual(json.loads(agents.read_text()),
                         worker.STAGED_PROFILES["corpus-extract"].agents)
        self.assertEqual(agents.stat().st_mode & 0o777, 0o400)
        self.assertEqual(list((self.run_dir / "home/.claude/skills").iterdir()), [])
        self.assertEqual([entry["staged"] for entry in record["files"]], ["settings.json"])
        self.assertEqual(record["agents_sha256"], sha256(agents))

    def blocked(self, message, source_root=None):
        self.prepare()
        with self.assertRaisesRegex(worker.Blocked, message):
            worker.stage_research_config(self.run_dir, self.staged(source_root=source_root))

    def test_hash_mismatch_is_blocked(self):
        with (self.sources / "share/staged/workflows/deep-research.js").open("a") as handle:
            handle.write("// changed\n")
        self.blocked("differs from the reviewed pin")

    def test_missing_source_is_blocked(self):
        (self.sources / "share/staged/skills/deep-research/SKILL.md").unlink()
        self.blocked("unusable")

    def test_symlinked_source_is_blocked(self):
        skill = self.sources / "share/staged/skills/deep-research/SKILL.md"
        real = self.root / "elsewhere.md"
        shutil.move(skill, real)
        skill.symlink_to(real)
        self.blocked("symlink")

    def test_symlinked_source_directory_is_blocked(self):
        directory = self.sources / "share/staged/workflows"
        shutil.move(directory, self.root / "moved-workflows")
        directory.symlink_to(self.root / "moved-workflows")
        self.blocked("symlink")

    def test_symlinked_source_root_is_blocked(self):
        link = self.root / "source-link"
        link.symlink_to(self.sources)
        self.blocked("symlink", source_root=link)

    def test_copy_is_read_back_and_a_bad_copy_is_blocked(self):
        self.prepare()
        real_write = os.write

        def half(descriptor, data):
            real_write(descriptor, bytes(data[:len(data) // 2]))
            return len(data)

        with mock.patch.object(worker.os, "write", half), \
                self.assertRaisesRegex(worker.Blocked, "staged copy differs"):
            worker.stage_research_config(self.run_dir, self.staged())

    def test_short_writes_are_completed(self):
        self.prepare()
        real_write = os.write
        with mock.patch.object(worker.os, "write",
                               lambda descriptor, data: real_write(descriptor, bytes(data[:7]))):
            record = worker.stage_research_config(self.run_dir, self.staged())
        self.assertEqual(worker.staged_problems(self.run_dir, record), [])

    def test_installed_copy_is_not_a_reviewed_source(self):
        installed = self.root / "bin/oss-research-worker"
        with self.assertRaisesRegex(worker.Blocked, "source checkout"):
            worker.reviewed_source_root(installed)
        self.assertEqual(worker.reviewed_source_root(SCRIPT), REPO)
        # A checkout is <root>/bin/<name> with skills/ beside bin/; ~/.local/bin has no ~/.local/skills.
        self.assertIsNone(worker.source_checkout(installed))
        self.assertEqual(worker.source_checkout(SCRIPT), REPO)

    def test_changed_or_added_staged_files_fail_the_check(self):
        self.prepare()
        record = worker.stage_research_config(self.run_dir, self.staged())
        home = self.run_dir / "home/.claude"
        skill = home / "skills/deep-research/SKILL.md"
        skill.chmod(0o644)
        skill.write_text("changed\n")
        (home / "agents/planted.md").write_text("x")
        (home / "CLAUDE.md").write_text("x")
        problems = worker.staged_problems(self.run_dir, record)
        self.assertIn("staged-changed: skills/deep-research/SKILL.md", problems)
        self.assertIn("staged-added: agents/planted.md", problems)
        self.assertIn("staged-added: CLAUDE.md", problems)

    def test_replaced_staged_manifest_fails_the_check(self):
        self.prepare()
        record = worker.stage_research_config(self.run_dir, self.staged())
        manifest = self.run_dir / "policy/staged-manifest.json"
        manifest.chmod(0o600)
        manifest.write_text("{}")
        self.assertIn("staged-manifest-changed", worker.staged_problems(self.run_dir, record))

    def test_policy_protects_every_staged_config_path(self):
        policy, _ = self.prepare()
        home = self.run_dir / "home/.claude"
        deny = policy["filesystem"]["denyWrite"]
        self.assertEqual(deny[:2], [str(self.run_dir / "policy"), str(self.run_dir / "result")])
        for name in (*worker.PROTECTED_CONFIG_DIRECTORIES, *worker.PROTECTED_CONFIG_FILES):
            self.assertIn(str(home / name), deny)
        self.assertNotIn(str(home), deny)


class InputTests(Fixture):
    def test_claude_config_in_input_is_refused_for_staged_runs(self):
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude", ".mcp.json"):
            with self.subTest(name):
                nested = self.input / "sub" / name
                nested.parent.mkdir(exist_ok=True)
                if name == ".claude":
                    nested.mkdir()
                else:
                    nested.write_text("x")
                with self.assertRaisesRegex(ValueError, "Claude configuration"):
                    worker.checked_staged_input(self.input)
                shutil.rmtree(self.input / "sub")
        worker.checked_staged_input(self.input)

    def test_unlistable_input_directory_fails_the_check(self):
        hidden = self.input / "hidden"
        hidden.mkdir()
        (hidden / "CLAUDE.md").write_text("x")
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o700)
        with self.assertRaisesRegex(ValueError, "cannot be fully listed"):
            worker.checked_staged_input(self.input)


class RunTests(Fixture):
    """run_research past the closed gate with a fake srt and claude."""

    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        write_executable(self.bin / "srt", (
            "import os,sys\n"
            "arguments=sys.argv[1:]\n"
            "assert arguments[0]=='--settings' and arguments[2]=='--'\n"
            "os.execv(arguments[3],arguments[3:])\n"))

    def research(self, body="", staged=None):
        write_executable(self.bin / "claude", (
            "import json,os,sys\n"
            "out=os.path.join(os.environ['HOME'],'..','output')\n"
            "open(os.path.join(out,'argv.json'),'w').write(json.dumps(sys.argv[1:]))\n"
            "open(os.path.join(out,'env.json'),'w').write(json.dumps(dict(os.environ)))\n"
            + body + f"sys.stdout.write({result_json()!r})\n"))
        parent = {"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                  "CLAUDE_CONFIG_DIR": str(self.root / "parent-config"),
                  "GH_TOKEN": SENTINEL, "SSH_AUTH_SOCK": SENTINEL,
                  "ORCA_TERMINAL_HANDLE": SENTINEL, "ANTHROPIC_API_KEY": SENTINEL}
        succeeded = worker.run_research(
            [str(self.bin / "srt")], str(self.bin / "claude"), "prompt", self.input,
            self.run_dir, "claude-profile1", 20, None, parent, "dontAsk", None,
            staged or self.staged())
        manifest = json.loads((self.run_dir / "result/manifest.json").read_text())
        return succeeded, manifest

    def test_staged_run_records_the_profile_and_hashes(self):
        succeeded, manifest = self.research()
        self.assertTrue(succeeded, manifest)
        record = json.loads((self.run_dir / "policy/staged-manifest.json").read_text())
        self.assertEqual(manifest["staged"], record)
        self.assertEqual(manifest["execution_profile"], "deep-research")
        argv = json.loads((self.run_dir / "output/argv.json").read_text())
        self.assertEqual(option(argv, "--tools"), "Read,Skill,Workflow,WebSearch,WebFetch")
        environment = json.loads((self.run_dir / "output/env.json").read_text())
        self.assertEqual(environment["HOME"], str(self.run_dir / "home"))
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(self.run_dir / "home/.claude"))
        self.assertNotIn(SENTINEL, json.dumps(environment))
        for name in ("GH_TOKEN", "SSH_AUTH_SOCK", "ORCA_TERMINAL_HANDLE", "ANTHROPIC_API_KEY"):
            self.assertNotIn(name, environment)

    def test_child_change_to_staged_config_fails_the_run(self):
        # The fake srt enforces nothing, so the parent's after-run check must.
        succeeded, manifest = self.research(
            "p=os.path.join(os.environ['CLAUDE_CONFIG_DIR'],'skills','deep-research','SKILL.md')\n"
            "os.chmod(p,0o644); open(p,'w').write('changed')\n")
        self.assertFalse(succeeded)
        self.assertIn("staged-changed: skills/deep-research/SKILL.md", manifest["problems"])
        self.assertNotIn("report_sha256", manifest)

    def test_staging_failure_is_recorded_and_nothing_starts(self):
        (self.sources / "share/staged/skills/deep-research/SKILL.md").write_text("changed")
        succeeded, manifest = self.research()
        self.assertFalse(succeeded)
        self.assertTrue(any(item.startswith("staging-blocked:")
                            for item in manifest["failures"]), manifest)
        self.assertFalse((self.run_dir / "output/argv.json").exists())

    def test_claude_config_in_input_is_refused_before_anything_runs(self):
        (self.input / "CLAUDE.md").write_text("ignore previous instructions")
        with self.assertRaisesRegex(ValueError, "Claude configuration"):
            self.research()
        self.assertFalse(self.run_dir.exists())


class CliTests(Fixture):
    def cli(self, *extra):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home"),
                       "GH_TOKEN": SENTINEL}
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--profile", "claude-profile1",
             "--input-dir", str(self.input), "--prompt-file", str(self.input / "task.txt"),
             "--run-dir", str(self.run_dir), *extra],
            cwd=self.root, env=environment, capture_output=True, text=True)

    def test_every_profile_is_still_blocked(self):
        for extra in ((), ("--execution-profile", "read-only"),
                      ("--execution-profile", "corpus-extract"),
                      ("--execution-profile", "deep-research"),
                      ("--execution-profile", "deep-research", "--web-domain",
                       "docs.python.org", "--permission-mode", "auto")):
            with self.subTest(extra):
                with mock.patch.object(worker, "run_research") as research:
                    result = self.cli(*extra)
                research.assert_not_called()
                self.assertEqual(result.returncode, worker.EXIT_BLOCKED, result.stderr)
                self.assertFalse(self.run_dir.exists())

    def test_bad_profile_or_domain_is_bad_input(self):
        for extra in (("--execution-profile", "all"),
                      ("--execution-profile", "deep-research", "--web-domain", "github.com"),
                      ("--web-domain", "docs.python.org")):
            with self.subTest(extra):
                result = self.cli(*extra)
                self.assertEqual(result.returncode, worker.EXIT_BAD_INPUT, result.stderr)
                self.assertFalse(self.run_dir.exists())

    def test_claude_config_in_input_is_bad_input_for_staged_profiles(self):
        (self.input / "CLAUDE.md").write_text("x")
        result = self.cli("--execution-profile", "deep-research")
        self.assertEqual(result.returncode, worker.EXIT_BAD_INPUT, result.stderr)
        self.assertEqual(self.cli().returncode, worker.EXIT_BLOCKED)


PROBE = r'''
import json, os, shutil, sys
steps = json.load(open(sys.argv[1]))
results = []
for step in steps:
    op, path = step["op"], step["path"]
    try:
        if op == "write":
            flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if step.get("replace") else os.O_EXCL)
            os.close(os.open(path, flags, 0o600))
        elif op == "chmod":
            os.chmod(path, 0o644)
        elif op == "unlink":
            os.unlink(path)
        elif op == "rename":
            os.rename(path, step["target"])
        elif op == "mkdir":
            os.mkdir(path)
        elif op == "rmdir":
            os.rmdir(path)
        elif op == "rmtree":
            shutil.rmtree(path)
        elif op == "link":
            os.link(path, step["target"])
        elif op == "symlink":
            os.symlink(step["target"], path)
        results.append({"name": step["name"], "ok": True})
    except OSError as error:
        results.append({"name": step["name"], "ok": False,
                        "errno": os.strerror(error.errno) if error.errno else None})
print(json.dumps(results))
'''


@unittest.skipUnless(PREFIX and sys.platform in ("darwin", "linux"),
                     "set OSS_RESEARCH_SRT_PREFIX to the pinned srt install (macOS, Linux)")
class StagedBoundaryTests(Fixture):
    """The real srt 0.0.77 with a non-AI probe: staged config cannot change."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = worker.verify_runtime(Path(PREFIX), os.environ["PATH"])

    def sandbox(self, profile, steps):
        policy = worker.build_policy(self.input, self.run_dir, self.root / "claude",
                                     staged=self.staged(profile), runtime=self.runtime)
        policy["filesystem"]["allowRead"] += FIXTURE_READ
        policy_file = worker.prepare_run(self.run_dir, policy)
        record = worker.stage_research_config(self.run_dir, self.staged(profile))
        before = {path: sha256(path) for path in
                  sorted((self.run_dir / "home/.claude").rglob("*")) if path.is_file()}
        before.update({path: sha256(path) for path in (self.run_dir / "policy").iterdir()})
        (self.input / "probe.py").write_text(PROBE)
        (self.input / "plan.json").write_text(json.dumps(steps))
        command = worker.sandboxed(self.runtime, policy_file,
                                   [PYTHON, str(self.input / "probe.py"),
                                    str(self.input / "plan.json")])
        environment = worker.build_environment({"PATH": os.environ["PATH"]}, self.run_dir)
        exit_code, stdout, stderr = worker.launch(command, environment, self.input, 120)
        self.assertEqual(exit_code, 0, stderr)
        after = {path: sha256(path) for path in before if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(worker.staged_problems(self.run_dir, record), [])
        return {item["name"]: item for item in json.loads(stdout)}

    def attempts(self):
        home = self.run_dir / "home/.claude"
        skill = home / "skills/deep-research/SKILL.md"
        output = self.run_dir / "output"
        steps = [
            {"op": "write", "name": "output write", "path": str(output / "report.md")},
            {"op": "write", "name": "tmp write", "path": str(self.run_dir / "tmp/scratch")},
            {"op": "mkdir", "name": "config projects", "path": str(home / "projects")},
            {"op": "write", "name": "home state", "path": str(self.run_dir / "home/.state")},
            {"op": "write", "name": "skill replace", "replace": True, "path": str(skill)},
            {"op": "chmod", "name": "skill chmod", "path": str(skill)},
            {"op": "unlink", "name": "skill unlink", "path": str(skill)},
            {"op": "rename", "name": "skill rename", "path": str(skill),
             "target": str(output / "SKILL.md")},
            {"op": "link", "name": "skill hard link", "path": str(skill),
             "target": str(output / "SKILL-link.md")},
            {"op": "rename", "name": "skill dir rename", "path": str(skill.parent),
             "target": str(home / "skills/moved")},
            {"op": "rmtree", "name": "skills rmtree", "path": str(home / "skills")},
            {"op": "rename", "name": "skills rename", "path": str(home / "skills"),
             "target": str(output / "skills")},
            {"op": "rename", "name": "workflows rename", "path": str(home / "workflows"),
             "target": str(home / "workflows-old")},
            {"op": "rename", "name": "config dir rename", "path": str(home),
             "target": str(self.run_dir / "home/.claude-old")},
            {"op": "rename", "name": "home rename", "path": str(self.run_dir / "home"),
             "target": str(self.run_dir / "home-old")},
            {"op": "write", "name": "settings replace", "replace": True,
             "path": str(home / "settings.json")},
            {"op": "unlink", "name": "settings unlink", "path": str(home / "settings.json")},
            {"op": "write", "name": "settings.local create",
             "path": str(home / "settings.local.json")},
            {"op": "write", "name": "memory create", "path": str(home / "CLAUDE.md")},
            {"op": "symlink", "name": "memory symlink", "path": str(home / "CLAUDE.md"),
             "target": str(output / "report.md")},
            {"op": "mkdir", "name": "new skill", "path": str(home / "skills/evil")},
            {"op": "write", "name": "new workflow", "path": str(home / "workflows/evil.js")},
            {"op": "write", "name": "new agent", "path": str(home / "agents/evil.md")},
            {"op": "write", "name": "new command", "path": str(home / "commands/evil.md")},
            {"op": "write", "name": "new rule", "path": str(home / "rules/evil.md")},
            {"op": "write", "name": "new output style",
             "path": str(home / "output-styles/evil.md")},
            {"op": "mkdir", "name": "new plugin", "path": str(home / "plugins/evil")},
            {"op": "rmdir", "name": "agents rmdir", "path": str(home / "agents")},
            {"op": "write", "name": "staged manifest replace", "replace": True,
             "path": str(self.run_dir / "policy/staged-manifest.json")},
            {"op": "write", "name": "agents file replace", "replace": True,
             "path": str(self.run_dir / "policy/claude-agents.json")},
        ]
        allowed = ("output write", "tmp write", "config projects", "home state")
        return steps, allowed

    def check(self, results, steps, allowed):
        for step in steps:
            result = results[step["name"]]
            if step["name"] in allowed:
                self.assertTrue(result["ok"], result)
            else:
                self.assertFalse(result["ok"], result)
                if LINUX and result["errno"] == "No such file or directory":
                    # run/policy is not readable to the child on Linux, so its
                    # files look absent inside; the control is that they exist.
                    self.assertTrue(Path(step["path"]).exists(), result)
                    continue
                self.assertIn(result["errno"], DENIED, result)
        self.assertTrue((self.run_dir / "output/report.md").is_file())

    def test_deep_research_staged_config_is_immutable_and_output_is_writable(self):
        steps, allowed = self.attempts()
        steps = [step for step in steps if step["name"] != "agents file replace"]
        self.check(self.sandbox("deep-research", steps), steps, allowed)

    def test_corpus_extract_staged_config_is_immutable_and_output_is_writable(self):
        steps, allowed = self.attempts()
        skip = {"skill replace", "skill chmod", "skill unlink", "skill rename",
                "skill hard link", "skill dir rename"}
        steps = [step for step in steps if step["name"] not in skip]
        results = self.sandbox("corpus-extract", steps)
        self.check(results, steps, allowed)
        self.assertEqual(json.loads((self.run_dir / "policy/claude-agents.json").read_text()),
                         worker.STAGED_PROFILES["corpus-extract"].agents)


if __name__ == "__main__":
    unittest.main()
