"""The non-AI Issue collection entry and its host pickup contract, offline.

    python3 -B -m unittest tests.test_oss_research_collect_issues

Entry: limits, no model path, the read set, the policy and child argv under
a fake supervisor, the record (complete, partial and failed) and main's
refusals. Pickup: verify_pickup.py --issues on crafted tars (synthetic
members, not a real run). No network, relay, srt or VM is used here; the
real run is on the Linux guest.
"""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


ISSUES_ENTRY = BIN / "oss-research-collect-issues"
entry = load("oss_research_collect_issues", ISSUES_ENTRY)
collect_entry = entry.load_collect_entry()
worker = collect_entry.load_worker()
wrapper = load("oss_research_linux_run_for_issues", BIN / "oss-research-linux-run")
VERIFY = REPO / "share/linux/verify_pickup.py"
SKILLS = REPO / "skills"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# The host tree's reviewed collector and sibling: the pickup loads them to
# validate, so the synthetic sources carry their real sha256 (guest paths).
HOST_ISSUES_SHA = sha256(SKILLS / "collecting-open-source-issues/scripts/collect_open_source_issues.py")
HOST_SIBLING_SHA = sha256(SKILLS / "collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py")
QUERY_SHA = hashlib.sha256(load("osr_issue_collector_query", SKILLS / "collecting-open-source-issues/scripts/"
                                "collect_open_source_issues.py").ISSUE_EVIDENCE_QUERY.encode()).hexdigest()


class IssueEntryTests(unittest.TestCase):
    GOOD = dict(repositories=["eslint-markdown/eslint-markdown"], date_field="created", state="open",
                start_at="2026-03-28T00:00:00Z", end_at="2026-09-28T00:00:00Z", max_per_repo=20,
                request_budget=60)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-issues-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def collection(self, **changes):
        values = dict(self.GOOD, **changes)
        return entry.checked_collection(worker, values["repositories"], values["date_field"],
                                        values["state"], values["start_at"], values["end_at"],
                                        values["max_per_repo"], values["request_budget"])

    def test_limits_are_fixed_and_refused_outside(self):
        self.assertEqual(self.collection().as_request()["max_per_repo"], 20)
        self.assertEqual(self.collection(end_at="2026-09-28T00:00:00Z",
                                         start_at="2026-03-28T00:00:00Z").as_request()["request_budget"], 60)
        self.assertEqual(len(self.collection(repositories=["a/b", "c/d"], max_per_repo=10).repositories), 2)
        bad = {"cap 21": dict(max_per_repo=21), "cap 0": dict(max_per_repo=0),
               "cap true": dict(max_per_repo=True), "two repos over 20": dict(repositories=["a/b", "c/d"]),
               "budget 91": dict(request_budget=91), "budget true": dict(request_budget=True),
               "no date field": dict(date_field=None), "other date field": dict(date_field="merged"),
               "other state": dict(state="merged"), "closed open": dict(date_field="closed", state="open"),
               "no Z": dict(start_at="2026-03-28T00:00:00"), "date only": dict(end_at="2026-09-28"),
               "backwards": dict(start_at="2026-09-29T00:00:00Z"),
               "185 days": dict(start_at="2026-03-27T00:00:00Z"),
               "bad repo": dict(repositories=["octo"]), "duplicate": dict(repositories=["a/b", "A/b"]),
               "no repo": dict(repositories=[])}
        for label, change in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.collection(**change)
        # 184 days exactly is the edge that passes.
        self.collection(start_at="2026-03-28T00:00:00Z", end_at="2026-09-28T00:00:00Z")

    def test_the_budget_ceiling_covers_a_full_cap_and_the_defaults_fit_their_budget(self):
        """About 3 fixed requests and 4 per Issue (measured 2026-10-03: 14 Issues took 59-60):
        a cap that the budget cannot read must not be the default, and a full cap must fit the ceiling."""
        self.assertEqual((entry.MAX_ISSUES, entry.MAX_REQUEST_BUDGET), (20, 90))
        self.assertLessEqual(3 + 4 * entry.MAX_ISSUES, entry.MAX_REQUEST_BUDGET)
        self.assertEqual((entry.DEFAULT_ISSUES, entry.DEFAULT_REQUEST_BUDGET), (14, 60))
        self.assertLessEqual(3 + 4 * entry.DEFAULT_ISSUES, entry.DEFAULT_REQUEST_BUDGET)
        for cap, budget in ((14, 60), (15, 70), (20, 90)):
            with self.subTest(cap=cap, budget=budget):
                request = self.collection(max_per_repo=cap, request_budget=budget).as_request()
                self.assertEqual((request["max_per_repo"], request["request_budget"]), (cap, budget))

    def test_the_command_line_defaults_are_a_cap_the_default_budget_can_read(self):
        parsed = entry.build_parser().parse_args(["--out-dir", "/x", "--repo", "a/b", "--date-field", "updated",
                                                  "--state", "open", "--start-at", "2026-07-05T00:00:00Z",
                                                  "--end-at", "2026-10-03T00:00:00Z"])
        self.assertEqual((parsed.max_per_repo, parsed.request_budget), (14, 60))

    def test_closed_and_all_states_and_the_closed_date_field_are_accepted(self):
        for state, date_field in (("closed", "updated"), ("closed", "closed"), ("closed", "created"),
                                  ("all", "closed"), ("all", "updated"), ("open", "updated")):
            with self.subTest(state=state, date_field=date_field):
                request = self.collection(state=state, date_field=date_field).as_request()
                self.assertEqual((request["state"], request["date_field"]), (state, date_field))

    def test_the_pr_entry_and_its_limits_are_unchanged(self):
        self.assertEqual((collect_entry.MAX_PER_REPO, collect_entry.MAX_REQUEST_BUDGET,
                          collect_entry.MAX_INTERVAL_DAYS), (5, 60, 31))
        self.assertEqual(wrapper.ENTRIES["collect"], "bin/oss-research-collect")
        self.assertEqual(wrapper.ENTRIES["collect-issues"], entry.ENTRY)
        self.assertEqual(set(wrapper.ENTRIES), {"worker", "collect", "collect-issues", "candidate-reads", "verify"})

    def test_the_entry_has_no_model_path(self):
        code = ISSUES_ENTRY.read_text().split('"""', 2)[2]
        for name in ("load_credential", "checked_credential", "CREDENTIAL_VARIABLE", "require_runtime",
                     "Selection(", "build_command", "run_research", "--model", "--effort",
                     "--permission-mode", "--credential-file", "--prompt-file", "--label", "GH_TOKEN"):
            self.assertNotIn(name, code, name)
        self.assertIs(worker.SYSTEM_READ_MEASURED["linux"], False)
        self.assertTrue(os.access(ISSUES_ENTRY, os.X_OK))

    def test_the_read_set_is_the_issue_collectors_revision_files_and_its_sibling(self):
        path = entry.collector_file(worker)
        self.assertEqual(path.name, "collect_open_source_issues.py")
        collector = load("osr_issue_collector", path)
        self.assertEqual(tuple(sorted(entry.COLLECTOR_REVISION_FILES)),
                         tuple(sorted(collector.SKILL_REVISION_PATHS)))
        self.assertEqual(Path(collector._sibling.__file__).resolve(), entry.sibling_file(worker))
        read = entry.collection_read(worker, "/usr/bin/python3")
        skill = path.parents[1]
        # The adapter loads relay-collect beside itself by path (its ADAPTER).
        adapter = load("osr_relay_issues_for_readset", entry.adapter_file(worker))
        self.assertEqual(entry.ADAPTER_DEPENDENCIES, (adapter.ADAPTER,))
        self.assertEqual(read, ["/usr/bin/python3",
                                str(entry.adapter_file(worker).with_name(adapter.ADAPTER)),
                                *[str(skill / f) for f in entry.COLLECTOR_REVISION_FILES],
                                str(entry.sibling_file(worker)), *entry.TZ_READ])
        self.assertNotIn(str(skill), read)
        self.assertNotIn(str(entry.sibling_file(worker).parents[1]), read)
        # The relay hashes the same two files as its ready sources.
        _, skills = worker.trusted_relay(Path(worker.__file__))
        sources = {s["role"]: s["path"] for s in worker.relay_sources(skills)}
        self.assertEqual(sources, {"issues": str(path), "sibling": str(entry.sibling_file(worker))})

    def test_policy_and_argv_under_the_supervisor(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            seen.update(policy=policy_for(4242), command=command_for(Path("/p.json")),
                        environment=environment, options=options, profile=profile)
            return True
        out = self.root / "out"
        out.mkdir()
        runtime = ["/opt/node", "/opt/oss-research/srt-0.0.77/node_modules/x/cli.js"]
        with mock.patch.object(worker, "run_supervised", fake_supervised), \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HTTP_PROXY": "http://10.0.0.1:1",
                                             "GH_TOKEN": "gho_parentsecret"}):
            entry.run_collection(worker, collect_entry, runtime, out, self.collection(), 60.0, launch=None,
                                 upstream="loopback-mock")
        network = seen["policy"]["network"]
        self.assertEqual(network["allowedDomains"], ["127.0.0.1:4242"])
        self.assertIs(network["allowLocalBinding"], False)
        read = seen["policy"]["filesystem"]["allowRead"]
        for path in (entry.collector_file(worker), entry.sibling_file(worker), entry.adapter_file(worker)):
            self.assertIn(str(path), read)
        self.assertIn("/usr/share/zoneinfo/UTC", read)
        self.assertFalse(any("claude" in p.lower() or "credential" in p.lower() for p in read), read)
        command = seen["command"]
        self.assertEqual(command[:4], [*runtime, "--settings", "/p.json"])
        self.assertEqual(command[-1], "--require-proxy")
        self.assertEqual(Path(command[command.index("-B") + 1]).name, "oss-research-relay-issues")
        for option, value in (("--relay-url", "http://127.0.0.1:4242"), ("--date-field", "created"),
                              ("--state", "open"), ("--max-per-repo", "20"), ("--request-budget", "60"),
                              ("--start-at", "2026-03-28T00:00:00Z"), ("--end-at", "2026-09-28T00:00:00Z"),
                              ("--repo", "eslint-markdown/eslint-markdown")):
            self.assertEqual(command[command.index(option) + 1], value, option)
        self.assertNotIn("--label", command)
        self.assertNotIn("--markdown-output", command)
        self.assertFalse({k for k in seen["environment"] if "proxy" in k.lower() or "TOKEN" in k})
        self.assertIn("record", seen["options"])

    def test_no_relay_port_starts_no_child(self):
        seen = {}

        def fake_supervised(input_dir, run_dir, profile, seconds, launch, policy_for, command_for,
                            environment, **options):
            seen["command"] = command_for(Path("/p.json"))
            return False
        out = self.root / "out"
        out.mkdir()
        with mock.patch.object(worker, "run_supervised", fake_supervised):
            entry.run_collection(worker, collect_entry, ["/n", "/c"], out, self.collection(), 60.0, None)
        self.assertEqual(seen["command"], ["/nonexistent/no-relay-port"])

    def fake_run(self, name, outputs=entry.OUTPUTS):
        run_dir = self.root / name / "run"
        run_dir.parent.mkdir()
        worker.prepare_run(run_dir, {"filesystem": {}, "network": {}})
        for item in outputs:
            (run_dir / "output" / item).write_text("{}")
        return run_dir

    def recorded(self, run_dir, exit_code=0, failures=()):
        record = entry.recorder(worker, collect_entry, self.collection(), "loopback-mock",
                                "oss-research-0123456789abcdef")
        relay = {"sources": [], "relay_sha256": "e" * 64, "port": 4242, "stderr": "relay log"}
        kept = record(run_dir, exit_code, "", "adapter stderr", list(failures), relay)
        manifest = json.loads((run_dir / "result/manifest.json").read_text())
        envelope = json.loads((run_dir / "result/collection-envelope.json").read_text())
        return kept, manifest, envelope

    def test_record_keeps_the_collectors_status_and_never_raises_it(self):
        kept, manifest, envelope = self.recorded(self.fake_run("complete"))
        self.assertTrue(kept)
        self.assertEqual((manifest["kind"], manifest["status"]), ("issue-collection", "succeeded"))
        self.assertEqual(envelope["schema"], entry.ENVELOPE_SCHEMA)
        self.assertEqual(set(envelope["files"]), {*entry.OUTPUTS, "relay.log", "run-manifest.json"})
        self.assertEqual(envelope["request"], self.collection().as_request())
        kept, manifest, envelope = self.recorded(self.fake_run("partial"), exit_code=3)
        self.assertTrue(kept)
        self.assertEqual((manifest["status"], manifest["problems"], envelope["adapter_exit"]),
                         ("partial", [], 3))
        for label, run_dir, arguments in (
                ("collector failed", self.fake_run("a"), dict(exit_code=4)),
                ("bad input", self.fake_run("b"), dict(exit_code=2)),
                ("stopped", self.fake_run("c"), dict(exit_code=None, failures=["timeout"])),
                ("partial with a supervisor failure", self.fake_run("d"), dict(exit_code=3,
                                                                               failures=["timeout"])),
                ("missing output", self.fake_run("e", ("corpus.json",)), {}),
                ("partial with a missing output", self.fake_run("f", ("corpus.json",)), dict(exit_code=3))):
            with self.subTest(label):
                kept, manifest, _ = self.recorded(run_dir, **arguments)
                self.assertFalse(kept)
                self.assertEqual(manifest["status"], "failed")
                self.assertTrue(manifest["problems"])

    def test_outputs_are_read_only_as_bounded_regular_files(self):
        secret = self.root / "outside.txt"
        secret.write_text("not the child's")
        cases = {"symlink": lambda path: path.symlink_to(secret),
                 "fifo": lambda path: os.mkfifo(path),
                 "directory": lambda path: path.mkdir(),
                 "oversize (sparse)": lambda path: os.truncate(path.open("w").name,
                                                               collect_entry.MAX_OUTPUT_BYTES + 1)}
        for label, make in cases.items():
            with self.subTest(label):
                run_dir = self.fake_run(label.split()[0], [n for n in entry.OUTPUTS if n != "corpus.json"])
                make(run_dir / "output/corpus.json")
                began = time.monotonic()
                kept, manifest, envelope = self.recorded(run_dir)
                self.assertLess(time.monotonic() - began, 5)
                self.assertFalse(kept)
                self.assertTrue(any(p.startswith("output corpus.json:") for p in manifest["problems"]))
                self.assertNotIn("corpus.json", envelope["files"])

    def test_main_refuses_bad_input_and_runs_only_in_the_unit(self):
        out = self.root / "out"
        out.mkdir()
        base = ["--out-dir", str(out), "--repo", "eslint-markdown/eslint-markdown", "--state", "open",
                "--start-at", "2026-03-28T00:00:00Z", "--end-at", "2026-09-28T00:00:00Z"]
        with mock.patch.object(sys, "stderr"):
            with self.assertRaises(SystemExit) as refused:
                entry.main(base)  # no --date-field: argparse refuses, there is no default
            self.assertEqual(refused.exception.code, 2)
            good = base + ["--date-field", "created"]
            self.assertEqual(entry.main(good + ["--max-per-repo", "21"]), 2)
            self.assertEqual(entry.main(good + ["--request-budget", "91"]), 2)
            self.assertEqual(entry.main(good + ["--timeout", "1801"]), 2)
            (out / "x").write_text("")
            self.assertEqual(entry.main(good), 2)
            (out / "x").unlink()
            environment = {k: v for k, v in os.environ.items() if k != worker.UNIT_ENV_MARKER}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(entry.main(good), 3)
        self.assertEqual(list(out.iterdir()), [])


class IssuePickupTests(unittest.TestCase):
    """verify_pickup.py --issues on crafted tars. Synthetic members, not a real run."""

    ISSUES = {"role": "issues", "path": "/opt/x/collect_open_source_issues.py", "sha256": HOST_ISSUES_SHA}
    SIBLING = {"role": "sibling", "path": "/opt/x/collect_recent_closed_prs.py", "sha256": HOST_SIBLING_SHA}
    REPOSITORY = "eslint-markdown/eslint-markdown"
    REQUEST = {"repositories": [REPOSITORY], "date_field": "created", "state": "open",
               "start_at": "2026-03-28T00:00:00Z", "end_at": "2026-09-28T00:00:00Z",
               "max_per_repo": 20, "request_budget": 60}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="osr-issuepick-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def record(self, number):
        return {"record_key": f"github-issue:{self.REPOSITORY}#{number}",
                "repository": {"full_name": self.REPOSITORY}, "issue": {"number": number},
                "evidence_snapshot": {}, "sources": [], "state_history": []}

    def members(self, status="succeeded"):
        adapter, collection = {"succeeded": (0, "complete"), "partial": (3, "partial")}[status]
        requests = [{"logical_url": "https://api.github.com/versions", "outcome": "relayed",
                     "upstream_status": 200}] * 8
        requests.append({"logical_url": "https://api.github.com/graphql", "method": "POST",
                         "operation": "IssueEvidence", "outcome": "upstream-error", "upstream_status": 401})
        sources = [self.ISSUES, self.SIBLING]
        return {
            "corpus.json": {"schema_version": "1.0.0", "corpus_kind": "github-issue",
                            "records": [self.record(1), self.record(2)]},
            "collection-manifest.json": {
                "schema_version": "2.0.0", "generated_by": {"name": "collecting-open-source-issues"},
                "records": [{"collection_status": collection,
                             "filters": {"repositories": [self.REPOSITORY], "date_field": "created",
                                         "state": "open", "labels": [], "label_semantics": "all-of"},
                             "interval": {"start_at": "2026-03-28T00:00:00Z", "end_at": "2026-09-28T00:00:00Z"},
                             "max_per_repository": 20, "request_budget": 60, "request_count": 9,
                             "repositories": [{"repository": self.REPOSITORY, "selected_count": 2}]}]},
            "transport.json": {"transport": "srt-proxy", "exit_code": adapter,
                               "relay_base_url": "http://127.0.0.1:4242",
                               "collector": {
                                   "issues": {"path": self.ISSUES["path"], "sha256": self.ISSUES["sha256"]},
                                   "sibling": {"path": self.SIBLING["path"], "sha256": self.SIBLING["sha256"]},
                                   "issue_evidence_query_sha256": QUERY_SHA},
                               "requests": requests},
            "relay.log": "",
            "run-manifest.json": {"kind": "issue-collection", "status": status, "exit_code": adapter,
                                  "failures": [], "problems": [],
                                  "github_token": {"authenticated": False, "token_file_sha256": None},
                                  "relay": {"sources": sources, "relay_sha256": "e" * 64, "port": 4242}},
            "envelope": {"schema": "oss-research-issue-collection-envelope/1",
                         "entry": "bin/oss-research-collect-issues",
                         "unit": "oss-research-0123456789abcdef", "upstream": "loopback-mock",
                         "github_token": {"authenticated": False, "token_file_sha256": None},
                         "request": dict(self.REQUEST), "adapter_exit": adapter,
                         "relay": {"sources": sources, "relay_sha256": "e" * 64, "port": 4242}},
        }

    def pack(self, name, members, raw=None, keep_hashes=None, extra=None):
        raw = raw or {}
        data = {}
        for member, value in members.items():
            if member == "envelope":
                continue
            data[member] = raw.get(member) or (value if isinstance(value, str) else json.dumps(value)).encode()
        envelope = dict(members["envelope"])
        envelope["files"] = {m: {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                             for m, b in data.items()}
        if keep_hashes:
            envelope["files"].update(keep_hashes)
        data["collection-envelope.json"] = raw.get("collection-envelope.json") or json.dumps(envelope).encode()
        archive = self.root / f"{name}.tar"
        with tarfile.open(archive, "w") as bundle:
            for member, blob in data.items():
                info = tarfile.TarInfo("./" + member)
                info.size = len(blob)
                bundle.addfile(info, io.BytesIO(blob))
            for info, blob in extra or ():
                bundle.addfile(info, io.BytesIO(blob) if blob is not None else None)
        return archive

    def wrapper(self, status="succeeded", exit_code=None, **changes):
        inner = dict({"mode": "collect-issues", "status": status}, **changes.pop("inner", {}))
        code = {"succeeded": 0, "partial": 5}.get(status, 4) if exit_code is None else exit_code
        record = dict({"unit": "oss-research-0123456789abcdef", "exit_code": code, "failures": [],
                       "stdout": json.dumps(inner) + "\n"}, **changes)
        path = self.root / f"wrapper-{len(list(self.root.glob('wrapper-*')))}.json"
        path.write_text(json.dumps(record) + "\n")
        return path

    def verify(self, archive, wrapper=None, upstream="loopback-mock", issues_sha=None, sibling_sha=None):
        # A new destination per call: verify_pickup refuses an existing one.
        dest = self.root / f"out-{archive.stem}-{len(list(self.root.glob('out-*')))}"
        self.assertFalse(dest.exists())
        completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(archive),
                                    str(1 << 20), str(dest), "--issues",
                                    "--issue-collector-sha256", issues_sha or self.ISSUES["sha256"],
                                    "--sibling-sha256", sibling_sha or self.SIBLING["sha256"],
                                    "--wrapper-record", str(wrapper or self.wrapper()),
                                    "--upstream", upstream],
                                   capture_output=True, text=True, timeout=60)
        return completed.returncode, json.loads(completed.stdout)

    def changed(self, path, value, status="succeeded"):
        members = json.loads(json.dumps(self.members(status)))
        target = members
        for key in path[:-1]:
            target = target[key]
        if value is KeyError:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return members

    def test_a_bound_complete_collection_passes_and_a_bound_partial_one_is_only_partial(self):
        code, seen = self.verify(self.pack("good", self.members()))
        self.assertEqual((code, seen["problems"], seen["records"], seen["collection_status"]),
                         (0, [], 2, "complete"), seen)
        self.assertEqual((seen["contract"], seen["upstream"], seen["upstream_errors"]),
                         ("issues", "loopback-mock", {"401": 1}))
        code, seen = self.verify(self.pack("partial", self.members("partial")), self.wrapper("partial"))
        self.assertEqual((code, seen["problems"], seen["collection_status"]), (3, [], "partial"), seen)
        empty = self.changed(("corpus.json", "records"), [])
        empty["collection-manifest.json"]["records"][0]["repositories"][0]["selected_count"] = 0
        self.assertEqual(self.verify(self.pack("no-results", empty))[0], 0)

    def test_every_binding_fails_closed(self):
        c = self.changed
        cases = {
            "partial called succeeded": c(("collection-manifest.json", "records", 0, "collection_status"), "partial"),
            "failed collection": c(("collection-manifest.json", "records", 0, "collection_status"), "failed"),
            "failed run": c(("run-manifest.json", "status"), "failed"),
            "run of the PR kind": c(("run-manifest.json", "kind"), "collection"),
            "run problems": c(("run-manifest.json", "problems"), ["timeout"]),
            "run failures": c(("run-manifest.json", "failures"), ["timeout"]),
            "adapter exit 3 on succeeded": c(("envelope", "adapter_exit"), 3),
            "adapter exit false": c(("envelope", "adapter_exit"), False),
            "run exit 0.0": c(("run-manifest.json", "exit_code"), 0.0),
            "PR envelope": c(("envelope", "schema"), "oss-research-collection-envelope/1"),
            "other entry": c(("envelope", "entry"), "bin/oss-research-collect"),
            "direct transport": c(("transport.json", "transport"), "direct"),
            "transport exit": c(("transport.json", "exit_code"), 3),
            "unrelayed request": c(("transport.json", "requests"), [{"outcome": "relay-refused"}] * 9),
            "no requests": c(("transport.json", "requests"), []),
            "count mismatch": c(("collection-manifest.json", "records", 0, "request_count"), 8),
            "count true": c(("collection-manifest.json", "records", 0, "request_count"), True),
            "over budget": c(("collection-manifest.json", "records", 0, "request_count"), 61),
            "budget 61 requested": c(("envelope", "request", "request_budget"), 61),
            "cap 21 requested": c(("envelope", "request", "max_per_repo"), 21),
            "extra request field": c(("envelope", "request", "labels"), ["bug"]),
            "other collector": c(("transport.json", "collector", "issues", "sha256"), "f" * 64),
            "other collector path": c(("transport.json", "collector", "issues", "path"), "/tmp/x.py"),
            "other sibling": c(("transport.json", "collector", "sibling", "sha256"), "f" * 64),
            "no sibling": c(("transport.json", "collector", "sibling"), KeyError),
            "extra collector field": c(("transport.json", "collector", "extra"), {"path": "/x", "sha256": "0" * 64}),
            "other query": c(("transport.json", "collector", "issue_evidence_query_sha256"), "0" * 64),
            "relay source with an extra key": c(("envelope", "relay", "sources"),
                                                [dict(self.ISSUES, extra=1), self.SIBLING]),
            "upstream error without status": c(("transport.json", "requests", 8, "upstream_status"), KeyError),
            "upstream error status 200": c(("transport.json", "requests", 8, "upstream_status"), 200),
            "upstream error status true": c(("transport.json", "requests", 8, "upstream_status"), True),
            "relay refused": c(("transport.json", "requests", 8, "outcome"), "relay-refused"),
            "relay timeout": c(("transport.json", "requests", 8, "outcome"), "relay-timeout"),
            "budget exhausted at the relay": c(("transport.json", "requests", 8, "outcome"),
                                               "relay-budget-exhausted"),
            "malformed envelope": c(("transport.json", "requests", 8, "outcome"), "malformed_envelope"),
            "transport failed": c(("transport.json", "requests", 8, "outcome"), "upstream-transport-failed"),
            "date field requested": c(("envelope", "request", "date_field"), "merged"),
            "state requested": c(("envelope", "request", "state"), "merged"),
            "closed date of open Issues": c(("envelope", "request", "date_field"), "closed"),
            "185 days": c(("envelope", "request", "start_at"), "2026-03-27T00:00:00Z"),
            "backwards": c(("envelope", "request", "start_at"), "2026-09-29T00:00:00Z"),
            "no Z": c(("envelope", "request", "end_at"), "2026-09-28T00:00:00"),
            "cap 0": c(("envelope", "request", "max_per_repo"), 0),
            "budget 0": c(("envelope", "request", "request_budget"), 0),
            "cap float": c(("envelope", "request", "max_per_repo"), 20.0),
            "repository without an owner": c(("envelope", "request", "repositories"), ["eslint-markdown"]),
            "status a list": c(("run-manifest.json", "status"), ["succeeded"]),
            "status a dict": c(("run-manifest.json", "status"), {"s": 1}),
            "role a list": c(("envelope", "relay", "sources"), [dict(self.ISSUES, role=["issues"]), self.SIBLING]),
            "record key a list": c(("corpus.json", "records"), [dict(self.record(1), record_key=["k"]),
                                                                self.record(2)]),
            "repositories a dict": c(("envelope", "request", "repositories"), {"a": "b"}),
            "requests a dict": c(("transport.json", "requests"), {"a": 1}),
            "manifest runs a string": c(("collection-manifest.json", "records"), "run"),
            "corpus records a dict": c(("corpus.json", "records"), {"a": 1}),
            "validator: record without sources": c(("corpus.json", "records"),
                                                   [dict(self.record(1), sources="x"), self.record(2)]),
            "relay with a third source": c(("envelope", "relay", "sources"),
                                           [self.ISSUES, self.SIBLING, {"role": "x", "path": "/x", "sha256": "0" * 64}]),
            "other relay port": c(("transport.json", "relay_base_url"), "http://127.0.0.1:1"),
            "relay differs": c(("run-manifest.json", "relay", "relay_sha256"), "0" * 64),
            "PR manifest": c(("collection-manifest.json", "generated_by", "name"), "collecting-recent-closed-prs"),
            "old manifest schema": c(("collection-manifest.json", "schema_version"), "1.0.0"),
            "two runs (resumed)": c(("collection-manifest.json", "records"),
                                    [self.members()["collection-manifest.json"]["records"][0]] * 2),
            "other repository": c(("collection-manifest.json", "records", 0, "filters", "repositories"), ["a/b"]),
            "other date field": c(("collection-manifest.json", "records", 0, "filters", "date_field"), "updated"),
            "other state": c(("collection-manifest.json", "records", 0, "filters", "state"), "all"),
            "labels": c(("collection-manifest.json", "records", 0, "filters", "labels"), ["bug"]),
            "other interval": c(("collection-manifest.json", "records", 0, "interval", "start_at"),
                                "2026-03-29T00:00:00Z"),
            "other cap": c(("collection-manifest.json", "records", 0, "max_per_repository"), 25),
            "selected differs": c(("collection-manifest.json", "records", 0, "repositories", 0, "selected_count"), 3),
            "PR corpus": c(("corpus.json", "corpus_kind"), "github-pull-request"),
            "foreign record": c(("corpus.json", "records"),
                                [dict(self.record(1), repository={"full_name": "x/y"}), self.record(2)]),
            "PR record key": c(("corpus.json", "records"),
                               [dict(self.record(1), record_key="github-pr:x"), self.record(2)]),
            "repeated key": c(("corpus.json", "records"), [self.record(1), self.record(1)]),
            "record a string": c(("corpus.json", "records"), ["eslint-markdown/eslint-markdown", self.record(2)]),
            "no upstream fact": c(("envelope", "upstream"), None),
            "unknown upstream": c(("envelope", "upstream"), "api.github.com"),
        }
        many = self.changed(("corpus.json", "records"), [self.record(n) for n in range(1, 22)])
        many["collection-manifest.json"]["records"][0]["repositories"][0]["selected_count"] = 21
        cases["21 records"] = many
        for label, members in cases.items():
            with self.subTest(label):
                code, seen = self.verify(self.pack(label.replace(" ", "-"), members))
                self.assertEqual(code, 1, seen)
                self.assertTrue(seen["problems"], seen)
        good = self.pack("good-for-host", self.members())
        partial = self.pack("partial-for-host", self.members("partial"))
        host_cases = {
            "live asked of a mock": (good, dict(upstream="github-rest-via-relay")),
            "host reviewed another collector": (good, dict(issues_sha="a" * 64)),
            "host reviewed another sibling": (good, dict(sibling_sha="a" * 64)),
            "wrapper exit 5 on complete": (good, dict(wrapper=self.wrapper(exit_code=5))),
            "wrapper exit 0 on partial": (partial, dict(wrapper=self.wrapper("partial", exit_code=0))),
            "wrapper says succeeded on partial": (partial, dict(wrapper=self.wrapper("succeeded", exit_code=5))),
            "wrapper failures": (good, dict(wrapper=self.wrapper(failures=["timeout"]))),
            "wrapper other unit": (good, dict(wrapper=self.wrapper(unit="oss-research-ffffffffffffffff"))),
            "wrapper PR mode": (good, dict(wrapper=self.wrapper(inner={"mode": "collect"}))),
        }
        for label, (archive, options) in host_cases.items():
            with self.subTest(label):
                code, seen = self.verify(archive, **options)
                self.assertEqual(code, 1, seen)
                self.assertTrue(seen["problems"], seen)
        raw_cases = {"top-level list": {"corpus.json": b"[]"}, "number": {"transport.json": b"4"},
                     "NaN": {"run-manifest.json": b'{"status": NaN}'},
                     "duplicate key": {"collection-manifest.json": b'{"records": [], "records": []}'},
                     "envelope files a list": {"collection-envelope.json": json.dumps(
                         dict(self.members()["envelope"], files=["corpus.json"])).encode()}}
        for label, raw in raw_cases.items():
            with self.subTest(label):
                code, seen = self.verify(self.pack(label.replace(" ", "-"), self.members(), raw))
                self.assertEqual(code, 1, seen)
        tampered = self.pack("hash", self.members(), keep_hashes={"corpus.json": {"bytes": 1, "sha256": "0" * 64}})
        self.assertEqual(self.verify(tampered)[0], 1)

    def test_a_consistent_request_outside_the_entry_limits_fails(self):
        """The request and the manifest agree, so only the limit check can refuse it."""
        def consistent(**request):
            members = self.members()
            members["envelope"]["request"].update(request)
            run = members["collection-manifest.json"]["records"][0]
            for key in ("date_field", "state", "repositories"):
                if key in request:
                    run["filters"][key] = request[key]
            for key in ("start_at", "end_at"):
                if key in request:
                    run["interval"][key] = request[key]
            if "max_per_repo" in request:
                run["max_per_repository"] = request["max_per_repo"]
            if "request_budget" in request:
                run["request_budget"] = request["request_budget"]
            return members
        cases = {"185 days": dict(start_at="2026-03-27T00:00:00Z"),
                 "backwards": dict(start_at="2026-09-29T00:00:00Z"),
                 "no Z": dict(start_at="2026-03-28T00:00:00", end_at="2026-09-28T00:00:00"),
                 "date field merged": dict(date_field="merged"), "state merged": dict(state="merged"),
                 "closed date of open Issues": dict(date_field="closed"),
                 "cap 21": dict(max_per_repo=21), "budget 91": dict(request_budget=91)}
        for label, request in cases.items():
            with self.subTest(label):
                code, seen = self.verify(self.pack("limit-" + label.replace(" ", "-"), consistent(**request)))
                self.assertEqual(code, 1, seen)
                self.assertTrue(any(p.startswith("envelope request") for p in seen["problems"]), seen)

    def test_the_artifact_set_is_fixed(self):
        def info(name, kind=tarfile.REGTYPE, size=0, link=""):
            item = tarfile.TarInfo(name)
            item.type, item.size, item.linkname = kind, size, link
            return item
        cases = {
            "symlink member": [(info("./notes.json", tarfile.SYMTYPE, link="/etc/passwd"), None)],
            "hard link member": [(info("./relay.log", tarfile.LNKTYPE, link="./corpus.json"), None)],
            "path escape": [(info("../corpus.json", size=2), b"{}")],
            "absolute path": [(info("/tmp/corpus.json", size=2), b"{}")],
            "duplicate member": [(info("./relay.log", size=1), b"x")],
            "extra member": [(info("./inventory.md", size=1), b"x")],
            "directory member": [(info("./output", tarfile.DIRTYPE), None)],
        }
        for label, extra in cases.items():
            with self.subTest(label):
                code, seen = self.verify(self.pack(label.replace(" ", "-"), self.members(), extra=extra))
                self.assertEqual(code, 1, seen)
                self.assertTrue(seen["problems"], seen)
                self.assertEqual(list(self.root.glob(f"out-{label.replace(' ', '-')}-*")), [])
        members = self.members()
        del members["relay.log"]
        code, seen = self.verify(self.pack("missing", members))
        self.assertEqual(code, 1)
        self.assertTrue(any("missing" in p or "exactly" in p for p in seen["problems"]), seen)

    def test_the_command_line_is_exact(self):
        archive = self.pack("cli", self.members())
        base = [sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 20), str(self.root / "o")]
        for label, tail in {
                "no sibling sha": ["--issues", "--issue-collector-sha256", "d" * 64, "--wrapper-record",
                                   str(self.wrapper()), "--upstream", "loopback-mock"],
                "short sha": ["--issues", "--issue-collector-sha256", "d" * 63, "--sibling-sha256", "c" * 64,
                              "--wrapper-record", str(self.wrapper()), "--upstream", "loopback-mock"],
                "other upstream": ["--issues", "--issue-collector-sha256", "d" * 64, "--sibling-sha256", "c" * 64,
                                   "--wrapper-record", str(self.wrapper()), "--upstream", "api.github.com"]}.items():
            with self.subTest(label):
                completed = subprocess.run(base + tail, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse((self.root / "o").exists())


# B's adapter harness (the real relay and adapter over a loopback mock
# api.github.com), read-only: this file owns none of it.
import tests.test_oss_research_relay_issues as relay_issues_tests  # noqa: E402


def setUpModule():
    relay_issues_tests.setUpModule()  # the loopback-only network guard


def tearDownModule():
    relay_issues_tests.tearDownModule()


class OfflinePipeline(relay_issues_tests.Harness):
    """entry argv -> B's adapter (real relay, loopback mock) -> entry record -> tar -> pickup.

    No srt here, so the adapter connects directly and its transport says
    "direct": that one line, and nothing else, is what the pickup refuses. The
    srt-proxy leg is the Linux guest's."""

    def pipeline(self, name, status_code):
        collection = entry.checked_collection(worker, [relay_issues_tests.TARGET], "created", "open",
                                              *relay_issues_tests.INTERVAL, 5, 60)
        run_dir = self.root.resolve() / name / "run"  # the record refuses an aliased (/var) path
        run_dir.parent.mkdir()
        worker.prepare_run(run_dir, {"filesystem": {}, "network": {}})
        argv = entry.adapter_arguments(worker, collection, self.port, run_dir)
        self.assertEqual(argv[-1], "--require-proxy")
        refused = relay_issues_tests.adapter.run(argv, {}, captured_at=relay_issues_tests.CAPTURED_AT)
        self.assertEqual(refused, 2)  # --require-proxy without srt's proxy: stops before the network
        self.assertEqual(sorted(p.name for p in (run_dir / "output").iterdir()), [])
        code = relay_issues_tests.adapter.run(argv[:-1], {}, captured_at=relay_issues_tests.CAPTURED_AT,
                                              sleeper=lambda seconds: None,
                                              clock=lambda: relay_issues_tests.FIXED_TIME)
        self.assertEqual(code, status_code)
        _, skills = worker.trusted_relay(Path(worker.__file__))
        relay = {"sources": worker.relay_sources(skills), "relay_sha256": "e" * 64, "port": self.port,
                 "stderr": ""}
        record = entry.recorder(worker, collect_entry, collection, "loopback-mock", "oss-research-0123456789abcdef")
        kept = record(run_dir, code, "", "", [], relay)
        manifest = json.loads((run_dir / "result/manifest.json").read_text())
        archive = self.root / (name + ".tar")
        with tarfile.open(archive, "w") as bundle:
            for member, path in (("corpus.json", "output/corpus.json"),
                                 ("collection-manifest.json", "output/collection-manifest.json"),
                                 ("transport.json", "output/transport.json"), ("relay.log", "result/relay.log"),
                                 ("run-manifest.json", "result/manifest.json"),
                                 ("collection-envelope.json", "result/collection-envelope.json")):
                bundle.add(run_dir / path, arcname=member)
        exit_code = {"succeeded": 0, "partial": 5}.get(manifest["status"], 4)
        wrapper_record = self.root / (name + "-wrapper.json")
        wrapper_record.write_text(json.dumps({
            "unit": "oss-research-0123456789abcdef", "exit_code": exit_code, "failures": [],
            "stdout": json.dumps({"mode": "collect-issues", "status": manifest["status"]}) + "\n"}) + "\n")
        completed = subprocess.run([sys.executable, "-I", "-B", str(VERIFY), str(archive), str(1 << 24),
                                    str(self.root / (name + "-out")), "--issues",
                                    "--issue-collector-sha256", HOST_ISSUES_SHA, "--sibling-sha256",
                                    HOST_SIBLING_SHA, "--wrapper-record", str(wrapper_record),
                                    "--upstream", "loopback-mock"], capture_output=True, text=True, timeout=60)
        return kept, manifest, completed.returncode, json.loads(completed.stdout)



class OfflinePartialPipelineTests(OfflinePipeline):
    def test_the_anonymous_graphql_401_partial_binds_except_the_missing_srt_proxy(self):
        kept, manifest, code, seen = self.pipeline("partial", 3)
        self.assertEqual((kept, manifest["status"], manifest["problems"]), (True, "partial", []))
        self.assertEqual(code, 1)
        self.assertEqual(seen["problems"], ["transport record does not say srt-proxy"], seen)


class OfflineCompletePipelineTests(OfflinePipeline):
    world_options = {"graphql_status": 200}

    def test_a_complete_collection_binds_except_the_missing_srt_proxy(self):
        kept, manifest, code, seen = self.pipeline("complete", 0)
        self.assertEqual((kept, manifest["status"]), (True, "succeeded"))
        self.assertEqual(seen["problems"], ["transport record does not say srt-proxy"], seen)


if __name__ == "__main__":
    unittest.main()
