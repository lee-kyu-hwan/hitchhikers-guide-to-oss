"""This machine's settings are read from the environment with HOME-based defaults (#290, #9).

machine_settings.py (VM root, VM logs, dated run records) and harness/analysis_call.sh (VM root, Lima instance):
unset or empty gives the default (the VM root is ~/.oss-research-vm under HOME, so every case here runs with a
fake HOME), a set variable gives that value. The harness case runs as an unchanged copy against a fake limactl
under a temporary VM root, so no VM, model or token is used.

  /opt/homebrew/bin/python3 -B -m unittest skills/researching-open-source-contributions/analysis/test_machine_settings.py
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

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
HARNESS = HERE / "harness/analysis_call.sh"
VARIABLES = ("OSS_RESEARCH_VM_ROOT", "OSS_RESEARCH_VM_NAME", "OSS_RESEARCH_RECORDS_ROOT")
# The defaults, written out here on purpose. The VM root hangs off HOME (#9: no personal home path in the code),
# so the cases run with this fake HOME; the records root is this repository's copy of the dotfiles records
# (tests/fixtures/dotfiles_records). The harness's setting line is counted verbatim.
FAKE_HOME = "/srv/home/runner"
OLD_VM_ROOT = FAKE_HOME + "/.oss-research-vm"
OLD_VM_NAME = "oss-research-debian"
OLD_RECORDS_ROOT = str(REPO / "tests/fixtures/dotfiles_records")
HARNESS_VM_ROOT_LINE = "W=${OSS_RESEARCH_VM_ROOT:-$HOME/.oss-research-vm}\n"
FAKE_LIMACTL = """#!/bin/bash
echo "$*" >> "$(dirname "$0")/../../calls.log"
case "$*" in
  *"cat > "*task.txt*) shasum -a 256 | cut -c1-64 ;;
  *model_once.sh*) cat > /dev/null; echo "SUMMARY fake 0 failures" ;;
esac
exit 0
"""


def environment(**values):
    env = {key: value for key, value in os.environ.items() if key not in VARIABLES}
    env["HOME"] = FAKE_HOME
    env.update(values)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def settings(**values):
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import machine_settings as m; "
            "print(json.dumps([str(m.VM_ROOT), str(m.VM_LOGS), str(m.RECORDS_ROOT)]))")
    done = subprocess.run([sys.executable, "-B", "-c", code, str(HERE)], capture_output=True, text=True,
                          env=environment(**values), timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def harness_settings(**values):
    """The harness's own setting lines, run by bash: the values W and VM end up with."""
    lines = [line for line in HARNESS.read_text().splitlines() if line.startswith(("W=", "VM="))]
    script = "\n".join(lines + ['printf "%s\\n%s\\n" "$W" "$VM"'])
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment(**values),
                          timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


class ModuleTests(unittest.TestCase):
    def test_unset_keeps_the_old_values(self):
        self.assertEqual(settings(), [OLD_VM_ROOT, OLD_VM_ROOT + "/logs", OLD_RECORDS_ROOT])

    def test_empty_keeps_the_old_values(self):
        self.assertEqual(settings(OSS_RESEARCH_VM_ROOT="", OSS_RESEARCH_RECORDS_ROOT=""),
                         [OLD_VM_ROOT, OLD_VM_ROOT + "/logs", OLD_RECORDS_ROOT])

    def test_the_default_follows_home(self):
        self.assertEqual(settings(HOME="/home/other")[:2], ["/home/other/.oss-research-vm", "/home/other/.oss-research-vm/logs"])
        self.assertEqual(harness_settings(HOME="/home/other"), ["/home/other/.oss-research-vm", OLD_VM_NAME])

    def test_set_values_are_used(self):
        self.assertEqual(settings(OSS_RESEARCH_VM_ROOT="/srv/vm", OSS_RESEARCH_RECORDS_ROOT="/srv/dotfiles/docs/research"),
                         ["/srv/vm", "/srv/vm/logs", "/srv/dotfiles/docs/research"])

    def test_a_test_reading_vm_evidence_follows_the_setting(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import test_model_retry as t; print(t.SPLIT_GATE)")
        for values, root in (({}, OLD_VM_ROOT), ({"OSS_RESEARCH_VM_ROOT": "/srv/vm"}, "/srv/vm")):
            done = subprocess.run([sys.executable, "-B", "-c", code, str(HERE)], capture_output=True, text=True,
                                  env=environment(**values), timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), root + "/logs/a78-split-gate-20261006T055754Z")


class HarnessTests(unittest.TestCase):
    def test_the_setting_lines_keep_the_old_values_unless_set(self):
        self.assertEqual(harness_settings(), [OLD_VM_ROOT, OLD_VM_NAME])
        self.assertEqual(harness_settings(OSS_RESEARCH_VM_ROOT="", OSS_RESEARCH_VM_NAME=""), [OLD_VM_ROOT, OLD_VM_NAME])
        self.assertEqual(harness_settings(OSS_RESEARCH_VM_ROOT="/srv/vm", OSS_RESEARCH_VM_NAME="other-vm"),
                         ["/srv/vm", "other-vm"])

    def test_the_instance_is_named_only_in_its_setting(self):
        text = HARNESS.read_text()
        self.assertEqual(text.count(OLD_VM_NAME), 1)
        self.assertEqual(text.count(HARNESS_VM_ROOT_LINE), 1)
        self.assertNotIn("/Users/", text)
        self.assertEqual(text.count('L shell --workdir / "$VM" --'), 4)

    def call(self, **values):
        """One sentinel call of an unchanged harness copy whose VM root (by the setting) holds a fake limactl."""
        root = Path(tempfile.mkdtemp(prefix="osrl-ms-")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        vm = root / "vm"
        (vm / "tool/bin").mkdir(parents=True)
        (vm / "tool/bin/limactl").write_text(FAKE_LIMACTL)
        (vm / "tool/bin/limactl").chmod(0o755)
        harness = root / "repo" / HARNESS.relative_to(REPO)
        harness.parent.mkdir(parents=True)
        shutil.copy(HARNESS, harness)
        (root / "task.txt").write_text("analyse these PRs\n")
        (root / "cred").write_text("sentinel-" + "c" * 40 + "\n")
        done = subprocess.run(["bash", str(harness), str(root / "ledger"), "call-1", str(root / "task.txt"), "0" * 40,
                               "token", str(root / "cred")], capture_output=True, text=True,
                              env=environment(OSS_RESEARCH_VM_ROOT=str(vm), **values), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return (vm / "calls.log").read_text().splitlines()

    def test_the_vm_root_setting_reaches_limactl_and_the_old_instance_is_the_default(self):
        calls = self.call()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.startswith("shell --workdir / %s -- " % OLD_VM_NAME) for call in calls), calls)

    def test_a_set_instance_is_the_one_limactl_is_given(self):
        calls = self.call(OSS_RESEARCH_VM_NAME="other-vm")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.startswith("shell --workdir / other-vm -- ") for call in calls), calls)


if __name__ == "__main__":
    unittest.main()
