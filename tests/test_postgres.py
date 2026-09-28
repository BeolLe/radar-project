"""Opt-in integration check. Requires a fresh, disposable database named radar_test."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from radar.__main__ import ROOT, connect, demo_snapshots, import_tags, ingest, prepare_tags


@unittest.skipUnless(os.environ.get("RADAR_TEST_DATABASE_URL"), "No disposable PostgreSQL configured")
class PostgresTests(unittest.TestCase):
    def test_end_to_end_and_idempotency(self):
        url = os.environ["RADAR_TEST_DATABASE_URL"]
        self.assertEqual(urlsplit(url).path, "/radar_test", "Refusing to use a non-test database")
        with tempfile.TemporaryDirectory(prefix="radar-test-") as directory, patch.dict(
            os.environ, {"DATABASE_URL": url, "RADAR_DATA_DIR": directory}
        ):
            with connect() as conn:
                self.assertIsNone(conn.execute("SELECT to_regclass('core.snapshot')").fetchone()[0],
                                  "Use a fresh database; this test never deletes existing schemas")
                conn.execute((ROOT / "schema.sql").read_text())
            snapshots = list(demo_snapshots())
            results = [ingest(p) for p in snapshots]
            for payload in snapshots:
                self.assertEqual(ingest(payload)["status"], "already_published")
            with connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM core.snapshot").fetchone()[0], 5)
                self.assertEqual(conn.execute("SELECT count(*) FROM core.observation").fetchone()[0], 13)
                self.assertEqual(conn.execute("SELECT count(*) FROM stage.observation").fetchone()[0], 0)
                observed = set(conn.execute("""
                  SELECT d.name,m.signal FROM mart.domain_signal m
                  JOIN core.domain d ON d.id=m.domain_id WHERE m.snapshot_id=%s
                """, (results[2]["snapshot_id"],)).fetchall())
                self.assertEqual(observed, {("alpha.example", "improved"),
                                           ("alpha.example", "consecutive_improvement"),
                                           ("return.example", "reentry")})
            changed = copy.deepcopy(snapshots[0])
            changed["sources"][0]["id"] = "revision"
            with self.assertRaises(ValueError):
                ingest(changed)
            invalid = copy.deepcopy(snapshots[0])
            invalid["date"] = "2025-12-29"
            with self.assertRaises(ValueError):
                ingest(invalid)
            self.assertGreaterEqual(len(list((Path(directory) / "raw").glob("*.parquet"))), 5)
            request = prepare_tags("preliminary", 100)
            first = request["domains"][0]
            response = {"phase": "preliminary", "checked_at": "2026-09-27T00:00:00+00:00",
                        "results": [{"domain_id": first["domain_id"], "status": "classified",
                                     "tags": [{"code": "topic.it", "confidence": 0.6}]}]}
            import_tags(request, response)
            import_tags(request, response)
            detail = prepare_tags("detail", 20)
            self.assertIn(first["domain_id"], [d["domain_id"] for d in detail["domains"]])
            response = {"phase": "detail", "checked_at": "2026-09-28T00:00:00+00:00",
                        "results": [{"domain_id": first["domain_id"], "status": "classified",
                                     "reason": "Synthetic adapter evidence",
                                     "tags": [{"code": "topic.news", "confidence": 0.7}]}],
                        "tool_evidence": {str(first["domain_id"]): {
                            "url": first["url"], "status": "success"}}}
            import_tags(detail, response)
            with connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM core.tag_result").fetchone()[0], 2)
                current = conn.execute("SELECT phase,tags FROM mart.current_tag WHERE domain_id=%s",
                                       (first["domain_id"],)).fetchone()
                self.assertEqual(current[0], "detail")
                self.assertEqual(current[1][0]["code"], "topic.news")
            # All provider entries, including single labels, reach core unchanged.
            weekly = copy.deepcopy(snapshots[0])
            weekly["date"] = "2026-01-26"
            names = {"web", "ws", "run.app", "api.example.com"}
            for source in weekly["sources"]:
                source["rows"] = [{"domain": name} for name in sorted(names)]
                source["expected_rows"] = len(names)
            published = ingest(weekly)
            self.assertEqual(ingest(weekly)["status"], "already_published")
            with connect() as conn:
                found = conn.execute("SELECT d.name FROM core.observation o "
                                     "JOIN core.domain d ON d.id=o.domain_id WHERE o.snapshot_id=%s",
                                     (published["snapshot_id"],)).fetchall()
                self.assertEqual({row[0] for row in found}, names)
            # Actual DB + Parquet + mocked API: no network/cost in integration tests.
            from radar.gemini import tag_batch
            for location, name in (("KR", "run.app"), ("JP", "api.example.com")):
                ingest({"kind": "daily", "date": "2026-01-27", "location": location,
                        "sources": [{"id": "priority-" + location, "expected_rows": 1,
                                     "rows": [{"domain": name, "rank": 1}]}]})
            pending = prepare_tags("preliminary", 100)
            ordered = [d["domain"] for d in pending["domains"]]
            self.assertLess(ordered.index("run.app"), ordered.index("api.example.com"))
            self.assertLess(ordered.index("api.example.com"), ordered.index("web"))
            api_response = {"modelVersion": "fixture-version", "candidates": [{
                "finishReason": "STOP", "content": {"parts": [{"text": json.dumps({
                    "results": [{"domain_id": d["domain_id"], "status": "unknown",
                                 "tags": [], "reason": "Synthetic unknown"}
                                for d in pending["domains"]]})}]}}]}
            with patch.dict(os.environ, {"GEMINI_API_KEY": "synthetic-test-only"}), patch(
                "radar.gemini.send", return_value=json.dumps(api_response)
            ) as api:
                tagged = tag_batch("integration", 100)
                self.assertEqual(tag_batch("integration", 100), tagged)
                self.assertEqual(api.call_count, 1)
                self.assertEqual(prepare_tags("preliminary", 100)["status"], "no_candidates")
            with connect() as conn:
                saved = conn.execute("SELECT evidence->'api'->>'model_version' "
                                     "FROM core.tag_result WHERE domain_id=%s AND phase='preliminary'",
                                     (pending["domains"][0]["domain_id"],)).fetchone()
                self.assertEqual(saved[0], "fixture-version")


if __name__ == "__main__":
    unittest.main()
