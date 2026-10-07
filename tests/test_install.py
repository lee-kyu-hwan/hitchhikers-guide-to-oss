"""Tests for install.sh (hitchhikers-guide-to-oss#1).

Every scenario runs the script as a subprocess against a temporary HOME, a
temporary BIN_DIR, and a temporary project directory. The real home of the
user running the tests is never read or written. S3 then runs the workflow
skill from the project-scope links (not from the checkout) and checks that it
finds its sibling skills through its own realpath parent, with no
``~/.codex/skills`` or ``~/.agents/skills`` present in the temporary HOME.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SKILLS = ROOT / "skills"
BIN = ROOT / "bin"
WORKFLOW_SKILL = "researching-open-source-contributions"
WORKFLOW_ITEMS = ("SKILL.md", "scripts", "references", "tests")
SKILL_NAMES = sorted(path.name for path in SKILLS.iterdir() if path.is_dir())
EXECUTABLES = sorted(path.name for path in BIN.iterdir() if path.name.startswith("oss-research-"))
SCOPE_DIRS = (".agents/skills", ".claude/skills")


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.assertEqual(6, len(SKILL_NAMES), SKILL_NAMES)
        self.assertEqual(12, len(EXECUTABLES), EXECUTABLES)
        self.tmp = Path(tempfile.mkdtemp(prefix="oss-guide-install-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.bin_dir = self.tmp / "bin"
        self.project = self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()

    def run_install(self, *args, env_overrides=None, expect=0):
        env = {
            "HOME": str(self.home),
            "BIN_DIR": str(self.bin_dir),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(env_overrides or {})
        result = subprocess.run([str(INSTALL), *args], cwd=str(self.tmp), env=env,
                                capture_output=True, text=True, timeout=60)
        if expect is not None:
            self.assertEqual(expect, result.returncode, result.stdout + result.stderr)
        return result

    # -- helpers --------------------------------------------------------------

    def skill_link(self, base: Path, skill: str) -> Path:
        return base / skill

    def expected_links(self, scope_roots):
        """Every (link, target) pair the install must create for the given scope roots."""
        pairs = []
        for base in scope_roots:
            for skill in SKILL_NAMES:
                if skill == WORKFLOW_SKILL:
                    for item in WORKFLOW_ITEMS:
                        pairs.append((base / skill / item, SKILLS / skill / item))
                else:
                    pairs.append((base / skill, SKILLS / skill))
        for name in EXECUTABLES:
            pairs.append((self.bin_dir / name, BIN / name))
        return pairs

    def assert_installed(self, scope_roots):
        for link, target in self.expected_links(scope_roots):
            self.assertTrue(link.is_symlink(), "missing link: %s" % link)
            self.assertEqual(str(target), os.readlink(link), "wrong target: %s" % link)
        for base in scope_roots:
            workflow = base / WORKFLOW_SKILL
            self.assertTrue(workflow.is_dir() and not workflow.is_symlink(), workflow)
            self.assertEqual(sorted(WORKFLOW_ITEMS), sorted(path.name for path in workflow.iterdir()))
            self.assertFalse((workflow / "analysis").exists(), "analysis/ must not be exposed")
            self.assertTrue((SKILLS / WORKFLOW_SKILL / "analysis").is_dir(), "the checkout still has analysis/")

    def project_roots(self):
        return [self.project / rel for rel in SCOPE_DIRS]

    def all_links(self):
        found = []
        for base in (self.project, self.home, self.bin_dir):
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                for name in dirnames + filenames:
                    path = Path(dirpath) / name
                    if path.is_symlink():
                        found.append(path)
        return sorted(found)


class ProjectInstallTests(InstallCase):
    """S1: an empty project gets every link, --check passes, and a second run changes nothing."""

    def test_project_install_links_six_skills_and_twelve_executables(self):
        result = self.run_install("--project", str(self.project))
        self.assert_installed(self.project_roots())
        self.assertEqual(len(self.expected_links(self.project_roots())), len(self.all_links()))
        self.assertIn("link", result.stdout)
        # Nothing is written under HOME for a project-scope install.
        self.assertEqual([], sorted(self.home.iterdir()))
        check = self.run_install("--check", "--project", str(self.project))
        self.assertIn("check: 30 ok, 0 missing, 0 conflict (of 30)", check.stdout)
        self.assertEqual(30, check.stdout.count("\nok ") + check.stdout.startswith("ok "))

    def test_project_install_is_idempotent(self):
        self.run_install("--project", str(self.project))
        before = self.all_links()
        again = self.run_install("--project", str(self.project))
        self.assertEqual(before, self.all_links())
        self.assertEqual(before, [path for path in before if path.is_symlink()])
        # The second run reports every link as already present and creates none.
        self.assertNotIn("link ", again.stdout.replace("linked", ""))
        self.assertIn("ok", again.stdout)

    def test_check_before_install_fails_and_names_missing_links(self):
        check = self.run_install("--check", "--project", str(self.project), expect=1)
        self.assertIn("missing", check.stdout)
        self.assertEqual([], self.all_links())

    def test_bin_dir_env_overrides_default_and_home_default_is_local_bin(self):
        self.run_install("--project", str(self.project), env_overrides={"BIN_DIR": ""})
        default_bin = self.home / ".local" / "bin"
        for name in EXECUTABLES:
            self.assertEqual(str(BIN / name), os.readlink(default_bin / name))
        self.assertFalse(self.bin_dir.exists())

    def test_project_must_exist(self):
        result = self.run_install("--project", str(self.tmp / "absent"), expect=2)
        self.assertEqual([], self.all_links())
        self.assertIn("absent", result.stderr)

    def test_scope_is_required(self):
        self.run_install(expect=2)
        self.run_install("--check", expect=2)
        self.assertEqual([], self.all_links())


class UserInstallTests(InstallCase):
    """--user links the skills under ~/.agents/skills of the given HOME."""

    def test_user_install_links_into_agents_skills(self):
        self.run_install("--user")
        roots = [self.home / ".agents" / "skills"]
        self.assert_installed(roots)
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(len(self.expected_links(roots)), len(self.all_links()))
        self.run_install("--check", "--user")
        self.run_install("--uninstall", "--user")
        self.assertEqual([], self.all_links())


class ConflictTests(InstallCase):
    """S2: a foreign symlink or a real file stops the install; --force replaces only links and files."""

    def test_foreign_symlink_stops_the_whole_install(self):
        base = self.project / ".agents" / "skills"
        base.mkdir(parents=True)
        foreign = self.tmp / "elsewhere"
        foreign.mkdir()
        (base / "collecting-recent-closed-prs").symlink_to(foreign)
        result = self.run_install("--project", str(self.project), expect=1)
        self.assertIn("conflict", result.stdout + result.stderr)
        self.assertIn("collecting-recent-closed-prs", result.stdout + result.stderr)
        # Nothing else was created: the conflict is detected before any link is made.
        self.assertEqual([base / "collecting-recent-closed-prs"], self.all_links())
        self.assertFalse((self.project / ".claude").exists())
        self.assertFalse(self.bin_dir.exists())
        self.assertEqual(str(foreign), os.readlink(base / "collecting-recent-closed-prs"))

    def test_force_replaces_foreign_symlink(self):
        base = self.project / ".agents" / "skills"
        base.mkdir(parents=True)
        foreign = self.tmp / "elsewhere"
        foreign.mkdir()
        (base / "collecting-recent-closed-prs").symlink_to(foreign)
        result = self.run_install("--project", str(self.project), "--force")
        self.assertIn("replace", result.stdout)
        self.assert_installed(self.project_roots())
        self.assertTrue(foreign.is_dir(), "the old target itself is never removed")

    def test_regular_file_stops_without_force(self):
        self.bin_dir.mkdir()
        (self.bin_dir / "oss-research-worker").write_text("#!/bin/sh\n", encoding="utf-8")
        result = self.run_install("--project", str(self.project), expect=1)
        self.assertIn("conflict", result.stdout + result.stderr)
        self.assertEqual([], self.all_links())
        self.assertEqual("#!/bin/sh\n", (self.bin_dir / "oss-research-worker").read_text(encoding="utf-8"))
        self.run_install("--project", str(self.project), "--force")
        self.assert_installed(self.project_roots())

    def test_directory_in_the_way_stops_even_with_force(self):
        base = self.project / ".claude" / "skills"
        (base / "verifying-open-source-contribution-candidates").mkdir(parents=True)
        (base / "verifying-open-source-contribution-candidates" / "SKILL.md").write_text("x\n", encoding="utf-8")
        for args in ((), ("--force",)):
            result = self.run_install("--project", str(self.project), *args, expect=1)
            self.assertIn("conflict", result.stdout + result.stderr)
            self.assertEqual([], self.all_links())
        self.assertTrue((base / "verifying-open-source-contribution-candidates" / "SKILL.md").is_file())

    def test_check_reports_conflict_and_does_not_change_anything(self):
        self.run_install("--project", str(self.project))
        link = self.project / ".agents" / "skills" / "analyzing-open-source-pr-patterns"
        link.unlink()
        link.symlink_to(self.tmp)
        result = self.run_install("--check", "--project", str(self.project), expect=1)
        self.assertIn("conflict", result.stdout)
        self.assertEqual(str(self.tmp), os.readlink(link))


class UninstallTests(InstallCase):
    """S4: --uninstall removes only the links that point into this checkout."""

    def test_uninstall_removes_every_link_and_empty_directories(self):
        self.run_install("--project", str(self.project))
        self.assertTrue(self.all_links())
        result = self.run_install("--uninstall", "--project", str(self.project))
        self.assertEqual([], self.all_links())
        self.assertIn("unlink", result.stdout)
        for rel in SCOPE_DIRS:
            self.assertFalse((self.project / rel / WORKFLOW_SKILL).exists())
        self.assertEqual([], sorted(self.project.iterdir()), "empty .agents/.claude trees are removed")
        self.assertFalse(self.bin_dir.exists() and list(self.bin_dir.iterdir()))
        # Uninstalling twice is harmless and --check now reports everything missing.
        self.run_install("--uninstall", "--project", str(self.project))
        self.run_install("--check", "--project", str(self.project), expect=1)

    def test_uninstall_keeps_foreign_links_and_other_files(self):
        self.run_install("--project", str(self.project))
        foreign = self.project / ".agents" / "skills" / "someone-elses-skill"
        foreign.symlink_to(self.tmp)
        note = self.project / ".claude" / "settings.json"
        note.write_text("{}\n", encoding="utf-8")
        other_bin = self.bin_dir / "unrelated"
        other_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        mine = self.project / ".agents" / "skills" / "collecting-open-source-issues"
        mine.unlink()
        mine.symlink_to(self.tmp / "elsewhere")
        result = self.run_install("--uninstall", "--project", str(self.project))
        self.assertEqual(sorted([foreign, mine]), self.all_links())
        self.assertIn("skip", result.stdout)
        self.assertTrue(note.is_file())
        self.assertTrue(other_bin.is_file())


class SiblingResolutionFromProjectInstallTests(InstallCase):
    """S3: the workflow skill run from the project links finds its siblings beside its real file."""

    def test_workflow_init_resolves_siblings_from_project_links(self):
        self.run_install("--project", str(self.project))
        script = self.project / ".agents" / "skills" / WORKFLOW_SKILL / "scripts" / "oss_workflow.py"
        self.assertTrue(script.is_file())
        self.assertTrue((self.project / ".agents" / "skills" / WORKFLOW_SKILL / "scripts").is_symlink())
        request = self.tmp / "request.json"
        request.write_text(json.dumps({
            "schema_version": "1.0.0",
            "mode": "offline-fixture",
            "source": {"kind": "recent-closed-prs", "repositories": ["example-org/example-repo"],
                       "interval": {"start_at": "2026-09-01T00:00:00Z", "end_at": "2026-09-08T00:00:00Z"},
                       "outcome": "all", "max_per_repository": 5, "request_budget": 200,
                       "reference_repositories": []},
            "target": {"repositories": ["example-org/example-repo"], "max_candidates_per_repo": 5,
                       "max_candidates_total": 20, "discover_request_budget": 60,
                       "locus_request_budget": 40, "allow_clone": False},
            "budget": {"total_api_requests": 400, "max_elapsed_seconds": None},
            "static_only": True,
            "provenance": {"model": "unknown", "effort": "unknown"},
        }), encoding="utf-8")
        run_dir = self.tmp / "run"
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C"}
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".agents").exists())
        result = subprocess.run([sys.executable, str(script), "init", "--run-dir", str(run_dir),
                                 "--request", str(request)], cwd=str(self.tmp), env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        resolution = state["skills"]["resolution"]
        self.assertEqual("shared-skill-parent", resolution["source"])
        self.assertEqual(os.path.realpath(SKILLS), resolution["realpath"])
        for key, path in resolution["scripts"].items():
            self.assertTrue(Path(path).is_file(), (key, path))
            self.assertTrue(Path(path).is_relative_to(SKILLS.resolve()), (key, path))
        self.assertEqual(sorted(["analyzer", "collector", "curated", "verifier", "workflow"]),
                         sorted(state["skills"]["revisions"]))
        self.assertEqual([], list((ROOT / "bin").glob("__pycache__")))


if __name__ == "__main__":
    unittest.main()
