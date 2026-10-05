"""An analysis made under an earlier analyzer revision, adopted under that recorded revision (#266, defect 3).

The analyzer's revision is a hash over its skill files, so a prompt-only change
moves it and the current validator refuses every earlier analysis. Re-adoption
needs the earlier analyzer's own skill files (``--analyzer-source``): their
revision must be the one the analysis records, that revision's own validator
must accept the analysis, and so must the current validator's rules with the
recorded generator. The drift is shown, never hidden. The earlier analyzers
below are copies of the current one with one changed byte.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_oss_workflow as base  # noqa: E402  (installs the offline guards first)

import importlib.util  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402

fx = base.fx
wf = base.wf
ANALYZER = "analyzing-open-source-pr-patterns"


def earlier_analyzer(flow, name="earlier", validator_edit=None):
    """A copy of the analyzer skill with another revision; ``validator_edit`` rewrites its validate_corpus.py."""
    directory = flow.tmp / name / ANALYZER
    shutil.copytree(fx.SKILLS_ROOT / ANALYZER, directory, ignore=shutil.ignore_patterns("__pycache__"))
    skill = directory / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n<!-- an earlier prompt wording -->\n", encoding="utf-8")
    if validator_edit is not None:
        script = directory / "scripts" / "validate_corpus.py"
        script.write_text(validator_edit(script.read_text(encoding="utf-8")), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("earlier_validate_corpus_" + name, directory / "scripts" / "validate_corpus.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return directory, module._canonical_revision()


def analysis_under(flow, revision, name="earlier-analysis.json", **kwargs):
    """The synthetic analysis as that analyzer revision would have written it: every revision field names it."""
    document = fx.synthetic_analysis(fx.read_json(flow.gen("collect", "corpus.json")), **kwargs)
    text = json.dumps(document, ensure_ascii=False).replace(fx.validate_corpus._canonical_revision(), revision)
    path = flow.producer / name
    path.write_text(text, encoding="utf-8")
    return path


class AnalysisReadoptTests(unittest.TestCase):
    def _through_digest(self):
        flow = base.Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        flow.ok("digest")
        return flow

    def _adopt(self, flow, analysis, *extra, code=0):
        return flow.ok("adopt-analysis", "--analysis", str(analysis), "--origin", "synthetic-fixture", *extra, code=code)

    def _unchanged(self, flow, before):
        after = flow.state()
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual("rejected", after["events"][-1]["result"])

    def test_s4_an_earlier_revision_analysis_is_adopted_under_its_recorded_revision_and_the_drift_is_shown(self):
        flow = self._through_digest()
        source, revision = earlier_analyzer(flow)
        current = fx.validate_corpus._canonical_revision()
        self.assertNotEqual(current, revision)
        analysis = analysis_under(flow, revision)
        self._adopt(flow, analysis, "--analyzer-source", str(source))
        generation = wf.active(flow.state(), "analysis")
        self.assertEqual("complete", generation["status"])
        drift = {"recorded": revision, "current": current, "source": str(source.resolve())}
        self.assertEqual(drift, generation["details"]["analyzer_revision_drift"])
        self.assertEqual(str(source.resolve()), generation["inputs"]["analyzer_source"]["source_path"])
        status = flow.status()
        self.assertEqual(drift, status["stages"]["analysis"]["analyzer_revision_drift"])
        self.assertIn("analyzer revision drift: adopted under recorded %s, current %s" % (revision, current),
                      status["stages"]["analysis"]["reasons"])
        # Later stages are not blocked: discovery, locus search and record run as for a current analysis.
        discovery, manifest = flow.discover()
        flow.ok("adopt-discovery", "--discovery", str(discovery), "--manifest", str(manifest))
        assessment = flow.assessment()
        locus, locus_manifest = flow.locus_search(assessment)
        flow.ok("adopt-locus-search", "--locus-search", str(locus), "--manifest", str(locus_manifest))
        flow.ok("record", "--assessment", str(assessment), "--origin", "synthetic-fixture")
        status = flow.status()
        self.assertEqual(("insufficient", "complete", []), (status["outcome"], status["completeness"], status["next"]))
        candidates = fx.read_json(flow.gen("record", "candidates.json"))
        self.assertEqual({fx.PATTERN_ID: revision}, candidates["records"][0]["pattern_revisions"])
        flow.ok("report")
        report = (flow.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("Analyzer revision drift: the analysis was adopted under its recorded analyzer revision `%s`; "
                      "the current analyzer is `%s`" % (revision, current), report)

    def test_the_same_drifted_adoption_again_is_a_revalidated_reuse(self):
        flow = self._through_digest()
        source, revision = earlier_analyzer(flow)
        analysis = analysis_under(flow, revision)
        self._adopt(flow, analysis, "--analyzer-source", str(source))
        self._adopt(flow, analysis, "--analyzer-source", str(source))
        self.assertEqual(1, len(flow.state()["stages"]["analysis"]["generations"]))
        self.assertEqual(("reused", "gen-001 revalidated under the recorded analyzer revision"),
                         (flow.state()["events"][-1]["result"], flow.state()["events"][-1]["detail"]))
        # Without the source the same bytes are another input: --replace is needed, and then the source again.
        self.assertIn("pass --replace", self._adopt(flow, analysis, code=2)[2])
        self.assertIn("--analyzer-source", self._adopt(flow, analysis, "--replace", code=2)[2])
        self.assertEqual(1, len(flow.state()["stages"]["analysis"]["generations"]))

    def test_a_drifted_analysis_without_its_analyzer_source_is_refused_with_both_paths_named(self):
        flow = self._through_digest()
        _, revision = earlier_analyzer(flow)
        analysis = analysis_under(flow, revision)
        before = flow.state()
        result = self._adopt(flow, analysis, code=2)
        self.assertIn("must equal the current analysis generator", result[2])
        self.assertIn("the analysis records analyzer revision %s, not the current %s" % (revision, fx.validate_corpus._canonical_revision()),
                      result[2])
        self.assertIn("--analyzer-source", result[2])
        self.assertIn("or regenerate the analysis", result[2])
        self._unchanged(flow, before)

    def test_an_analyzer_source_of_another_revision_is_refused(self):
        flow = self._through_digest()
        _, revision = earlier_analyzer(flow)
        other, other_revision = earlier_analyzer(flow, "other", validator_edit=lambda text: text + "\n# another revision\n")
        analysis = analysis_under(flow, revision)
        before = flow.state()
        result = self._adopt(flow, analysis, "--analyzer-source", str(other), code=2)
        self.assertIn("--analyzer-source is analyzer revision %s, not the recorded %s" % (other_revision, revision), result[2])
        result = self._adopt(flow, analysis, "--analyzer-source", str(flow.tmp / "missing"), code=2)
        self.assertIn("has no scripts/validate_corpus.py", result[2])
        self._unchanged(flow, before)

    def test_a_current_revision_analysis_needs_no_analyzer_source(self):
        flow = self._through_digest()
        source, _ = earlier_analyzer(flow)
        before = flow.state()
        result = self._adopt(flow, flow.analysis_file(), "--analyzer-source", str(source), code=2)
        self.assertIn("names the current analyzer revision; adopt it without --analyzer-source", result[2])
        self._unchanged(flow, before)
        self._adopt(flow, flow.analysis_file())
        generation = wf.active(flow.state(), "analysis")
        self.assertNotIn("analyzer_revision_drift", generation["details"])
        self.assertEqual({"analysis", "corpus"}, set(generation["inputs"]))
        self.assertNotIn("analyzer_revision_drift", flow.status()["stages"]["analysis"])

    def test_a_tampered_drifted_analysis_is_still_refused(self):
        flow = self._through_digest()
        source, revision = earlier_analyzer(flow)
        before = flow.state()
        # A quote that is not in the pull request.
        result = self._adopt(flow, analysis_under(flow, revision, quote_override="never said"), "--analyzer-source", str(source), code=2)
        self.assertIn("the recorded analyzer's own validator rejected the analysis", result[2])
        # One record left on the current revision: the revision fields no longer agree.
        path = analysis_under(flow, revision, name="mixed.json")
        document = fx.read_json(path)
        document["records"][0]["analysis_history"][-1]["revision"] = fx.validate_corpus._canonical_revision()
        fx.write_json(path, document)
        result = self._adopt(flow, path, "--analyzer-source", str(source), code=2)
        self.assertIn("analysis_history[-1].revision must equal analysis_generated_by.revision", result[2])
        self._unchanged(flow, before)

    def test_the_current_validator_rules_apply_even_when_the_earlier_validator_accepts(self):
        # An earlier validator that accepted anything: its own check passes, the current rules still refuse.
        flow = self._through_digest()
        lenient = "def _validate_analysis_output(current, output, existing, revision, reference_repositories=()):\n    return []\n\n\ndef _unused_strict("
        source, revision = earlier_analyzer(flow, "lenient", validator_edit=lambda text: text.replace(
            "def _validate_analysis_output(", lenient, 1))
        before = flow.state()
        result = self._adopt(flow, analysis_under(flow, revision, quote_override="never said"), "--analyzer-source", str(source), code=2)
        self.assertIn("the current analyzer rules (under the recorded generator) rejected the analysis", result[2])
        self._unchanged(flow, before)


if __name__ == "__main__":
    unittest.main()
