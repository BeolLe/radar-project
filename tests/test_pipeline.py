import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from radar.__main__ import archive, demo_snapshots, digest, read_archive
from radar.domain import FULL_BUCKETS, domain_name, validate_snapshot
from radar.tagging import TAXONOMY, make_request, validate_results


class PipelineTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RADAR_SCALE_TEST") == "1", "Opt-in million-domain raw/validation check")
    def test_million_domain_raw_validation(self):
        import resource
        import sys
        import time
        started = time.monotonic()
        payload = {"kind": "weekly", "date": "2026-01-19", "location": "WORLD", "sources": [
            {"id": f"scale-{bucket}", "bucket": bucket, "expected_rows": bucket,
             "rows": [{"domain": f"d{i}.example"} for i in range(bucket)]}
            for bucket in FULL_BUCKETS]}
        validate_snapshot(payload)
        batch = digest(payload)
        with tempfile.TemporaryDirectory(prefix="radar-scale-") as folder, patch.dict(
            os.environ, {"RADAR_DATA_DIR": folder}
        ):
            raw = archive(payload, batch)
            _, rows, values = validate_snapshot(read_archive(raw, batch))
            self.assertEqual(len(rows), 1_888_700)
            self.assertEqual(len(values), 1_000_000)
            self.assertEqual(values["d0.example"], 200)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            print(json.dumps({"synthetic_only": True, "seconds": round(time.monotonic() - started, 2),
                              "raw_bytes": raw.stat().st_size,
                              "max_rss_mib": round(rss / (1024 ** 2 if sys.platform == "darwin" else 1024), 1)}))

    def test_hash_compatibility_and_old_parquet_reader(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from pathlib import Path
        payload = next(demo_snapshots())
        payload["description"] = "한글 테스트"
        old_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                            allow_nan=False).encode()).hexdigest()
        self.assertEqual(digest(payload), old_hash)
        metadata = {**payload, "sources": [{k: v for k, v in s.items() if k != "rows"}
                                           for s in payload["sources"]]}
        rows = [{"source_id": source["id"], "bucket": source.get("bucket"),
                 "payload": json.dumps(row, ensure_ascii=False)}
                for source in payload["sources"] for row in source["rows"]]
        table = pa.Table.from_pylist(rows).replace_schema_metadata({
            b"batch_hash": old_hash.encode(), b"source_metadata": json.dumps(metadata).encode()})
        with tempfile.TemporaryDirectory(prefix="radar-legacy-") as folder:
            path = Path(folder) / "old.parquet"
            pq.write_table(table, path, compression="zstd")
            self.assertEqual(read_archive(path, old_hash), payload)

    def test_chunked_archive_preserves_order_across_batches(self):
        payload = {"kind": "weekly", "date": "2026-01-19", "location": "WORLD", "sources": [{
            "id": "large-fixture", "bucket": 20000, "expected_rows": 10001,
            "rows": [{"domain": f"d{i}.example"} for i in range(10001)]}]}
        with tempfile.TemporaryDirectory(prefix="radar-chunks-") as folder, patch.dict(
            os.environ, {"RADAR_DATA_DIR": folder}
        ):
            path = archive(payload, digest(payload))
            self.assertEqual(read_archive(path, digest(payload)), payload)

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
