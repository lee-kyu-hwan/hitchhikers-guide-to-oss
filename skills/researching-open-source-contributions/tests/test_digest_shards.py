"""digest --max-bytes: the shard size that keeps one lens prompt under a worker's input limit."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))

import test_merge_collections as merge  # noqa: E402  (installs the offline guards first)

import json  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

fx = merge.fx
wf = merge.wf


class DigestShardCase(unittest.TestCase):
    def collected(self):
        flow = merge.base.Flow(self)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, paths = flow.collect()
        flow.adopt_collection(paths)
        return flow

    def section_bytes(self, flow):
        keymap = fx.read_json(flow.gen("digest", "extraction/keymap.json"))
        return [len((flow.gen("digest", "extraction") / name).read_bytes()) for name in keymap["shards"]]

    def digest(self, flow, *extra, code=0):
        return flow.ok("digest", *extra, code=code)


class DefaultTests(DigestShardCase):
    def test_the_default_keeps_a_lens_prompt_under_60000_bytes_with_room_for_the_instructions(self):
        # Measured on the real 13 pull request corpus: the analyzer's head and tail around a shard
        # were 4,193 bytes (four repositories, several shards); the reserve covers that with headroom.
        self.assertEqual(60000, wf.DIGEST_PROMPT_TARGET_BYTES)
        self.assertEqual(6000, wf.DIGEST_PROMPT_RESERVE_BYTES)
        self.assertEqual(54000, wf.DIGEST_MAX_BYTES)
        self.assertGreater(wf.DIGEST_PROMPT_RESERVE_BYTES, 4193)

    def test_a_digest_without_the_option_uses_the_default_and_records_it(self):
        flow = self.collected()
        self.digest(flow)
        details = flow.state()["stages"]["digest"]["generations"][0]["details"]
        self.assertEqual((wf.DIGEST_MAX_BYTES, 1), (details["max_bytes"], details["shards"]))
        self.assertEqual(sorted(details["prompts"]), details["prompts"])
        self.assertEqual(max(details["prompt_bytes"].values()), details["largest_prompt_bytes"])
        self.assertEqual([], flow.state()["stages"]["digest"]["generations"][0]["reasons"])


class OptionTests(DigestShardCase):
    def test_a_smaller_shard_size_splits_the_digest_and_every_lens_gets_one_prompt_per_shard(self):
        flow = self.collected()
        # The fixture digest is about 1,700 bytes in two sections of about 870 each.
        self.digest(flow, "--max-bytes", "1200")
        generation = flow.state()["stages"]["digest"]["generations"][-1]
        self.assertEqual(2, generation["details"]["shards"])
        self.assertEqual(1200, generation["details"]["max_bytes"])
        self.assertEqual(10, len(generation["details"]["prompts"]))
        self.assertIn("extract-blind-01.md", generation["details"]["prompts"])
        self.assertIn("extract-structure-02.md", generation["details"]["prompts"])
        self.assertTrue(all(size <= 1200 for size in self.section_bytes(flow)))

    def test_the_shard_size_is_part_of_the_input_so_another_size_is_not_a_reuse(self):
        flow = self.collected()
        self.digest(flow)
        self.digest(flow)
        self.assertEqual(1, len(flow.state()["stages"]["digest"]["generations"]))
        self.assertEqual("reused", flow.state()["events"][-1]["result"])
        self.digest(flow, "--max-bytes", "54000")
        self.assertEqual(1, len(flow.state()["stages"]["digest"]["generations"]))
        self.digest(flow, "--max-bytes", "40000")
        self.assertEqual(2, len(flow.state()["stages"]["digest"]["generations"]))
        inputs = flow.state()["stages"]["digest"]["generations"][-1]["inputs"]
        self.assertEqual({"corpus", "max_bytes"}, set(inputs))

    def test_a_section_larger_than_the_shard_size_is_refused_and_nothing_changes(self):
        flow = self.collected()
        result = self.digest(flow, "--max-bytes", "10", code=2)
        self.assertIn("build_digest failed", result[2])
        self.assertIsNone(flow.state()["stages"]["digest"]["active"])
        self.assertEqual([], flow.state()["stages"]["digest"]["generations"])

    def test_a_shard_size_that_is_not_a_positive_integer_is_refused(self):
        flow = self.collected()
        for bad in ("0", "-5", "abc", "1.5"):
            with self.subTest(bad):
                self.assertEqual(2, flow.cli("digest", "--max-bytes", bad)[0])

    def test_a_lens_prompt_over_the_target_is_recorded_not_hidden(self):
        flow = self.collected()
        with mock.patch.object(wf, "DIGEST_PROMPT_TARGET_BYTES", 100):
            self.digest(flow)
        generation = flow.state()["stages"]["digest"]["generations"][0]
        self.assertEqual("complete", generation["status"])
        self.assertTrue(generation["reasons"][0].startswith("largest lens prompt"), generation["reasons"])
        self.assertIn("100", generation["reasons"][0])


class RealisticShapeTests(DigestShardCase):
    def test_a_corpus_of_windows_and_outcomes_digests_within_the_target(self):
        request = fx.base_request(source__outcome=["merged", "closed-unmerged"])
        flow = merge.base.Flow(self, request)
        flow.ok("init", "--request", str(flow.request_path))
        _, _, base = flow.collect()
        start, mid, end = "2026-09-01T00:00:00Z", "2026-09-04T00:00:00Z", "2026-09-08T00:00:00Z"
        bundles = [
            fx.derive_window(base, flow.producer / "w1", "w1", interval=(start, mid), outcome="merged", keep=[0, 1]),
            fx.derive_window(base, flow.producer / "w2", "w2", interval=(mid, end), outcome="merged", keep=[0, 1], shift=100),
            fx.derive_window(base, flow.producer / "c", "c", interval=(start, end), outcome="closed-unmerged",
                             keep=[0, 1], shift=200),
        ]
        flow.ok("adopt-collection", *sum([["--corpus", str(b["corpus"]), "--manifest", str(b["manifest"])]
                                          for b in bundles], []))
        self.digest(flow)
        details = flow.state()["stages"]["digest"]["generations"][0]["details"]
        self.assertLessEqual(details["largest_prompt_bytes"], wf.DIGEST_PROMPT_TARGET_BYTES)
        self.assertEqual(6, flow.state()["stages"]["collect"]["generations"][0]["details"]["records"])
        self.digest(flow, "--max-bytes", "1200")
        self.assertGreater(flow.state()["stages"]["digest"]["generations"][-1]["details"]["shards"], 2)


if __name__ == "__main__":
    unittest.main()
