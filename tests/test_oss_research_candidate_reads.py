"""The non-AI candidate-verifier read entry and its host pickup contract, offline.

    python3 -B -m unittest tests.test_oss_research_candidate_reads

Entry: limits, no model path, the read set (the verifier's REVISION_PATHS), the
policy and child argv under a fake supervisor, the record (complete, partial and
failed), the input checks and main's refusals. Pickup: verify_pickup.py
--candidate-reads on tars built from a real offline run (real relay and real
adapter against a loopback mock, no srt) and edited one member at a time. No
network, srt or VM is used here; the real run is on the Linux guest.
"""

import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "dot_local/bin"
SKILLS = REPO / "dot_codex/skills"
VERIFY = REPO / "dot_local/share/oss-research/linux/verify_pickup.py"
VERIFIER_SOURCE = SKILLS / "verifying-open-source-contribution-candidates/scripts/verify_candidates.py"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


ENTRY = BIN / "executable_oss-research-candidate-reads"
entry = load("oss_research_candidate_reads", ENTRY)
collect_entry = entry.load_collect_entry()
worker = collect_entry.load_worker()
wrapper = load("oss_research_linux_run_for_candidate_reads", BIN / "executable_oss-research-linux-run")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


HOST_VERIFIER_SHA = sha256(VERIFIER_SOURCE)
HOST_ISSUES_SHA = sha256(SKILLS / "collecting-open-source-issues/scripts/collect_open_source_issues.py")
HOST_SIBLING_SHA = sha256(SKILLS / "collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py")

# The target, the analysis and the loopback mock api.github.com are the relay-verify
# tests' (read-only: this file owns none of it).
import tests.test_oss_research_relay_verify as relay_verify_tests  # noqa: E402

TARGET = relay_verify_tests.TARGET
LOCUS = "validation boundary"


def setUpModule():
    relay_verify_tests.setUpModule()  # the loopback-only network guard


def tearDownModule():
    relay_verify_tests.tearDownModule()


def add_locus_routes(routes):
    """Search routes for the clues the verifier derives from LOCUS (empty issue searches, a 401 code search)."""
    quote = relay_verify_tests.urllib.parse.quote_plus
    key = relay_verify_tests._query_key
    for clue in relay_verify_tests.verifier._locus_clues(LOCUS):
        for kind in ("issue", "pr"):
            for state in ("open", "closed"):
                query = 'repo:%s "%s" is:%s is:%s' % (TARGET, clue, kind, state)
                routes[("/search/issues", key("q=" + quote(query)))] = {
                    "status": 200, "payload": {"total_count": 0, "incomplete_results": False, "items": []}}
        routes[("/search/code", key("q=" + quote('repo:%s "%s"' % (TARGET, clue))))] = {
            "status": 401, "payload": {"message": "Requires authentication"}}


def assessment_for(discovery_path: Path) -> dict:
    """A synthetic assessment of every discovery record (shape only; no real analysis exists yet)."""
    discovery = json.loads(discovery_path.read_text())
    digest = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
    return {"schema_version": "1.0.0", "discovery_sha256": digest, "assessments": [
        {"pattern_id": record["pattern_id"], "repository": record["repository"], "locus": LOCUS,
         "status": "insufficient", "status_reason": "synthetic fixture", "sensitivity": "normal",
         "summary": "synthetic", "impact": "synthetic", "readiness_checks": [], "duplicate_verdict": None,
         "ai_policy_status": "unknown", "disclosure_required": None, "private_evidence_reference": None,
         "evidence_links": [], "execution_evidence": None, "reproduction": None, "policy_checks": [],
         "blocking_gaps": [], "superseded_by": None} for record in discovery["records"]]}


def sparse(path, size):
    with open(path, "w"):
        pass
    os.truncate(path, size)


class EntryCase(unittest.TestCase):
    PATTERNS = ["PAT-001"]

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-reads-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def discover(self, **changes):
        values = dict(stage="discover", repository=TARGET, patterns=self.PATTERNS, clues=[],
                      max_candidates_per_repo=5, max_candidates_total=5, max_clues_per_pattern=5,
                      max_clues_per_locus=None, request_budget=60)
        values.update(changes)
        return entry.checked_reads(worker, **values)

    def locus(self, **changes):
        values = dict(stage="locus-search", repository=TARGET, patterns=[], clues=[],
                      max_candidates_per_repo=None, max_candidates_total=None,
                      max_clues_per_pattern=None, max_clues_per_locus=5, request_budget=60)
        values.update(changes)
        return entry.checked_reads(worker, **values)

    def inputs(self, name, files):
        directory = self.root / name
        directory.mkdir()
        for filename, content in files.items():
            (directory / filename).write_text(content if isinstance(content, str) else json.dumps(content))
        directory.chmod(0o755)
        for path in directory.iterdir():
            path.chmod(0o644)
        return directory


class CandidateReadsEntryTests(EntryCase):
    def test_limits_are_fixed_and_refused_outside(self):
        self.assertEqual(self.discover().as_request()["request_budget"], 60)
        self.assertEqual(self.discover().repositories, (TARGET, "octo/.github"))
        self.assertEqual(self.discover(repository="octo/.github").repositories, ("octo/.github",))
        self.assertEqual(self.locus(clues=["a" * 200]).clues, ("a" * 200,))
        bad = {"no pattern": dict(patterns=[]), "six patterns": dict(patterns=[f"PAT-00{n}" for n in range(1, 7)]),
               "duplicate pattern": dict(patterns=["PAT-001", "PAT-001"]),
               "not a pattern id": dict(patterns=["pat-1"]), "pattern with option text": dict(patterns=["PAT-1 --x"]),
               "candidates 6": dict(max_candidates_per_repo=6), "candidates 0": dict(max_candidates_total=0),
               "candidates true": dict(max_candidates_per_repo=True), "candidates none": dict(max_candidates_total=None),
               "clues per pattern 6": dict(max_clues_per_pattern=6), "budget 61": dict(request_budget=61),
               "budget 0": dict(request_budget=0), "budget true": dict(request_budget=True),
               "clue on discover": dict(clues=["x"]), "locus cap on discover": dict(max_clues_per_locus=1),
               "bad repo": dict(repository="octo"), "repo with a slash too many": dict(repository="a/b/c"),
               "repo with an option": dict(repository="octo/demo --allow-clone"), "stage": dict(stage="record")}
        for label, change in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.discover(**change)
        bad = {"pattern on locus": dict(patterns=["PAT-001"]), "candidate cap on locus": dict(max_candidates_total=1),
               "six clues": dict(clues=[f"c{n}" for n in range(6)]), "duplicate clue": dict(clues=["a", "a"]),
               "empty clue": dict(clues=[""]), "201 characters": dict(clues=["a" * 201]),
               "quote": dict(clues=['a"b']), "backslash": dict(clues=["a\\b"]), "newline": dict(clues=["a\nb"]),
               "edge space": dict(clues=[" a"]), "not a string": dict(clues=[1]),
               "locus cap 6": dict(max_clues_per_locus=6), "locus cap none": dict(max_clues_per_locus=None),
               "budget 61": dict(request_budget=61)}
        for label, change in bad.items():
            with self.subTest("locus " + label), self.assertRaises(ValueError):
                self.locus(**change)

    def test_the_entry_is_registered_beside_the_others_and_is_not_the_fixture_entry(self):
        self.assertEqual(wrapper.ENTRIES["candidate-reads"], entry.ENTRY)
        self.assertEqual(entry.ENTRY, "dot_local/bin/executable_oss-research-candidate-reads")
        self.assertEqual(set(wrapper.ENTRIES), {"worker", "collect", "collect-issues", "candidate-reads", "verify"})
        self.assertEqual(wrapper.ENTRIES["collect"], "dot_local/bin/executable_oss-research-collect")
        self.assertEqual(wrapper.ENTRIES["collect-issues"], "dot_local/bin/executable_oss-research-collect-issues")
        self.assertEqual(wrapper.ENTRIES["verify"], "tests/fixtures/oss_research_worker/linux/unit_inner.py")
        self.assertNotEqual(entry.MODE, "verify")
        self.assertTrue(os.access(ENTRY, os.X_OK))

    def test_the_entry_has_no_model_path_no_clone_and_no_upstream_option(self):
        code = ENTRY.read_text().split('"""', 2)[2]
        for name in ("load_credential", "checked_credential", "CREDENTIAL_VARIABLE", "require_runtime",
                     "Selection(", "build_command", "run_research", "--model", "--effort",
                     "--permission-mode", "--credential-file", "--prompt-file", "--allow-clone",
                     "--clone-root", "--keep-clone", "--fixture-dir", "--api-version", "GH_TOKEN",
                     "GITHUB_TOKEN", "--upstream", "--base-url", "--markdown-output"):
            self.assertNotIn(name, code, name)
        self.assertIs(worker.SYSTEM_READ_MEASURED["linux"], False)
        options = set(re.findall(r'add_argument\("(--[a-z-]+)"', code))
        self.assertEqual(options, {"--out-dir", "--input-dir", "--stage", "--repo", "--pattern", "--clue",
                                   "--max-candidates-per-repo", "--max-candidates-total",
                                   "--max-clues-per-pattern", "--max-clues-per-locus", "--request-budget",
                                   "--timeout", "--github-token-file", "--static-reads", "--static-dir"})

    def test_the_read_set_is_the_verifiers_revision_files(self):
        verifier = relay_verify_tests.verifier
        self.assertEqual(tuple(sorted(entry.VERIFIER_REVISION_FILES)), tuple(sorted(verifier.REVISION_PATHS)))
        # The entry reads the same list from the source text, without running the verifier.
        self.assertEqual(entry.declared_revision_files(VERIFIER_SOURCE), tuple(verifier.REVISION_PATHS))
        path = entry.verifier_file(worker)
        self.assertEqual(path, VERIFIER_SOURCE.resolve())
        # The adapter loads relay-collect beside itself by path (its ADAPTER constant).
        adapter = load("osr_relay_verify_for_readset", entry.adapter_file(worker))
        self.assertEqual(entry.ADAPTER_DEPENDENCIES, (adapter.ADAPTER,))
        skill = path.parents[1]
        read = entry.candidate_read(worker, "/usr/bin/python3")
        self.assertEqual(read, ["/usr/bin/python3",
                               str(entry.adapter_file(worker).with_name(adapter.ADAPTER)),
                               *[str(skill / f) for f in entry.VERIFIER_REVISION_FILES]])
        for item in read[2:]:
            self.assertTrue(Path(item).is_file(), item)
        self.assertNotIn(str(skill), read)
        self.assertFalse(any("zoneinfo" in item for item in read))
        # The relay hashes the issues collector and the PR sibling, not the verifier.
        _, skills = worker.trusted_relay(Path(worker.__file__))
        sources = {s["role"]: s["path"] for s in worker.relay_sources(skills)}
        self.assertEqual(set(sources), {"issues", "sibling"})
        self.assertNotIn(str(path), sources.values())

    def test_a_changed_revision_list_is_read_from_the_source_text(self):
        changed = self.root / "verify_candidates.py"
        changed.write_text("REVISION_PATHS = (\n    'SKILL.md',\n    'scripts/verify_candidates.py',\n)\n"
                           "raise SystemExit('never run')\n")
        declared = entry.declared_revision_files(changed)
        self.assertEqual(declared, ("SKILL.md", "scripts/verify_candidates.py"))
        self.assertNotEqual(tuple(sorted(declared)), tuple(sorted(entry.VERIFIER_REVISION_FILES)))
        changed.write_text("x = 1\n")
        with self.assertRaises(ValueError):
            entry.declared_revision_files(changed)

    def test_policy_and_argv_under_the_supervisor(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            seen.update(policy=policy_for(4242), command=command_for(Path("/p.json")),
                        environment=environment, options=options, input_dir=input_dir)
            return True
        out, analysis = self.root / "out", self.inputs("in", {"analysis.json": {"patterns": []}})
        out.mkdir()
        runtime = ["/opt/node", "/opt/oss-research/srt-0.0.77/node_modules/x/cli.js"]
        reads = self.discover(patterns=["PAT-001", "PAT-002"], max_candidates_per_repo=3,
                              max_candidates_total=4, max_clues_per_pattern=2, request_budget=40)
        with mock.patch.object(worker, "run_supervised", fake_supervised), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HTTP_PROXY": "http://10.0.0.1:1",
                                             "GH_TOKEN": "gho_parentsecret"}):
            entry.run_reads(worker, collect_entry, runtime, out, analysis, reads, {}, 120.0, launch=None,
                            upstream="loopback-mock")
        network = seen["policy"]["network"]
        self.assertEqual(network["allowedDomains"], ["127.0.0.1:4242"])
        self.assertIs(network["allowLocalBinding"], False)
        read = seen["policy"]["filesystem"]["allowRead"]
        for path in (entry.verifier_file(worker), entry.adapter_file(worker)):
            self.assertIn(str(path), read)
        for relative in entry.VERIFIER_REVISION_FILES:
            self.assertIn(str(entry.verifier_file(worker).parents[1] / relative), read)
        self.assertEqual(seen["input_dir"], analysis)
        self.assertIn(str(analysis), read)
        self.assertFalse(any("claude" in p.lower() or "credential" in p.lower() for p in read), read)
        command = seen["command"]
        self.assertEqual(command[:4], [*runtime, "--settings", "/p.json"])
        self.assertEqual(Path(command[command.index("-B") + 1]).name, "executable_oss-research-relay-verify")
        tail = command[command.index("discover"):]
        self.assertEqual(tail[0], "discover")
        self.assertIn("--require-proxy", tail)
        for option, value in (("--relay-url", "http://127.0.0.1:4242"), ("--repo", TARGET),
                              ("--max-candidates-per-repo", "3"), ("--max-candidates-total", "4"),
                              ("--max-clues-per-pattern", "2"), ("--request-budget", "40"),
                              ("--analysis", str(analysis / "analysis.json")),
                              ("--deadline-seconds", "90")):
            self.assertEqual(tail[tail.index(option) + 1], value, option)
        self.assertEqual([tail[i + 1] for i, a in enumerate(tail) if a == "--pattern"], ["PAT-001", "PAT-002"])
        for banned in ("--allow-clone", "--clone-root", "--fixture-dir", "--api-version", "--markdown-output"):
            self.assertNotIn(banned, tail)
        self.assertFalse({k for k in seen["environment"] if "proxy" in k.lower() or "TOKEN" in k})
        self.assertIn("record", seen["options"])

    def test_the_locus_search_argv_and_the_clue_options(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            policy_for(4242)
            seen["command"] = command_for(Path("/p.json"))
            return True
        out = self.root / "out"
        out.mkdir()
        directory = self.inputs("in", {"discovery.json": {}, "assessment.json": {}})
        reads = self.locus(clues=["validation boundary", "x"], max_clues_per_locus=2)
        with mock.patch.object(worker, "run_supervised", fake_supervised):
            entry.run_reads(worker, collect_entry, ["/n", "/c"], out, directory, reads, {}, 60.0, None)
        command = seen["command"]
        tail = command[command.index("locus-search"):]
        self.assertEqual(tail[tail.index("--discovery") + 1], str(directory / "discovery.json"))
        self.assertEqual(tail[tail.index("--assessment") + 1], str(directory / "assessment.json"))
        self.assertEqual(tail[tail.index("--max-clues-per-locus") + 1], "2")
        self.assertEqual([tail[i + 1] for i, a in enumerate(tail) if a == "--clue"], ["validation boundary", "x"])
        self.assertNotIn("--pattern", tail)
        self.assertEqual(tail[tail.index("--deadline-seconds") + 1], "30")

    def test_no_relay_port_starts_no_child(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            seen["command"] = command_for(Path("/p.json"))
            return False
        out = self.root / "out"
        out.mkdir()
        directory = self.inputs("in", {"analysis.json": {}})
        with mock.patch.object(worker, "run_supervised", fake_supervised):
            entry.run_reads(worker, collect_entry, ["/n", "/c"], out, directory, self.discover(), {}, 60.0, None)
        self.assertEqual(seen["command"], ["/nonexistent/no-relay-port"])

    def test_a_dropped_read_is_dropped_from_the_policy(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            seen["read"] = policy_for(4242)["filesystem"]["allowRead"]
            return True
        out = self.root / "out"
        out.mkdir()
        directory = self.inputs("in", {"analysis.json": {}})
        dropped = str(entry.verifier_file(worker).parents[1] / entry.VERIFIER_REVISION_FILES[-1])
        with mock.patch.object(worker, "run_supervised", fake_supervised):
            entry.run_reads(worker, collect_entry, ["/n", "/c"], out, directory, self.discover(), {}, 60.0, None,
                            drop_read=(dropped,))
        self.assertNotIn(dropped, seen["read"])
        self.assertIn(str(entry.verifier_file(worker)), seen["read"])

    def fake_run(self, stage, name, outputs=None):
        run_dir = self.root / name / "run"
        run_dir.parent.mkdir()
        worker.prepare_run(run_dir, {"filesystem": {}, "network": {}})
        for item in outputs if outputs is not None else entry.OUTPUT_FILES[stage]:
            (run_dir / "output" / item).write_text("{}")
        return run_dir

    def recorded(self, run_dir, reads, exit_code=0, failures=()):
        record = entry.recorder(worker, collect_entry, reads, {"analysis.json": {"bytes": 2, "sha256": "a" * 64}},
                                "loopback-mock", "oss-research-0123456789abcdef")
        relay = {"sources": [], "relay_sha256": "e" * 64, "port": 4242, "stderr": "relay log"}
        kept = record(run_dir, exit_code, "", "adapter stderr", list(failures), relay)
        manifest = json.loads((run_dir / "result/manifest.json").read_text())
        envelope = json.loads((run_dir / "result/read-envelope.json").read_text())
        return kept, manifest, envelope

    def test_record_keeps_the_verifiers_status_and_never_raises_it(self):
        for stage, reads in (("discover", self.discover()), ("locus-search", self.locus())):
            with self.subTest(stage):
                kept, manifest, envelope = self.recorded(self.fake_run(stage, stage + "-ok"), reads)
                self.assertTrue(kept)
                self.assertEqual((manifest["kind"], manifest["stage"], manifest["status"]),
                                 ("candidate-reads", stage, "succeeded"))
                self.assertEqual(envelope["schema"], entry.ENVELOPE_SCHEMA)
                self.assertEqual(set(envelope["files"]), {*entry.OUTPUT_FILES[stage], "relay.log", "run-manifest.json"})
                self.assertEqual(envelope["request"], reads.as_request())
                self.assertEqual(envelope["upstream"], "loopback-mock")
                kept, manifest, envelope = self.recorded(self.fake_run(stage, stage + "-partial"), reads, exit_code=3)
                self.assertTrue(kept)
                self.assertEqual((manifest["status"], manifest["problems"], envelope["adapter_exit"]),
                                 ("partial", [], 3))
                for label, run_dir, arguments in (
                        ("verifier failed", self.fake_run(stage, stage + "-a"), dict(exit_code=4)),
                        ("bad input", self.fake_run(stage, stage + "-b"), dict(exit_code=2)),
                        ("stopped", self.fake_run(stage, stage + "-c"), dict(exit_code=None, failures=["timeout"])),
                        ("partial with a supervisor failure", self.fake_run(stage, stage + "-d"),
                         dict(exit_code=3, failures=["timeout"])),
                        ("missing output", self.fake_run(stage, stage + "-e", entry.OUTPUT_FILES[stage][1:]), {}),
                        ("partial with a missing output", self.fake_run(stage, stage + "-f", entry.OUTPUT_FILES[stage][:1]),
                         dict(exit_code=3))):
                    with self.subTest(stage + " " + label):
                        kept, manifest, _ = self.recorded(run_dir, reads, **arguments)
                        self.assertFalse(kept)
                        self.assertEqual(manifest["status"], "failed")
                        self.assertTrue(manifest["problems"])

    def test_outputs_are_read_only_as_bounded_regular_files(self):
        secret = self.root / "outside.txt"
        secret.write_text("not the child's")
        cases = {"symlink": lambda path: path.symlink_to(secret),
                 "fifo": lambda path: os.mkfifo(path),
                 "directory": lambda path: path.mkdir(),
                 "oversize (sparse)": lambda path: sparse(path, collect_entry.MAX_OUTPUT_BYTES + 1)}
        for label, make in cases.items():
            with self.subTest(label):
                run_dir = self.fake_run("discover", label.split()[0], [n for n in entry.OUTPUT_FILES["discover"]
                                                                       if n != "discovery.json"])
                make(run_dir / "output/discovery.json")
                began = time.monotonic()
                kept, manifest, envelope = self.recorded(run_dir, self.discover())
                self.assertLess(time.monotonic() - began, 5)
                self.assertFalse(kept)
                self.assertTrue(any(p.startswith("output discovery.json:") for p in manifest["problems"]))
                self.assertNotIn("discovery.json", envelope["files"])

    def test_inputs_are_checked_against_the_request(self):
        analysis = {"patterns": [{"pattern_id": "PAT-001", "superseded_by": None},
                                 {"pattern_id": "PAT-002", "superseded_by": "PAT-001"}]}
        good = self.inputs("good", {"analysis.json": analysis})
        described = entry.checked_inputs(self.discover(), good)
        self.assertEqual(set(described), {"analysis.json"})
        self.assertEqual(described["analysis.json"]["sha256"], sha256(good / "analysis.json"))
        for label, directory, reads in (
                ("pattern not in the analysis", good, self.discover(patterns=["PAT-009"])),
                ("superseded pattern", good, self.discover(patterns=["PAT-002"])),
                ("extra file", self.inputs("extra", {"analysis.json": analysis, "note.txt": "x"}), self.discover()),
                ("missing file", self.inputs("missing", {}), self.discover()),
                ("wrong stage files", good, self.locus()),
                ("not JSON", self.inputs("junk", {"analysis.json": "not json"}), self.discover()),
                ("a list", self.inputs("list", {"analysis.json": "[]"}), self.discover()),
                ("duplicate key", self.inputs("dup", {"analysis.json": '{"patterns": [], "patterns": []}'}),
                 self.discover()),
                ("NaN", self.inputs("nan", {"analysis.json": '{"patterns": [], "x": NaN}'}), self.discover()),
                ("empty", self.inputs("empty", {"analysis.json": ""}), self.discover())):
            with self.subTest(label), self.assertRaises(ValueError):
                entry.checked_inputs(reads, directory)
        link = self.inputs("link", {})
        (self.root / "real.json").write_text("{}")
        (link / "analysis.json").symlink_to(self.root / "real.json")
        writable = self.inputs("writable", {"analysis.json": analysis})
        (writable / "analysis.json").chmod(0o666)
        open_dir = self.inputs("opendir", {"analysis.json": analysis})
        open_dir.chmod(0o777)
        big = self.inputs("big", {"analysis.json": ""})
        os.truncate(big / "analysis.json", entry.MAX_INPUT_BYTES + 1)
        fifo = self.inputs("fifo", {})
        os.mkfifo(fifo / "analysis.json")
        for label, directory in (("symlink", link), ("writable file", writable), ("writable directory", open_dir),
                                 ("oversize", big), ("fifo", fifo), ("directory link", None)):
            with self.subTest(label), self.assertRaises(ValueError):
                if directory is None:
                    (self.root / "alias").symlink_to(good)
                    directory = self.root / "alias"
                entry.checked_inputs(self.discover(), directory)

    def test_locus_search_inputs_must_be_for_this_repository_and_bound(self):
        discovery = {"inputs": {"repositories": [TARGET]}, "records": [{"repository": TARGET, "pattern_id": "PAT-001"}]}
        digest = hashlib.sha256(json.dumps(discovery).encode()).hexdigest()
        assessment = {"schema_version": "1.0.0", "discovery_sha256": digest, "assessments": []}
        good = self.inputs("good", {"discovery.json": discovery, "assessment.json": assessment})
        self.assertEqual(set(entry.checked_inputs(self.locus(), good)), {"discovery.json", "assessment.json"})
        other = dict(discovery, records=[{"repository": "octo/other", "pattern_id": "PAT-001"}])
        for label, files, reads in (
                ("another repository in a record", {"discovery.json": other, "assessment.json": assessment}, self.locus()),
                ("another requested repository", {"discovery.json": discovery, "assessment.json": assessment},
                 self.locus(repository="octo/other")),
                ("two repositories", {"discovery.json": dict(discovery, inputs={"repositories": [TARGET, "o/x"]}),
                                      "assessment.json": assessment}, self.locus()),
                ("assessment of another discovery", {"discovery.json": discovery,
                                                     "assessment.json": dict(assessment, discovery_sha256="0" * 64)},
                 self.locus()),
                ("no inputs", {"discovery.json": {"records": []}, "assessment.json": assessment}, self.locus())):
            with self.subTest(label), self.assertRaises(ValueError):
                entry.checked_inputs(reads, self.inputs(re.sub(r"\W", "", label), files))

    def test_main_refuses_bad_input_before_the_relay_and_runs_only_in_the_unit(self):
        real = entry.load_collect_entry()
        local = real.load_worker()
        launches = []
        local.relay_launch = lambda *args, **kwargs: launches.append(args)
        real.load_worker = lambda *args, **kwargs: local
        out = self.root / "out"
        out.mkdir()
        directory = self.inputs("in", {"analysis.json": {"patterns": [{"pattern_id": "PAT-001", "superseded_by": None}]}})
        base = ["--out-dir", str(out), "--input-dir", str(directory), "--stage", "discover", "--repo", TARGET,
                "--pattern", "PAT-001"]
        with mock.patch.object(entry, "load_collect_entry", lambda *args: real), mock.patch.object(sys, "stderr"):
            for label, arguments in (
                    ("budget 61", base + ["--request-budget", "61"]),
                    ("candidates 6", base + ["--max-candidates-total", "6"]),
                    ("timeout 1801", base + ["--timeout", "1801"]),
                    ("no pattern", [a for a in base if a not in ("--pattern", "PAT-001")]),
                    ("pattern not in analysis", base[:-1] + ["PAT-009"]),
                    ("clue on discover", base + ["--clue", "x"]),
                    ("clone-like repo", base[:7] + [TARGET + " --allow-clone"] + base[8:]),
                    ("unknown stage", base[:5] + ["record"] + base[6:]),
                    ("locus cap on discover", base + ["--max-clues-per-locus", "1"])):
                with self.subTest(label):
                    self.assertEqual(entry.main(arguments), 2)
            with self.assertRaises(SystemExit) as refused:
                entry.main(base + ["--allow-clone"])  # argparse: no such option
            self.assertEqual(refused.exception.code, 2)
            with self.assertRaises(SystemExit):
                entry.main(base + ["--upstream", "http://127.0.0.1:1"])
            (out / "x").write_text("")
            self.assertEqual(entry.main(base), 2)
            (out / "x").unlink()
            (directory / "extra.json").write_text("{}")
            self.assertEqual(entry.main(base), 2)
            (directory / "extra.json").unlink()
            environment = {k: v for k, v in os.environ.items() if k != worker.UNIT_ENV_MARKER}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(entry.main(base), 3)
        self.assertEqual(launches, [])
        self.assertEqual(list(out.iterdir()), [])


class OfflineHarness(relay_verify_tests.Harness):
    """entry argv -> the relay-verify adapter (real relay, loopback mock) -> entry record.

    No srt here, so the adapter connects directly and its transport says "direct": that
    one line is what the pickup refuses until a test turns it into srt-proxy. The srt-proxy
    leg is the Linux guest's."""

    def setUp(self):
        super().setUp()
        self.input_root = self.root / "inputs"
        self.input_root.mkdir()
        add_locus_routes(self.upstream.routes)

    def make_input(self, name, files):
        directory = self.input_root / name
        directory.mkdir()
        for filename, source in files.items():
            shutil.copy(source, directory / filename)
            (directory / filename).chmod(0o644)
        directory.chmod(0o755)
        return directory

    def step(self, name, reads, input_dir, expect_exit, srt_proxy=False):
        """Run one step; returns (run_dir, recorded?, manifest)."""
        inputs = entry.checked_inputs(reads, input_dir)
        run_dir = self.root.resolve() / name / "run"  # the record refuses an aliased (/var) path
        run_dir.parent.mkdir()
        worker.prepare_run(run_dir, {"filesystem": {}, "network": {}})
        argv = entry.adapter_arguments(worker, reads, self.port, run_dir, input_dir.resolve(), 120.0)
        self.assertIn("--require-proxy", argv)
        refused = relay_verify_tests.adapter.main(argv, environ={}, clock=lambda: relay_verify_tests.FIXED_TIME,
                                                  sleep=lambda seconds: None)
        self.assertEqual(refused, 2)  # --require-proxy without srt's proxy: stops before the network
        self.assertEqual(sorted(p.name for p in (run_dir / "output").iterdir()), [])
        direct = [a for a in argv if a != "--require-proxy"]
        code = relay_verify_tests.adapter.main(direct, environ={}, clock=lambda: relay_verify_tests.FIXED_TIME,
                                               sleep=lambda seconds: None)
        self.assertEqual(code, expect_exit)
        _, skills = worker.trusted_relay(Path(worker.__file__))
        relay = {"sources": worker.relay_sources(skills), "relay_sha256": "e" * 64, "port": self.port, "stderr": ""}
        record = entry.recorder(worker, collect_entry, reads, inputs, "loopback-mock", "oss-research-0123456789abcdef")
        kept = record(run_dir, code, "", "", [], relay)
        return run_dir, kept, json.loads((run_dir / "result/manifest.json").read_text())

    def members(self, stage, run_dir, srt_proxy=True):
        """The tar's members from a run directory; the transport record can be made to say srt-proxy."""
        members = {}
        for member, path in ((entry.OUTPUT_FILES[stage][0], "output/" + entry.OUTPUT_FILES[stage][0]),
                             ("verification-manifest.json", "output/verification-manifest.json"),
                             ("transport.json", "output/transport.json"), ("relay.log", "result/relay.log"),
                             ("run-manifest.json", "result/manifest.json"),
                             ("read-envelope.json", "result/read-envelope.json")):
            members[member] = (run_dir / path).read_bytes()
        if srt_proxy:
            transport = json.loads(members["transport.json"])
            transport["transport"] = "srt-proxy"
            members["transport.json"] = json.dumps(transport, indent=1).encode()
            members = sealed(members)
        return members


def sealed(members):
    """The envelope's file list recomputed from the members (so an edit meets its own binding)."""
    envelope = json.loads(members["read-envelope.json"])
    envelope["files"] = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                         for name, data in members.items() if name != "read-envelope.json"}
    return dict(members, **{"read-envelope.json": json.dumps(envelope, indent=1).encode()})


def edited(members, name, change, seal=True):
    document = json.loads(members[name])
    change(document)
    result = dict(members, **{name: json.dumps(document, indent=1).encode()})
    return sealed(result) if seal else result


class PickupCase(OfflineHarness):
    def pickup(self, stage, members, *, upstream="loopback-mock", status="partial", unit="oss-research-0123456789abcdef",
               wrapper_exit=None, inner=None, verifier_sha=HOST_VERIFIER_SHA, issues_sha=HOST_ISSUES_SHA,
               sibling_sha=HOST_SIBLING_SHA, name=None):
        self.counter = getattr(self, "counter", 0) + 1
        name = "pickup%d" % self.counter if name is None else name + str(self.counter)
        archive = self.root / (name + ".tar")
        with tarfile.open(archive, "w") as bundle:
            for member, data in members.items():
                path = self.root / ("m-" + name + "-" + member)
                path.write_bytes(data)
                bundle.add(path, arcname=member)
        exit_code = {"succeeded": 0, "partial": 5}.get(status, 4) if wrapper_exit is None else wrapper_exit
        record = self.root / (name + "-wrapper.json")
        record.write_text(json.dumps({
            "unit": unit, "exit_code": exit_code, "failures": [],
            "stdout": json.dumps(inner if inner is not None else
                                 {"mode": "candidate-reads", "stage": stage, "status": status}) + "\n"}) + "\n")
        dest = self.root / (name + "-out")
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 24), str(dest), "--candidate-reads",
             "--stage", stage, "--verifier-sha256", verifier_sha, "--relay-issues-sha256", issues_sha,
             "--relay-sibling-sha256", sibling_sha, "--wrapper-record", str(record), "--upstream", upstream],
            capture_output=True, text=True, timeout=120)
        try:
            return completed.returncode, json.loads(completed.stdout)
        except ValueError:
            self.fail(completed.stdout + completed.stderr)

    def discover_run(self, name="discover", **changes):
        values = dict(stage="discover", repository=TARGET, patterns=["PAT-001"], clues=[], max_candidates_per_repo=5,
                      max_candidates_total=5, max_clues_per_pattern=3, max_clues_per_locus=None, request_budget=60)
        values.update(changes)
        reads = entry.Reads(**values)
        directory = self.make_input(name, {"analysis.json": relay_verify_tests.ANALYSIS})
        return reads, directory

    def problems_of(self, seen):
        return seen["problems"]

    def expect(self, stage, members, text, **options):
        code, seen = self.pickup(stage, members, **options)
        self.assertEqual(code, 1, seen)
        self.assertTrue(any(text in problem for problem in seen["problems"]), (text, seen["problems"]))
        return seen


class OfflineDiscoverTests(PickupCase):
    def test_the_real_offline_run_binds_except_the_missing_srt_proxy(self):
        reads, directory = self.discover_run()
        run_dir, kept, manifest = self.step("run", reads, directory, 3)
        self.assertEqual((kept, manifest["status"], manifest["problems"]), (True, "partial", []))
        code, seen = self.pickup("discover", self.members("discover", run_dir, srt_proxy=False))
        self.assertEqual(code, 1)
        self.assertIn("transport record does not say srt-proxy", seen["problems"], seen)

    def test_a_bound_partial_discover_is_only_partial_and_a_mock_is_never_live(self):
        run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
        members = self.members("discover", run_dir)
        code, seen = self.pickup("discover", members)
        self.assertEqual((code, seen["problems"]), (3, []), seen)
        self.assertEqual((seen["contract"], seen["stage"], seen["step_status"], seen["upstream"]),
                         ("candidate-reads", "discover", "partial", "loopback-mock"))
        # The anonymous code search answers 401 per clue; the mock has none of the policy files (404).
        self.assertEqual(seen["upstream_errors"], {"401": 3, "404": 18}, seen)
        self.assertGreater(seen["requests"], 5)
        self.expect("discover", members, "envelope upstream is not the one the host asked for",
                    upstream="github-rest-via-relay")

    def test_every_binding_fails_closed(self):
        run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
        good = self.members("discover", run_dir)
        env_and_run = lambda change: (lambda m: edited(edited(m, "read-envelope.json", change), "run-manifest.json", change))
        request = lambda key, value: (lambda d: d["request"].__setitem__(key, value))
        cases = {
            # members
            "a member changed after the envelope": (lambda m: dict(m, **{"relay.log": b"edited"}), {}, "does not match the envelope"),
            "an input digest malformed": (lambda m: edited(m, "read-envelope.json",
                                                           lambda d: d["inputs"]["analysis.json"].__setitem__("sha256", "x")),
                                          {}, "has no size and sha256"),
            "no analysis input listed": (lambda m: edited(m, "read-envelope.json", lambda d: d["inputs"].clear()),
                                         {}, "does not list exactly the stage's inputs"),
            "another entry": (lambda m: edited(m, "read-envelope.json", lambda d: d.__setitem__("entry", "x")),
                              {}, "is not the candidate reads entry's"),
            "another stage in the envelope": (lambda m: edited(m, "read-envelope.json", lambda d: d.__setitem__("stage", "locus-search")),
                                              {}, "is not the candidate reads entry's"),
            # the host's reviewed files
            "the host reviewed another verifier": (lambda m: m, dict(verifier_sha="0" * 64),
                                                   "the host tree's verifier is not the one the host reviewed"),
            "the verifier that ran is another file": (lambda m: edited(m, "transport.json", lambda d: d["verifier"].__setitem__("sha256", "1" * 64)),
                                                      {}, "transport verifier is not the one the host reviewed"),
            "the verifier path is another script": (lambda m: edited(m, "transport.json", lambda d: d["verifier"].__setitem__("path", "/x/other.py")),
                                                    {}, "transport verifier is not the one the host reviewed"),
            "the host reviewed another issues collector": (lambda m: m, dict(issues_sha="2" * 64),
                                                           "relay ready sources are not exactly"),
            "the host reviewed another sibling": (lambda m: m, dict(sibling_sha="3" * 64),
                                                  "relay ready sources are not exactly"),
            "relay source of another sha": (env_and_run(lambda d: d["relay"]["sources"][1].__setitem__("sha256", "4" * 64)),
                                            {}, "relay ready sources are not exactly"),
            "relay source role added": (env_and_run(lambda d: d["relay"]["sources"].append({"role": "verifier", "path": "/x", "sha256": "5" * 64})),
                                        {}, "relay ready sources are not exactly"),
            "output revision of another verifier": (lambda m: edited(edited(m, "discovery.json", lambda d: d["generated_by"].__setitem__("revision", "sha256:" + "6" * 64)),
                                                                     "verification-manifest.json", lambda d: d["generated_by"].__setitem__("revision", "sha256:" + "6" * 64)),
                                                    {}, "output revision is not the host verifier's"),
            # transport
            "not srt-proxy": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("transport", "direct")), {},
                              "does not say srt-proxy"),
            "a credential was used": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("credential", "Bearer x")), {},
                                      "no credential was used"),
            "another stage in the transport": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("stage", "locus-search")), {},
                                               "transport record is not this stage's"),
            "transport exit differs": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("exit_code", 0)), {},
                                       "transport exit code is not the adapter's"),
            "transport relay is another port": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("relay_base_url", "http://127.0.0.1:1")),
                                                {}, "transport relay URL is not the envelope's relay"),
            "a refused request": (lambda m: edited(m, "transport.json", lambda d: d["requests"][0].__setitem__("outcome", "relay-refused")),
                                  {}, "was not delivered"),
            "a failed transport": (lambda m: edited(m, "transport.json", lambda d: d["requests"][0].update({"outcome": "transport-error"})),
                                   {}, "was not delivered"),
            "an upstream error without a status": (lambda m: edited(m, "transport.json", lambda d: d["requests"][0].update({"outcome": "upstream-error", "upstream_status": 200})),
                                                   {}, "was not delivered"),
            "no requests": (lambda m: edited(m, "transport.json", lambda d: d.__setitem__("requests", [])), {}, "was not delivered"),
            "a request dropped from the transport": (lambda m: edited(m, "transport.json", lambda d: d["requests"].pop()), {},
                                                     "consumed requests differ"),
            # the request and its limits
            "budget over the entry limit": (env_and_run(request("request_budget", 61)), {}, "request budget is outside"),
            "budget a string": (env_and_run(request("request_budget", "60")), {}, "request budget is outside"),
            "repository with an option": (env_and_run(request("repository", "octo/demo --allow-clone")), {}, "has no OWNER/NAME"),
            "relay allows another repository": (env_and_run(request("relay_repositories", [TARGET, "octo/.github", "evil/x"])), {},
                                                "relay repositories are not the repository"),
            "six patterns": (env_and_run(request("patterns", [f"PAT-00{n}" for n in range(1, 7)])), {}, "outside the entry's limits"),
            "a clue on discover": (env_and_run(request("clues", ["x"])), {}, "outside the entry's limits"),
            "candidate cap 6": (env_and_run(request("max_candidates_total", 6)), {}, "outside the entry's limits"),
            "request fields added": (env_and_run(request("allow_clone", True)), {}, "does not hold exactly the request fields"),
            "run request differs from the envelope": (lambda m: edited(m, "run-manifest.json", request("request_budget", 59)), {},
                                                      "run manifest request or inputs differ"),
            # the verifier's own records
            "the budget the verifier ran with": (lambda m: edited(m, "verification-manifest.json", lambda d: d["runs"][0]["budget"].__setitem__("request_limit", 100)),
                                                 {}, "verification budget is not the request's"),
            "consumed past the budget": (lambda m: edited(m, "verification-manifest.json", lambda d: d["runs"][0]["budget"].__setitem__("requests_consumed", 10**6)),
                                         {}, "consumed requests differ"),
            "two runs": (lambda m: edited(m, "verification-manifest.json", lambda d: d["runs"].append(d["runs"][0])), {},
                         "does not have exactly one run"),
            "another command": (lambda m: edited(m, "verification-manifest.json", lambda d: d["runs"][0].__setitem__("command", "locus-search")),
                                {}, "is not this stage's or its status differs"),
            "a clone was allowed": (lambda m: edited(m, "discovery.json", lambda d: d["inputs"].__setitem__("allow_clone", True)),
                                    {}, "output inputs differ from the request"),
            "another repository requested": (lambda m: edited(m, "discovery.json", lambda d: d["inputs"].__setitem__("repositories", ["octo/other"])),
                                             {}, "output inputs differ from the request"),
            "another analysis": (lambda m: edited(m, "discovery.json", lambda d: d["inputs"].__setitem__("analysis_sha256", "7" * 64)),
                                 {}, "output analysis is not the envelope's input"),
            "a record of another repository": (lambda m: edited(m, "discovery.json", lambda d: d["records"][0].__setitem__("repository", "octo/other")),
                                               {}, "an output record is for another repository or pattern"),
            "a record of an unrequested pattern": (lambda m: edited(m, "discovery.json", lambda d: d["records"][0].__setitem__("pattern_id", "PAT-009")),
                                                   {}, "an output record is for another repository or pattern"),
            "more candidates than the cap": (lambda m: edited(m, "discovery.json", lambda d: d.__setitem__("records", d["records"] * 6)),
                                             {}, "more candidates than the caps"),
            "output status differs from the run": (lambda m: edited(m, "discovery.json", lambda d: d.__setitem__("status", "complete")),
                                                   {}, "output document is not this status's"),
            "an output of the wrong type": (lambda m: edited(m, "discovery.json", lambda d: d.__setitem__("records", "x")),
                                            {}, "has no record list"),
            # the run
            "a succeeded run claiming a partial exit": (lambda m: edited(m, "run-manifest.json", lambda d: d.__setitem__("status", "succeeded")),
                                                        {}, "adapter exit is not the run status's"),
            "a failed run": (lambda m: edited(m, "run-manifest.json", lambda d: d.__setitem__("status", "failed")), {},
                             "is not a succeeded or partial candidate read"),
            "failures in the run": (lambda m: edited(m, "run-manifest.json", lambda d: d.__setitem__("failures", ["timeout"])), {},
                                    "has failures or problems"),
            "the other run kind": (lambda m: edited(m, "run-manifest.json", lambda d: d.__setitem__("kind", "issue-collection")), {},
                                   "is not a succeeded or partial candidate read"),
            # the wrapper
            "wrapper exit": (lambda m: m, dict(wrapper_exit=0), "the unit wrapper's exit is not the run status's"),
            "wrapper unit": (lambda m: m, dict(unit="oss-research-aaaaaaaaaaaaaaaa"), "wrapper unit is not the envelope's unit"),
            "wrapper inner mode": (lambda m: m, dict(inner={"mode": "collect-issues", "stage": "discover", "status": "partial"}),
                                   "wrapper inner summary is not this candidate read"),
            "wrapper inner stage": (lambda m: m, dict(inner={"mode": "candidate-reads", "stage": "locus-search", "status": "partial"}),
                                    "wrapper inner summary is not this candidate read"),
        }
        for label, (change, options, text) in cases.items():
            with self.subTest(label):
                self.expect("discover", change(good), text, **options)

    def test_a_value_of_the_wrong_json_type_is_a_failed_pickup_not_a_traceback(self):
        run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
        good = self.members("discover", run_dir)
        for label, change in (("requests a string", lambda m: edited(m, "transport.json", lambda d: d.__setitem__("requests", "x"))),
                              ("sources a string", lambda m: edited(edited(m, "read-envelope.json", lambda d: d["relay"].__setitem__("sources", "x")),
                                                                    "run-manifest.json", lambda d: d["relay"].__setitem__("sources", "x"))),
                              ("request a list", lambda m: edited(m, "read-envelope.json", lambda d: d.__setitem__("request", []))),
                              ("runs a dict", lambda m: edited(m, "verification-manifest.json", lambda d: d.__setitem__("runs", {}))),
                              ("records of dicts", lambda m: edited(m, "discovery.json", lambda d: d.__setitem__("records", [1, {}])))):
            with self.subTest(label):
                code, seen = self.pickup("discover", change(good))
                self.assertEqual(code, 1, seen)
                self.assertTrue(seen["problems"])

    def test_the_artifact_set_is_fixed_and_the_command_line_is_exact(self):
        run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
        good = self.members("discover", run_dir)
        extra = dict(good, **{"corpus.json": b"{}"})
        self.expect("discover", extra, "unexpected entry", name="extra")
        self.expect("discover", {k: v for k, v in good.items() if k != "relay.log"}, "missing: relay.log", name="missing")
        self.expect("discover", {k: v for k, v in good.items() if k != "discovery.json"} | {"locus-search.json": b"{}"},
                    "missing: discovery.json", name="other-stage")
        # A duplicate member and a link are refused before anything is unpacked.
        archive = self.root / "dup.tar"
        with tarfile.open(archive, "w") as bundle:
            for member, data in good.items():
                path = self.root / ("d-" + member)
                path.write_bytes(data)
                bundle.add(path, arcname=member)
            bundle.add(self.root / "d-relay.log", arcname="relay.log")
        completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 24),
                                    str(self.root / "dup-out"), "--candidate-reads", "--stage", "discover",
                                    "--verifier-sha256", HOST_VERIFIER_SHA, "--relay-issues-sha256", HOST_ISSUES_SHA,
                                    "--relay-sibling-sha256", HOST_SIBLING_SHA, "--wrapper-record", str(self.root / "x"),
                                    "--upstream", "loopback-mock"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("duplicate entry: relay.log", json.loads(completed.stdout)["problems"])
        link = self.root / "link.tar"
        with tarfile.open(link, "w") as bundle:
            for member, data in good.items():
                if member != "relay.log":
                    path = self.root / ("l-" + member)
                    path.write_bytes(data)
                    bundle.add(path, arcname=member)
            info = tarfile.TarInfo("relay.log")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            bundle.addfile(info)
        completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(link), str(1 << 24),
                                    str(self.root / "link-out"), "--candidate-reads", "--stage", "discover",
                                    "--verifier-sha256", HOST_VERIFIER_SHA, "--relay-issues-sha256", HOST_ISSUES_SHA,
                                    "--relay-sibling-sha256", HOST_SIBLING_SHA, "--wrapper-record", str(self.root / "x"),
                                    "--upstream", "loopback-mock"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        for arguments in ([], ["--candidate-reads"], ["--candidate-reads", "--stage", "record"] + ["x"] * 10,
                          ["--candidate-reads", "--stage", "discover", "--verifier-sha256", "zz", "--relay-issues-sha256",
                           HOST_ISSUES_SHA, "--relay-sibling-sha256", HOST_SIBLING_SHA, "--wrapper-record", "w",
                           "--upstream", "loopback-mock"],
                          ["--candidate-reads", "--stage", "discover", "--verifier-sha256", HOST_VERIFIER_SHA,
                           "--relay-issues-sha256", HOST_ISSUES_SHA, "--relay-sibling-sha256", HOST_SIBLING_SHA,
                           "--wrapper-record", "w", "--upstream", "somewhere"]):
            with self.subTest(arguments):
                completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(link), str(1 << 24),
                                            str(self.root / "arg-out")] + arguments, capture_output=True, text=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("Traceback", completed.stderr)
        self.assertFalse((self.root / "arg-out").exists())


class OfflineCompleteDiscoverTests(PickupCase):
    routes_options = {"code_status": 200}

    def test_a_complete_discover_binds_and_exits_zero(self):
        run_dir, kept, manifest = self.step("run", *self.discover_run(), 0)
        self.assertEqual((kept, manifest["status"]), (True, "succeeded"))
        code, seen = self.pickup("discover", self.members("discover", run_dir), status="succeeded")
        self.assertEqual((code, seen["problems"], seen["step_status"]), (0, [], "complete"), seen)
        self.assertEqual(seen["upstream_errors"], {"404": 18})  # absent policy files are answers, not failures


class LocusCase(PickupCase):
    def locus_inputs(self, discover_dir, name="locus"):
        directory = self.input_root / name
        directory.mkdir()
        shutil.copy(discover_dir / "output/discovery.json", directory / "discovery.json")
        (directory / "assessment.json").write_text(json.dumps(assessment_for(directory / "discovery.json")))
        for path in directory.iterdir():
            path.chmod(0o644)
        directory.chmod(0o755)
        return directory

    def locus_run(self, **changes):
        discover_reads, discover_input = self.discover_run()
        discover_dir, kept, _ = self.step("first", discover_reads, discover_input, 3)
        values = dict(stage="locus-search", repository=TARGET, patterns=[], clues=["validation boundary"],
                      max_candidates_per_repo=None, max_candidates_total=None, max_clues_per_pattern=None,
                      max_clues_per_locus=3, request_budget=60)
        values.update(changes)
        return entry.Reads(**values), self.locus_inputs(discover_dir)



class OfflineLocusSearchTests(LocusCase):
    def test_a_bound_complete_locus_search_and_its_counterexamples(self):
        reads, directory = self.locus_run()
        run_dir, kept, manifest = self.step("second", reads, directory, 0)
        self.assertEqual((kept, manifest["status"]), (True, "succeeded"))
        good = self.members("locus-search", run_dir)
        code, seen = self.pickup("locus-search", good, status="succeeded")
        self.assertEqual((code, seen["problems"], seen["stage"], seen["step_status"]), (0, [], "locus-search", "complete"), seen)
        self.assertTrue(seen["records"], seen)
        self.assertEqual(sorted(seen["files"]), sorted(["locus-search.json", *entry_common()]))
        self.expect("locus-search", good, "envelope upstream is not the one the host asked for",
                    upstream="github-rest-via-relay", status="succeeded", name="live")
        # A discover tar is not a locus-search tar and the other way round.
        self.expect("discover", good, "missing: discovery.json", name="wrong-stage")
        cases = {
            "another discovery": (lambda m: edited(m, "locus-search.json", lambda d: d.__setitem__("discovery_sha256", "8" * 64)),
                                  "output discovery is not the envelope's input"),
            "a search of another repository": (lambda m: edited(m, "locus-search.json", lambda d: d["searches"][0].__setitem__("repository", "octo/other")),
                                               "an output search is for another repository"),
            "more clues than the cap": (lambda m: edited(m, "locus-search.json", lambda d: d["searches"][0].__setitem__("clues", ["a", "b", "c", "d"])),
                                        "uses more clues than the cap"),
            "six clues requested": (lambda m: edited(edited(m, "read-envelope.json", lambda d: d["request"].__setitem__("clues", [f"c{n}" for n in range(6)])),
                                                     "run-manifest.json", lambda d: d["request"].__setitem__("clues", [f"c{n}" for n in range(6)])),
                                    "outside the entry's limits"),
            "a quote in a clue": (lambda m: edited(edited(m, "read-envelope.json", lambda d: d["request"].__setitem__("clues", ['a"b'])),
                                                   "run-manifest.json", lambda d: d["request"].__setitem__("clues", ['a"b'])),
                                  "outside the entry's limits"),
            "a pattern on locus-search": (lambda m: edited(edited(m, "read-envelope.json", lambda d: d["request"].__setitem__("patterns", ["PAT-001"])),
                                                           "run-manifest.json", lambda d: d["request"].__setitem__("patterns", ["PAT-001"])),
                                          "outside the entry's limits"),
            "the discovery input listed twice with another sha": (lambda m: edited(m, "read-envelope.json", lambda d: d["inputs"].pop("assessment.json")),
                                                                  "does not list exactly the stage's inputs"),
        }
        for label, (change, text) in cases.items():
            with self.subTest(label):
                self.expect("locus-search", change(good), text, status="succeeded")


class OfflinePartialLocusSearchTests(LocusCase):
    def test_an_unanswered_search_makes_a_partial_locus_search_that_still_binds(self):
        reads, directory = self.locus_run(clues=["no route for this clue"])
        run_dir, kept, manifest = self.step("second", reads, directory, 3)
        self.assertEqual((kept, manifest["status"]), (True, "partial"))
        code, seen = self.pickup("locus-search", self.members("locus-search", run_dir))
        self.assertEqual((code, seen["problems"], seen["step_status"]), (3, [], "partial"), seen)
        self.assertIn("404", seen["upstream_errors"])


STATIC_DIRS = ("docs/rules",)


class StaticReadsEntryTests(EntryCase):
    """--static-reads rest (#256): the entry's limits, request and child argv."""

    def test_static_reads_are_inside_fixed_limits_and_the_default_request_is_unchanged(self):
        legacy = set(self.discover().as_request())
        self.assertNotIn("static_reads", legacy)
        rest = self.discover(static_reads="rest", static_dirs=STATIC_DIRS)
        self.assertEqual(set(rest.as_request()) - legacy, {"static_reads", "static_dirs"})
        self.assertEqual((rest.as_request()["static_reads"], rest.as_request()["static_dirs"]), ("rest", ["docs/rules"]))
        self.assertEqual(self.discover(static_reads="rest", static_dirs=("a", "b/c", ".github/x"),
                                       request_budget=60).static_dirs, ("a", "b/c", ".github/x"))
        bad = {"unknown mode": dict(static_reads="clone"), "rest without a directory": dict(static_reads="rest"),
               "a directory without rest": dict(static_dirs=STATIC_DIRS),
               "dot-dot": dict(static_reads="rest", static_dirs=("docs/../x",)),
               "absolute": dict(static_reads="rest", static_dirs=("/docs",)),
               "too deep": dict(static_reads="rest", static_dirs=("a/b/c/d",)),
               "four directories": dict(static_reads="rest", static_dirs=("a", "b", "c", "d")),
               "duplicate": dict(static_reads="rest", static_dirs=("a", "a")),
               "encoded": dict(static_reads="rest", static_dirs=("docs%2Frules",)),
               "not a string": dict(static_reads="rest", static_dirs=(1,)),
               "budget below the static reserve": dict(static_reads="rest", static_dirs=STATIC_DIRS, request_budget=57),
               "three directories over the budget": dict(static_reads="rest", static_dirs=("a", "b", "c"),
                                                         request_budget=59)}
        for label, change in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.discover(**change)
        with self.assertRaises(ValueError):
            self.locus(static_reads="rest", static_dirs=STATIC_DIRS)

    def test_the_limits_are_the_verifiers(self):
        verifier = relay_verify_tests.verifier
        self.assertEqual((entry.STATIC_READS_MODES, entry.STATIC_DEFAULT_DIRS, entry.STATIC_MAX_DIRS,
                          entry.STATIC_MAX_DEPTH, entry.STATIC_MAX_FILES, entry.REPOSITORY_FACT_REQUESTS,
                          entry.CONTENT_SEGMENT),
                         (verifier.STATIC_READS_MODES, verifier.STATIC_DEFAULT_DIRS, verifier.STATIC_MAX_DIRS,
                          verifier.STATIC_MAX_DEPTH, verifier.STATIC_MAX_FILES, verifier.REPOSITORY_FACT_REQUESTS,
                          verifier.CONTENT_SEGMENT))
        pickup = load("verify_pickup_for_static", VERIFY)
        self.assertEqual((pickup.CR_STATIC_MAX_DIRS, pickup.CR_STATIC_MAX_DEPTH, pickup.CR_STATIC_MAX_FILES,
                          pickup.CR_STATIC_MAX_FILE_BYTES, pickup.CR_STATIC_EXCERPT_CHARS, pickup.CR_STATIC_MAX_LINKS,
                          pickup.CR_REPOSITORY_FACT_REQUESTS, pickup.CR_CONTENT_SEGMENT, pickup.CR_POLICY_PATHS),
                         (verifier.STATIC_MAX_DIRS, verifier.STATIC_MAX_DEPTH, verifier.STATIC_MAX_FILES,
                          verifier.STATIC_MAX_FILE_BYTES, verifier.STATIC_EXCERPT_CHARS, verifier.STATIC_MAX_LINKS,
                          verifier.REPOSITORY_FACT_REQUESTS, verifier.CONTENT_SEGMENT, verifier.POLICY_PATHS))

    def test_the_child_gets_the_static_options_only_in_rest_mode(self):
        run_dir, input_dir = self.root / "run", self.root / "in"
        legacy = entry.adapter_arguments(worker, self.discover(), 1, run_dir, input_dir, 300.0)
        self.assertFalse([a for a in legacy if a.startswith("--static")])
        rest = entry.adapter_arguments(worker, self.discover(static_reads="rest", static_dirs=("docs/rules", "docs")),
                                       1, run_dir, input_dir, 300.0)
        self.assertEqual(rest[len(legacy):], ["--static-reads", "rest", "--static-dir", "docs/rules",
                                              "--static-dir", "docs"])
        self.assertEqual(rest[:len(legacy)], legacy)

    def test_main_takes_the_default_directory_and_refuses_bad_static_options_before_the_relay(self):
        real = entry.load_collect_entry()
        local = real.load_worker()
        launches, seen = [], []
        local.relay_launch = lambda *args, **kwargs: launches.append(args)
        real.load_worker = lambda *args, **kwargs: local
        out = self.root / "out"
        out.mkdir()
        directory = self.inputs("in", {"analysis.json": {"patterns": [{"pattern_id": "PAT-001", "superseded_by": None}]}})
        base = ["--out-dir", str(out), "--input-dir", str(directory), "--stage", "discover", "--repo", TARGET,
                "--pattern", "PAT-001"]
        original = entry.checked_reads

        def recording(*args, **kwargs):
            reads = original(*args, **kwargs)
            seen.append(reads)
            return reads

        with mock.patch.object(entry, "load_collect_entry", lambda *args: real), mock.patch.object(sys, "stderr"), \
                mock.patch.object(entry, "checked_reads", recording):
            for label, arguments in (("dot-dot", base + ["--static-reads", "rest", "--static-dir", "../x"]),
                                     ("dir without rest", base + ["--static-dir", "docs/rules"]),
                                     ("unknown mode", base + ["--static-reads", "clone"]),
                                     ("small budget", base + ["--static-reads", "rest", "--request-budget", "57"]),
                                     ("on locus-search", base[:5] + ["locus-search"] + base[6:7] + [TARGET]
                                      + ["--static-reads", "rest"])):
                with self.subTest(label):
                    self.assertEqual(entry.main(arguments), 2)
            environment = {k: v for k, v in os.environ.items() if k != worker.UNIT_ENV_MARKER}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(entry.main(base + ["--static-reads", "rest"]), 3)  # outside the unit: Blocked
        self.assertEqual((seen[-1].static_reads, seen[-1].static_dirs), ("rest", ("docs/rules",)))
        self.assertEqual(launches, [])
        self.assertEqual(list(out.iterdir()), [])


class OfflineStaticDiscoverTests(PickupCase):
    """A real offline rest-mode discover (relay, adapter, verifier) and its pickup."""

    def setUp(self):
        super().setUp()
        relay_verify_tests.add_static_routes(self.upstream.routes)

    def static_run(self):
        reads, directory = self.discover_run(static_reads="rest", static_dirs=STATIC_DIRS)
        run_dir, kept, manifest = self.step("run", reads, directory, 3)
        self.assertEqual((kept, manifest["status"], manifest["problems"]), (True, "partial", []))
        return run_dir

    def test_a_bound_rest_discover_and_its_counterexamples(self):
        good = self.members("discover", self.static_run())
        code, seen = self.pickup("discover", good)
        self.assertEqual((code, seen["problems"], seen["step_status"]), (3, [], "partial"), seen)
        discovery = json.loads(good["discovery.json"])
        static = discovery["records"][0]["static_search"]
        self.assertEqual(static["mode"], "rest-contents")
        self.assertTrue(static["hits"])
        both = lambda change: (lambda m: edited(edited(m, "read-envelope.json", change), "run-manifest.json", change))
        request = lambda key, value: (lambda d: d["request"].__setitem__(key, value))
        record = lambda change: (lambda m: edited(m, "discovery.json", lambda d: change(d["records"][0]["static_search"])))
        section = lambda change: (lambda m: edited(m, "discovery.json", lambda d: change(d["static_reads"])))
        cases = {
            "a dot-dot directory requested": (both(request("static_dirs", ["docs/../x"])), "outside the entry's limits"),
            "four directories requested": (both(request("static_dirs", ["a", "b", "c", "d"])), "outside the entry's limits"),
            "another mode requested": (both(request("static_reads", "clone")), "outside the entry's limits"),
            "a budget below the static reserve": (both(request("request_budget", 57)), "outside the entry's limits"),
            "static fields on a locus-like request": (both(lambda d: d["request"].pop("static_dirs")),
                                                      "does not hold exactly the request fields"),
            "output inputs without the static mode": (
                lambda m: edited(m, "discovery.json", lambda d: d["inputs"].pop("static_reads")),
                "output static reads differ from the request"),
            "a record without REST static reads": (record(lambda s: s.__setitem__("mode", "clone")),
                                                   "a record's static reads are not the REST contents reads"),
            "a read outside the directories": (
                record(lambda s: s["files"].append({"path": "src/x.md", "blob_sha": None, "sha256": "0" * 64, "size": 1})),
                "a record's static reads name a path outside the fixed rule"),
            "a hit outside the directories": (record(lambda s: s["hits"][0].__setitem__("path", "docs/rules/x/y.md")),
                                              "a record's static reads name a path outside the fixed rule"),
            "a long excerpt": (record(lambda s: s["hits"][0]["excerpt"].__setitem__("text", "x" * 201)),
                               "a record's static evidence holds more than a one-line excerpt"),
            "a file body kept": (record(lambda s: s["files"][0].__setitem__("content", "Zm9v")),
                                 "a record's static evidence holds more than a one-line excerpt"),
            "another ref": (record(lambda s: s.__setitem__("ref", "f" * 40)),
                            "a record's static reads are not the REST contents reads"),
            "a static section for another repository": (
                section(lambda items: items[0].__setitem__("repository", "octo/other")),
                "output static section differs from the request"),
            "no static section": (section(lambda items: items.clear()), "output static section differs from the request"),
            "a static section that is not a list": (
                lambda m: edited(m, "discovery.json", lambda d: d.__setitem__("static_reads", False)),
                "output static section differs from the request"),
            "two static section entries": (section(lambda items: items.append(copy.deepcopy(items[0]))),
                                           "output static section differs from the request"),
            "a static section at another ref": (
                section(lambda items: items[0]["static_search"].__setitem__("ref", "f" * 40)),
                "the static section's static reads are not the REST contents reads"),
            "an unread static section entry with a file": (
                section(lambda items: items[0]["static_search"].update(ref=None, error="budget-exhausted",
                                                                       available=False)),
                "the static section's static reads are not the REST contents reads"),
            "a record claiming the section's unread reason": (
                record(lambda s: s.update(ref=None, error="budget-exhausted", available=False, files=[], hits=[])),
                "a record's static reads are not the REST contents reads"),
            "a static section apart from the record's": (
                section(lambda items: items[0]["static_search"]["hits"].pop()),
                "output static section differs from the records' static reads"),
            "a static section hit outside the directories": (
                section(lambda items: items[0]["static_search"]["hits"][0].__setitem__("path", "src/x.md")),
                "the static section's static reads name a path outside the fixed rule"),
            "a static section file body kept": (
                section(lambda items: items[0]["static_search"]["files"][0].__setitem__("content", "Zm9v")),
                "the static section's static evidence holds more than a one-line excerpt"),
            "an extra contents request": (
                lambda m: edited(m, "transport.json", lambda d: d["requests"].append(dict(
                    d["requests"][-1], logical_url="https://api.github.com/repos/%s/contents/src/secret.md?ref=%s"
                    % (TARGET, relay_verify_tests.HEAD)))),
                "a contents request is outside the fixed static paths"),
        }
        for label, (change, text) in cases.items():
            with self.subTest(label):
                self.expect("discover", change(good), text)
        # An unread entry (no records then) is the section's other honest shape.
        unread = edited(good, "discovery.json", lambda d: (d["records"].clear(), d["static_reads"][0]["static_search"].update(
            ref=None, error="repository-unavailable", available=False, files=[], hits=[], listings=[], absent=[])))
        code, seen = self.pickup("discover", unread)
        self.assertEqual((code, seen["problems"]), (3, []), seen)

    def test_static_reads_survive_a_budget_that_ends_in_the_searches(self):
        """#266 S3 through the entry and the pickup: at the static floor the searches run out."""
        relay_verify_tests.add_static_routes(self.upstream.routes, docs={
            "r%02d.md" % index: b"# r\n\n## Rule Details\n" for index in range(30)})
        reads, directory = self.discover_run(static_reads="rest", static_dirs=STATIC_DIRS, request_budget=58)
        run_dir, kept, manifest = self.step("run", reads, directory, 3)
        self.assertEqual((kept, manifest["status"], manifest["problems"]), (True, "partial", []))
        members = self.members("discover", run_dir)
        code, seen = self.pickup("discover", members)
        self.assertEqual((code, seen["problems"], seen["step_status"], seen["records"]), (3, [], "partial", 0), seen)
        discovery = json.loads(members["discovery.json"])
        self.assertEqual(discovery["failed_scopes"],
                         [{"repository": TARGET, "pattern_id": "PAT-001", "reason": "budget-exhausted"}])
        [item] = discovery["static_reads"]
        self.assertEqual((item["repository"], len(item["static_search"]["files"])), (TARGET, 31))
        self.assertEqual(len([hit for hit in item["static_search"]["hits"]
                              if hit["rule"] == "options-heading-missing"]), 30)

    def test_a_default_run_with_a_static_section_fails(self):
        run_dir, kept, _ = self.step("run", *self.discover_run(), 3)
        members = edited(self.members("discover", run_dir), "discovery.json",
                         lambda d: d.__setitem__("static_reads", []))
        self.expect("discover", members, "output static section differs from the request")

    def test_a_legacy_request_with_static_output_fails(self):
        good = self.members("discover", self.static_run())
        legacy = lambda d: [d["request"].pop(key) for key in ("static_reads", "static_dirs")]
        members = edited(edited(good, "read-envelope.json", legacy), "run-manifest.json", legacy)
        self.expect("discover", members, "output static reads differ from the request")


def entry_common():
    return ("verification-manifest.json", "transport.json", "relay.log", "run-manifest.json", "read-envelope.json")


if __name__ == "__main__":
    unittest.main()
