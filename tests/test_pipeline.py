import copy
import os
import tempfile
import unittest
from unittest.mock import patch
from radar.__main__ import archive, demo_snapshots, digest, read_archive
from radar.domain import domain_name, validate_snapshot
from radar.tagging import TAXONOMY, make_request, validate_results


class PipelineTests(unittest.TestCase):
    def test_cumulative_dedup(self):
        meta, raw, values = validate_snapshot(next(demo_snapshots()))
        self.assertGreater(len(raw), len(values))
        self.assertEqual(values["alpha.example"], 500000)
        self.assertEqual(meta["location"], "WORLD")

    def test_incomplete_and_nonnested_sources(self):
        payload = next(demo_snapshots())
        payload["sources"][0]["expected_rows"] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(payload)
        payload = next(demo_snapshots())
        payload["sources"][0]["rows"] = [{"domain": "missing.example"}]
        with self.assertRaises(ValueError):
            validate_snapshot(payload)

    def test_global_vs_country(self):
        payload = next(demo_snapshots())
        payload["location"] = "KR"
        with self.assertRaises(ValueError):
            validate_snapshot(payload)

    def test_daily_rank_contract(self):
        payload = list(demo_snapshots())[-1]
        validate_snapshot(payload)
        payload["sources"][0]["rows"][1]["rank"] = 1
        with self.assertRaises(ValueError):
            validate_snapshot(payload)

    def test_domain_boundary(self):
        self.assertEqual(domain_name("WWW.Example.COM."), "www.example.com")
        self.assertNotEqual(domain_name("www.example.com"), domain_name("example.com"))
        for value in ("https://example.com", "localhost", "127.0.0.1", "bad name.example", "a..example"):
            with self.assertRaises(ValueError):
                domain_name(value)

    def test_parquet_roundtrip(self):
        payload = next(demo_snapshots())
        payload["sources"][0]["rows"][0]["categories"] = [{"name": "Example category"}]
        with tempfile.TemporaryDirectory(prefix="radar-unit-") as folder, patch.dict(
            os.environ, {"RADAR_DATA_DIR": folder}
        ):
            raw = archive(payload, digest(payload))
            self.assertEqual(read_archive(raw, digest(payload)), payload)
            self.assertEqual(archive(payload, digest(payload)), raw)
            with self.assertRaises(ValueError):
                read_archive(raw, "wrong-hash")

    def test_batch_limits_and_taxonomy(self):
        self.assertEqual(len(TAXONOMY["tags"]), 44)
        for phase, limit in (("preliminary", 100), ("detail", 20)):
            domains = [{"domain_id": i, "domain": f"d{i}.example"} for i in range(limit)]
            make_request(phase, domains)
            with self.assertRaises(ValueError):
                make_request(phase, domains + [{"domain_id": 999, "domain": "extra.example"}])

    def test_detail_requires_tool_evidence(self):
        request = make_request("detail", [{"domain_id": 1, "domain": "alpha.example"}])
        response = {"phase": "detail", "checked_at": "2026-09-27T00:00:00+00:00",
                    "results": [{"domain_id": 1, "status": "classified", "reason": "Adapter evidence",
                                 "tags": [{"code": "topic.it", "confidence": 0.8}]}]}
        with self.assertRaises(ValueError):
            validate_results(request, response)
        response["tool_evidence"] = {"1": {"url": "https://alpha.example", "status": "success"}}
        self.assertEqual(len(validate_results(request, response)), 1)
        for confidence in (True, float("nan"), 1.1):
            invalid = copy.deepcopy(response)
            invalid["results"][0]["tags"][0]["confidence"] = confidence
            with self.assertRaises(ValueError):
                validate_results(request, invalid)


if __name__ == "__main__":
    unittest.main()
