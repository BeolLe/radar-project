"""Backfill contracts; DB proof uses only a fresh disposable radar_backfill_test."""
import copy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from radar import __main__ as pipeline


def snapshot(day, values, location="MS"):
    return {"kind": "daily", "date": day, "location": location, "sources": [{
        "id": f"fixture-{day}-{location}", "expected_rows": len(values),
        "rows": [{"domain": name + ".example", "rank": rank} for name, rank in values.items()],
    }]}


class BackfillTests(unittest.TestCase):
    def test_explicit_cli_and_no_data_failure(self):
        payload = snapshot("2026-09-28", {"a": 98, "b": 98})
        with patch("radar.cloudflare.collect_daily", return_value=[payload]) as fetch, patch(
            "radar.__main__.ingest", return_value={"status": "published", "signals_rebuilt_snapshots": 4}
        ) as ingest, patch("sys.stdout", new_callable=StringIO) as output, patch(
            "sys.argv", ["radar", "backfill-daily", "--location", "MS", "--date", "2026-09-28"]
        ):
            pipeline.main()
            fetch.assert_called_once_with(["MS"], "2026-09-28")
            ingest.assert_called_once_with(payload, backfill=True)
            self.assertEqual(json.loads(output.getvalue())["rows"], 2)
            fetch.return_value = []
            ingest.reset_mock()
            with self.assertRaisesRegex(ValueError, "nothing was repaired"):
                pipeline.main()
            ingest.assert_not_called()

    def test_backfill_requires_exact_target_and_rejects_weekly(self):
        for args in ([], ["--date", "2026-09-28"], ["--location", "MS"]):
            with patch("sys.argv", ["radar", "backfill-daily", *args]), patch(
                "sys.stderr", new_callable=StringIO
            ), patch("radar.cloudflare.collect_daily") as fetch, self.assertRaises(SystemExit):
                pipeline.main()
            fetch.assert_not_called()
        with patch("radar.__main__.archive") as archive, self.assertRaisesRegex(ValueError, "only for daily"):
            pipeline.ingest(next(pipeline.demo_snapshots()), backfill=True)
        archive.assert_not_called()


@unittest.skipUnless(os.environ.get("RADAR_BACKFILL_TEST_DATABASE_URL"), "No disposable backfill PostgreSQL configured")
class BackfillPostgresTests(unittest.TestCase):
    def test_atomic_rebuild_preservation_and_replay(self):
        url = os.environ["RADAR_BACKFILL_TEST_DATABASE_URL"]
        self.assertEqual(urlsplit(url).path, "/radar_backfill_test", "Refusing a non-test database")
        with tempfile.TemporaryDirectory(prefix="radar-backfill-") as directory, patch.dict(
            os.environ, {"DATABASE_URL": url, "RADAR_DATA_DIR": directory}
        ):
            with pipeline.connect() as conn:
                self.assertIsNone(conn.execute("SELECT to_regclass('core.snapshot')").fetchone()[0],
                                  "Use a fresh database; this test never deletes schemas")
                conn.execute((pipeline.ROOT / "schema.sql").read_text())

            def dump():
                with pipeline.connect() as conn:
                    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                            for table in ("core.snapshot", "core.domain", "core.observation",
                                          "core.tag_result", "mart.domain_signal")}

            def signals(location):
                with pipeline.connect() as conn:
                    return set(conn.execute("SELECT s.period_date::text,d.name,m.signal "
                        "FROM mart.domain_signal m JOIN core.snapshot s ON s.id=m.snapshot_id "
                        "JOIN core.domain d ON d.id=m.domain_id WHERE s.kind='daily' AND s.location=%s",
                        (location,)).fetchall())

            values = [
                ("2026-09-27", {"a": 90}),
                ("2026-09-28", {"a": 80, "hidden": 60, "backfill-only": 50}),
                ("2026-09-29", {"a": 70, "bridge": 40}),
                ("2026-09-30", {"a": 60, "hidden": 50, "bridge": 30}),
                # A separate missing day must not become a false daily comparison.
                ("2026-10-02", {"a": 50, "hidden": 40}),
            ]
            for day, ranks in values:
                if day != "2026-09-28":
                    pipeline.ingest(snapshot(day, ranks))
                pipeline.ingest(snapshot(day, {"a": 10}, "KR"))
            pipeline.ingest(next(pipeline.demo_snapshots()))  # Weekly stream stays unchanged.
            with pipeline.connect() as conn:
                domain_id = conn.execute("SELECT id FROM core.domain WHERE name='a.example'").fetchone()[0]
            pipeline.import_tags(pipeline.make_request("preliminary", [{
                "domain_id": domain_id, "domain": "a.example", "url": "https://a.example"
            }]), {"phase": "preliminary", "checked_at": "2026-10-02T00:00:00Z", "results": [{
                "domain_id": domain_id, "status": "classified", "tags": [{"code": "topic.it", "confidence": .8}]
            }]})
            missing = snapshot(*values[1])
            before = dump()
            raw_before = {path: path.read_bytes() for path in (Path(directory) / "raw").glob("*.parquet")}
            self.assertIn(("2026-09-30", "hidden.example", "first_seen"), signals("MS"))
            with self.assertRaisesRegex(ValueError, "chronologically"):
                pipeline.ingest(missing)
            self.assertEqual(dump(), before)

            real_build = pipeline.build_signals
            calls = []

            def fail_after_one(conn, current, previous, meta):
                calls.append(current)
                real_build(conn, current, previous, meta)
                if len(calls) == 2:
                    raise RuntimeError("injected rebuild failure")

            with patch("radar.__main__.build_signals", side_effect=fail_after_one), self.assertRaisesRegex(
                RuntimeError, "injected"
            ):
                pipeline.ingest(missing, backfill=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(dump(), before)  # Includes old signals and all core/tag data.
            with pipeline.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM stage.observation WHERE batch_hash=%s",
                                              (pipeline.digest(missing),)).fetchone()[0], 3)
            self.assertTrue((Path(directory) / "raw" / (pipeline.digest(missing) + ".parquet")).exists())

            result = pipeline.ingest(missing, backfill=True)
            self.assertEqual(result["signals_rebuilt_snapshots"], 4)
            after = dump()
            self.assertTrue(all(row in after["core.snapshot"] for row in before["core.snapshot"]))
            self.assertTrue(all(row in after["core.domain"] for row in before["core.domain"]))
            self.assertTrue(all(row in after["core.observation"] for row in before["core.observation"]))
            self.assertEqual(after["core.tag_result"], before["core.tag_result"])
            self.assertTrue(all(path.read_bytes() == content for path, content in raw_before.items()))
            target_ids = {row[0] for row in after["core.snapshot"]
                          if row[1] == "daily" and row[3] == "MS" and str(row[2]) >= "2026-09-28"}
            self.assertEqual([r for r in before["mart.domain_signal"] if r[0] not in target_ids],
                             [r for r in after["mart.domain_signal"] if r[0] not in target_ids])
            self.assertIn(("2026-09-30", "hidden.example", "reentry"), signals("MS"))
            self.assertNotIn(("2026-09-30", "hidden.example", "first_seen"), signals("MS"))
            self.assertIn(("2026-09-29", "a.example", "consecutive_improvement"), signals("MS"))
            self.assertIn(("2026-09-30", "a.example", "consecutive_improvement"), signals("MS"))
            self.assertFalse(any(row[0] == "2026-10-02" for row in signals("MS")))
            self.assertEqual(pipeline.ingest(missing, backfill=True)["status"], "already_published")
            self.assertEqual(pipeline.ingest(missing)["status"], "already_published")
            self.assertEqual(dump(), after)
            changed = copy.deepcopy(missing)
            changed["sources"][0]["rows"][0]["rank"] = 79
            with self.assertRaisesRegex(ValueError, "Revision detected"):
                pipeline.ingest(changed, backfill=True)
            self.assertEqual(dump(), after)
            with pipeline.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM stage.observation").fetchone()[0], 0)
            # Reference: same history loaded chronologically must yield identical signals.
            for day, ranks in values:
                pipeline.ingest(snapshot(day, ranks, "ZZ"))
            self.assertEqual(signals("MS"), signals("ZZ"))


if __name__ == "__main__":
    unittest.main()
