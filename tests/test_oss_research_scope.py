"""Tests for the org scope survey (executable_oss-research-scope).

The script runs as a subprocess with a fake ``gh`` first on PATH. The fake serves
fixed JSON and logs every argv, so nothing here reaches a network and no test
reads a token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dot_local/bin/executable_oss-research-scope"
FAKE_TOKEN = "ghp_FAKETOKENFORLEAKCHECK0123456789"

FAKE_GH = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys
    from urllib.parse import parse_qs, urlsplit

    root = os.environ["FAKE_GH_DIR"]
    argv = sys.argv[1:]
    with open(os.path.join(root, "calls.jsonl"), "a") as log:
        log.write(json.dumps(argv) + "\\n")
    scenario = json.load(open(os.path.join(root, "scenario.json")))
    if scenario.get("fail"):
        sys.stderr.write("gh: HTTP 500\\n")
        sys.exit(1)
    endpoint = [a for a in argv[1:] if not a.startswith("-") and a not in ("GET", "POST")][0]
    parts = urlsplit(endpoint)
    query = parse_qs(parts.query)
    page = int(query.get("page", ["1"])[0])
    per = int(query.get("per_page", ["30"])[0])
    segments = parts.path.strip("/").split("/")
    if segments[0] == "orgs" and segments[2] == "repos":
        if scenario.get("repos_endless"):
            repos = [{{"full_name": "%s/p%d-r%d" % (segments[1], page, i), "archived": False, "fork": False}}
                     for i in range(per)]
        else:
            repos = scenario["repos"][segments[1]][(page - 1) * per : page * per]
        print(json.dumps(repos))
    elif segments[:2] == ["search", "issues"]:
        q = query["q"][0]
        org = [t.split(":", 1)[1] for t in q.split() if t.startswith("org:")][0]
        kind = "pr" if "merged:" in q else "label" if "label:" in q else "title"
        names = scenario["search"][kind].get(org, [])
        items = [{{"repository_url": "https://api.github.com/repos/" + n, "number": i + 1}}
                 for i, n in enumerate(names)]
        print(json.dumps({{"total_count": len(items), "incomplete_results": False,
                          "items": items[(page - 1) * per : page * per]}}))
    else:
        sys.stderr.write("unexpected endpoint " + endpoint + "\\n")
        sys.exit(1)
    '''
)


def repo(full_name, **flags):
    return {"full_name": full_name, "archived": False, "fork": False, **flags}


SCENARIO = {
    "repos": {
        "eslint": [
            repo("eslint/eslint"),
            repo("eslint/markdown"),
            repo("eslint/quiet"),
            repo("eslint/archive-website", archived=True),
            repo("eslint/forked-thing", fork=True),
        ],
        "eslint-community": [repo("eslint-community/eslint-doc-generator")],
    },
    "search": {
        "label": {
            "eslint": ["eslint/eslint"] * 10 + ["eslint/markdown"] * 3
            + ["eslint/archive-website"] * 2 + ["eslint/forked-thing"],
            "eslint-community": [],
        },
        "title": {
            "eslint": ["eslint/eslint"] * 4 + ["eslint/archive-website"],
            "eslint-community": [],
        },
        "pr": {
            "eslint": ["eslint/eslint"] * 26 + ["eslint/markdown"] * 2 + ["eslint/forked-thing"] * 5,
            "eslint-community": ["eslint-community/eslint-doc-generator"],
        },
    },
}


class ScopeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake_dir = self.tmp / "fake"
        self.fake_dir.mkdir()
        gh = self.fake_dir / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
        gh.chmod(0o755)
        self.out = self.tmp / "out"

    def scenario(self, data):
        (self.fake_dir / "scenario.json").write_text(json.dumps(data), encoding="utf-8")

    def run_scope(self, *args, out=True):
        env = {
            "PATH": str(self.fake_dir) + os.pathsep + "/usr/bin:/bin",
            "FAKE_GH_DIR": str(self.fake_dir),
            "GH_TOKEN": FAKE_TOKEN,
            "GITHUB_TOKEN": FAKE_TOKEN,
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.tmp),
        }
        argv = [sys.executable, str(SCRIPT), *args]
        if out:
            argv += ["--out-dir", str(self.out)]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        path = self.fake_dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def manifest(self):
        return json.loads((self.out / "scope-manifest.json").read_text(encoding="utf-8"))

    def summary(self):
        return (self.out / "summary.md").read_text(encoding="utf-8")


class SurveyTests(ScopeTestCase):
    """S2: a fixed run writes the raw responses, the manifest and the summary."""

    def test_writes_manifest_raw_responses_and_summary(self):
        self.scenario(SCENARIO)
        result = self.run_scope("--org", "eslint", "--since", "2026-07-05")
        self.assertEqual(0, result.returncode, result.stderr)

        manifest = self.manifest()
        self.assertEqual(len(self.calls()), manifest["request_count"])
        self.assertEqual(4, manifest["request_count"])  # repos + label + title + merged PRs
        self.assertEqual("complete", manifest["status"])
        self.assertEqual(20, manifest["max_requests"])
        self.assertEqual(["eslint"], manifest["args"]["orgs"])
        self.assertEqual("2026-07-05", manifest["args"]["since"])
        self.assertEqual("documentation", manifest["args"]["label"])
        self.assertRegex(manifest["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(manifest["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(4, len(manifest["requests"]))
        for entry in manifest["requests"]:
            raw = self.out / entry["file"]
            self.assertTrue(raw.is_file(), entry["file"])
            json.loads(raw.read_text(encoding="utf-8"))

    def test_summary_rows_have_the_registry_columns_and_the_counts(self):
        self.scenario(SCENARIO)
        self.assertEqual(0, self.run_scope("--org", "eslint", "--since", "2026-07-05").returncode)
        summary = self.summary()

        self.assertIn("| 저장소 | 조직 | 역할 | 선별 근거 | 마지막 조사 | 증거 문서 | 제외·보류 사유 | 메모 |", summary)
        rows = {line.split("|")[1].strip(): line for line in summary.splitlines() if line.startswith("| eslint")}
        self.assertIn("열린 `documentation` 이슈 10", rows["eslint/eslint"])
        self.assertIn("열린 제목 `docs` 이슈 4", rows["eslint/eslint"])
        self.assertIn("2026-07-05 이후 병합 `docs` PR 26", rows["eslint/eslint"])
        self.assertEqual(8, len(rows["eslint/eslint"].strip().strip("|").split("|")))
        self.assertIn("열린 `documentation` 이슈 3", rows["eslint/markdown"])
        # the more active repository comes first
        self.assertLess(summary.index("| eslint/eslint |"), summary.index("| eslint/markdown |"))

    def test_archived_and_fork_repositories_never_reach_the_table(self):
        self.scenario(SCENARIO)
        self.assertEqual(0, self.run_scope("--org", "eslint", "--since", "2026-07-05").returncode)
        summary = self.summary()
        self.assertNotIn("archive-website", summary)
        self.assertNotIn("forked-thing", summary)
        # a repository with no hit is counted, not listed
        self.assertNotIn("eslint/quiet", summary)

    def test_label_option_and_several_orgs(self):
        data = json.loads(json.dumps(SCENARIO))
        data["search"]["label"]["eslint-community"] = ["eslint-community/eslint-doc-generator"] * 2
        self.scenario(data)
        result = self.run_scope("--org", "eslint", "--org", "eslint-community", "--since", "2026-07-05",
                                "--label", "good first issue")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(8, self.manifest()["request_count"])
        summary = self.summary()
        self.assertIn("열린 `good first issue` 이슈 2", summary)
        self.assertIn("| eslint-community/eslint-doc-generator | eslint-community |", summary)
        queries = [parse_qs(urlsplit(arg).query).get("q", [""])[0] for call in self.calls() for arg in call]
        self.assertTrue(any('label:"good first issue"' in q for q in queries), queries)

    def test_every_call_is_a_get_through_gh_api(self):
        self.scenario(SCENARIO)
        self.assertEqual(0, self.run_scope("--org", "eslint", "--since", "2026-07-05").returncode)
        for call in self.calls():
            self.assertEqual(["api", "--method", "GET"], call[:3], call)
            for flag in ("-f", "-F", "-X", "--field", "--raw-field", "--input", "POST", "PUT", "PATCH", "DELETE"):
                self.assertNotIn(flag, call)

    def test_follows_a_full_page_of_repositories(self):
        data = json.loads(json.dumps(SCENARIO))
        data["repos"]["eslint"] = [repo("eslint/r%d" % i) for i in range(100)] + [repo("eslint/eslint")]
        self.scenario(data)
        self.assertEqual(0, self.run_scope("--org", "eslint", "--since", "2026-07-05").returncode)
        self.assertEqual(5, self.manifest()["request_count"])
        self.assertIn("| eslint/eslint |", self.summary())

    def test_no_token_value_is_written_or_printed(self):
        self.scenario(SCENARIO)
        result = self.run_scope("--org", "eslint", "--since", "2026-07-05")
        self.assertEqual(0, result.returncode)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        for path in self.out.iterdir():
            self.assertNotIn(FAKE_TOKEN, path.read_text(encoding="utf-8"), path.name)


class LimitTests(ScopeTestCase):
    """S3: the request cap stops the run and keeps what was fetched."""

    def test_stops_at_the_request_cap_and_keeps_the_responses(self):
        self.scenario({"repos_endless": True, "repos": {}, "search": {}})
        result = self.run_scope("--org", "eslint", "--since", "2026-07-05")
        self.assertEqual(1, result.returncode)
        self.assertEqual(20, len(self.calls()))
        manifest = self.manifest()
        self.assertEqual("aborted", manifest["status"])
        self.assertEqual("request-limit", manifest["reason"])
        self.assertEqual(20, manifest["request_count"])
        self.assertEqual(20, len(manifest["requests"]))
        for entry in manifest["requests"]:
            self.assertTrue((self.out / entry["file"]).is_file(), entry["file"])
        self.assertFalse((self.out / "summary.md").exists())

    def test_a_failing_gh_aborts_with_the_request_recorded(self):
        self.scenario({"fail": True})
        result = self.run_scope("--org", "eslint", "--since", "2026-07-05")
        self.assertEqual(1, result.returncode)
        manifest = self.manifest()
        self.assertEqual("aborted", manifest["status"])
        self.assertEqual("gh-failed", manifest["reason"])
        self.assertEqual(1, manifest["request_count"])
        self.assertEqual(1, len(self.calls()))


class ArgumentTests(ScopeTestCase):
    def assert_rejected(self, *args, out=True):
        self.scenario(SCENARIO)
        result = self.run_scope(*args, out=out)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual([], self.calls())

    def test_rejects_unknown_and_missing_arguments_before_any_call(self):
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05", "--token", "x")
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05", "--max-requests", "99")
        self.assert_rejected("--since", "2026-07-05")
        self.assert_rejected("--org", "eslint")
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05", out=False)

    def test_rejects_malformed_values(self):
        self.assert_rejected("--org", "es lint", "--since", "2026-07-05")
        self.assert_rejected("--org", "eslint/eslint", "--since", "2026-07-05")
        self.assert_rejected("--org", "eslint", "--since", "2026-13-40")
        self.assert_rejected("--org", "eslint", "--since", "last week")
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05", "--label", "")
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05", "--label", 'a" org:other')

    def test_refuses_a_directory_that_already_has_files(self):
        self.out.mkdir()
        (self.out / "earlier.json").write_text("{}", encoding="utf-8")
        self.assert_rejected("--org", "eslint", "--since", "2026-07-05")
        self.assertEqual({"earlier.json"}, {p.name for p in self.out.iterdir()})

    def test_help_says_this_is_not_an_isolated_entry(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                                env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertEqual(0, result.returncode)
        text = " ".join(result.stdout.split())
        self.assertIn("host", text)
        self.assertIn("not an isolated entry", text)
        self.assertIn("read-only", text)


class SourceTests(unittest.TestCase):
    """S4: the script has no write path to GitHub, and no token handling."""

    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_no_write_command_or_write_method(self):
        forbidden = (
            r"\bgh\s+(issue|pr|release|repo|workflow|api\s+-X)\b",
            r"--method\s*[\"',]*\s*(POST|PUT|PATCH|DELETE)",
            r"[\"'](POST|PUT|PATCH|DELETE)[\"']",
            r"[\"']-[fFX][\"']",
            r"[\"']--(field|raw-field|input)[\"']",
            r"[\"'](issue|pr)[\"']\s*,\s*[\"'](create|comment|edit|close|merge|review)",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, self.source), pattern)

    def test_only_get_is_ever_passed_to_gh(self):
        self.assertEqual(1, len(re.findall(r"\"--method\"", self.source)))
        self.assertIn('"--method", "GET"', self.source)
        self.assertEqual(1, len(re.findall(r"subprocess\.run\(", self.source)))

    def test_no_token_is_read_or_asked_for(self):
        for needle in ("GH_TOKEN", "GITHUB_TOKEN", "auth token", "auth status", "Authorization"):
            self.assertNotIn(needle, self.source)


if __name__ == "__main__":
    unittest.main()
