"""Tests for the collection window planner (oss-research-windows).

The planner turns a period, repositories and outcomes into one collection run per
repository, outcome and window of at most 31 days, and prints the isolated
entry's argument lists. It runs nothing and opens nothing, so every test is a
plain subprocess call whose stdout is parsed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/oss-research-windows"
FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse(value):
    return datetime.strptime(value, FORMAT).replace(tzinfo=timezone.utc)


def plan(*args, code=0):
    done = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == code, (done.returncode, done.stdout, done.stderr)
    return (json.loads(done.stdout) if code == 0 else None), done.stderr


def base(start, end, *extra):
    return ["--start-at", start, "--end-at", end, "--repo", "eslint/eslint", "--outcome", "merged", *extra]


class WindowTests(unittest.TestCase):
    def windows_of(self, start, end, *extra):
        document, _ = plan(*base(start, end, *extra))
        return [(run["start_at"], run["end_at"]) for run in document["runs"]]

    def test_a_period_of_31_days_or_less_is_one_window(self):
        self.assertEqual([("2026-09-03T00:00:00Z", "2026-10-04T00:00:00Z")],
                         self.windows_of("2026-09-03T00:00:00Z", "2026-10-04T00:00:00Z"))
        self.assertEqual([("2026-09-03T00:00:00Z", "2026-09-10T00:00:00Z")],
                         self.windows_of("2026-09-03T00:00:00Z", "2026-09-10T00:00:00Z"))

    def test_a_longer_period_is_cut_every_31_days_and_the_last_window_is_shorter(self):
        windows = self.windows_of("2026-07-05T00:00:00Z", "2026-10-04T00:00:00Z")
        self.assertEqual([("2026-07-05T00:00:00Z", "2026-08-05T00:00:00Z"),
                          ("2026-08-05T00:00:00Z", "2026-09-05T00:00:00Z"),
                          ("2026-09-05T00:00:00Z", "2026-10-04T00:00:00Z")], windows)
        self.assertEqual([31, 31, 29], [(parse(e) - parse(s)).days for s, e in windows])

    def test_32_days_is_a_31_day_window_and_a_one_day_window(self):
        windows = self.windows_of("2026-09-01T00:00:00Z", "2026-10-03T00:00:00Z")
        self.assertEqual([31, 1], [(parse(e) - parse(s)).days for s, e in windows])

    def test_the_end_is_exclusive_so_windows_touch_without_overlap_and_tile_the_period(self):
        for start, end in (("2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z"), ("2026-03-15T06:30:00Z", "2026-09-02T18:45:10Z"),
                           ("2026-09-01T00:00:00Z", "2026-11-02T00:00:00Z")):
            with self.subTest(start=start, end=end):
                windows = self.windows_of(start, end)
                self.assertEqual(start, windows[0][0])
                self.assertEqual(end, windows[-1][1])
                for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
                    self.assertEqual(previous_end, next_start)
                for window_start, window_end in windows:
                    self.assertGreater(parse(window_end), parse(window_start))
                    self.assertLessEqual(parse(window_end) - parse(window_start), timedelta(days=31))

    def test_a_shorter_window_can_be_asked_for_but_never_a_longer_one(self):
        windows = self.windows_of("2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z", "--window-days", "7")
        self.assertEqual([7, 7, 7], [(parse(e) - parse(s)).days for s, e in windows])
        for bad in ("32", "0", "-1", "x"):
            with self.subTest(bad):
                plan(*base("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "--window-days", bad), code=2)


class RunListTests(unittest.TestCase):
    ARGS = ["--start-at", "2026-07-05T00:00:00Z", "--end-at", "2026-10-04T00:00:00Z",
            "--repo", "eslint/eslint", "--repo", "eslint/markdown",
            "--outcome", "merged", "--outcome", "closed-unmerged"]

    def test_one_run_per_repository_outcome_and_window(self):
        document, _ = plan(*self.ARGS)
        runs = document["runs"]
        self.assertEqual(2 * 2 * 3, len(runs))
        self.assertEqual(list(range(1, 13)), [run["index"] for run in runs])
        seen = {(run["repository"], run["outcome"], run["start_at"]) for run in runs}
        self.assertEqual(len(runs), len(seen))
        for repository in ("eslint/eslint", "eslint/markdown"):
            for outcome in ("merged", "closed-unmerged"):
                own = [(r["start_at"], r["end_at"]) for r in runs if r["repository"] == repository and r["outcome"] == outcome]
                self.assertEqual("2026-07-05T00:00:00Z", own[0][0])
                self.assertEqual("2026-10-04T00:00:00Z", own[-1][1])
                self.assertEqual(3, len(own))

    def test_each_run_prints_the_entry_arguments_and_nothing_is_run(self):
        document, _ = plan(*self.ARGS, "--docs-only", "title", "--max-per-repo", "5", "--request-budget", "60")
        run = document["runs"][0]
        self.assertEqual(["--entry", "collect", "--egress", "pin", "--timeout", "900", "--",
                          "--out-dir", "<OUT_DIR>", "--repo", run["repository"], "--start-at", run["start_at"],
                          "--end-at", run["end_at"], "--outcome", run["outcome"], "--docs-only", "title",
                          "--max-per-repo", "5", "--request-budget", "60", "--timeout", "840"], run["linux_run_args"])
        self.assertIs(document["executed"], False)
        self.assertIn("oss-research-linux-run", document["linux_run"])
        without, _ = plan(*self.ARGS)
        self.assertNotIn("--docs-only", without["runs"][0]["linux_run_args"])

    def test_budget_and_expected_requests_add_up(self):
        document, _ = plan(*self.ARGS, "--max-per-repo", "5", "--request-budget", "60")
        totals = document["totals"]
        self.assertEqual((12, 6, 60 * 12), (totals["runs"], totals["windows"] * 2, totals["request_budget_total"]))
        # measured: 4 requests + 7 per pull request; one run never plans past its own budget
        self.assertEqual(min(60, 4 + 7 * 5), totals["expected_requests_max_per_run"])
        self.assertEqual(12 * min(60, 4 + 7 * 5), totals["expected_requests_max"])
        self.assertEqual(document["request"]["outcome"], ["merged", "closed-unmerged"])

    def test_the_request_fragment_names_the_period_and_the_outcome_form(self):
        document, _ = plan(*self.ARGS)
        self.assertEqual({"interval": {"start_at": "2026-07-05T00:00:00Z", "end_at": "2026-10-04T00:00:00Z"},
                          "repositories": ["eslint/eslint", "eslint/markdown"],
                          "outcome": ["merged", "closed-unmerged"], "max_per_repository": 5, "request_budget": 60},
                         document["request_fragment"])
        single, _ = plan(*base("2026-09-03T00:00:00Z", "2026-10-04T00:00:00Z"))
        self.assertEqual("merged", single["request_fragment"]["outcome"])

    def test_every_run_is_inside_the_entry_limits(self):
        document, _ = plan(*self.ARGS)
        for run in document["runs"]:
            self.assertLessEqual((parse(run["end_at"]) - parse(run["start_at"])).days, 31)
            self.assertLessEqual(run["request_budget"], 60)


class RefusalTests(unittest.TestCase):
    GOOD = base("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")

    def refuse(self, *args):
        plan(*args, code=2)

    def test_malformed_input_is_refused(self):
        self.refuse("--start-at", "2026-09-01", "--end-at", "2026-10-01T00:00:00Z", "--repo", "a/b", "--outcome", "merged")
        self.refuse("--start-at", "2026-10-01T00:00:00Z", "--end-at", "2026-09-01T00:00:00Z", "--repo", "a/b", "--outcome", "merged")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-09-01T00:00:00Z", "--repo", "a/b", "--outcome", "merged")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--repo", "not a repo", "--outcome", "merged")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--repo", "a/b", "--repo", "A/B", "--outcome", "merged")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--repo", "a/b", "--outcome", "open")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--repo", "a/b")
        self.refuse("--start-at", "2026-09-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--outcome", "merged")

    def test_all_cannot_be_mixed_with_a_kind_and_a_kind_cannot_repeat(self):
        self.refuse(*self.GOOD, "--outcome", "all")
        self.refuse(*self.GOOD, "--outcome", "merged")

    def test_unknown_options_and_odd_numbers_are_refused(self):
        self.refuse(*self.GOOD, "--token", "x")
        self.refuse(*self.GOOD, "--max-per-repo", "0")
        self.refuse(*self.GOOD, "--request-budget", "61")
        self.refuse(*self.GOOD, "--max-per-repo", "6")
        self.refuse(*self.GOOD, "--docs-only", "other")
        self.refuse(*self.GOOD, "--max-runs", "0")

    def test_a_plan_with_too_many_runs_is_refused_not_cut(self):
        self.refuse("--start-at", "2020-01-01T00:00:00Z", "--end-at", "2026-10-01T00:00:00Z", "--repo", "a/b",
                    "--outcome", "merged", "--max-runs", "10")


class SourceTests(unittest.TestCase):
    """The planner only computes: nothing in it can reach a network or start a process."""

    def test_no_network_process_or_credential_code(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for needle in ("subprocess", "socket", "urllib", "http.client", "import requests", "os.system", "os.exec", "os.popen",
                       "gh api", "GH_TOKEN", "GITHUB_TOKEN", "Authorization", "open("):
            self.assertNotIn(needle, source, needle)
        imports = set(re.findall(r"^(?:from|import) +([A-Za-z_\.]+)", source, re.M))
        self.assertLessEqual(imports, {"__future__", "argparse", "datetime", "json", "re", "sys"})

    def test_the_help_says_it_is_not_an_entry(self):
        done = subprocess.run([sys.executable, "-B", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        text = " ".join(done.stdout.split())
        self.assertEqual(0, done.returncode)
        self.assertIn("not an isolated entry", text)
        self.assertIn("runs nothing", text)


if __name__ == "__main__":
    unittest.main()
