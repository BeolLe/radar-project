"""Preserve conflicting Gemini fields without publishing them or repeating API calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from radar.__main__ import import_tags
from radar.gemini import normalize, tag_batch, tag_pending
from radar.tagging import DomainIdMismatch, make_request, validate_results


class TagReviewTests(unittest.TestCase):
    def setUp(self):
        self.request = make_request("preliminary", [
            {"domain_id": i, "domain": f"d{i}.example", "url": f"https://d{i}.example"}
            for i in (1, 2, 3)
        ])
        self.results = [
            {"domain_id": 1, "status": "classified", "tags": [], "reason": "Empty classification"},
            {"domain_id": 2, "status": "unknown", "tags": [{"code": "topic.it", "confidence": .8}],
             "reason": "Contradictory tags"},
            {"domain_id": 3, "status": "classified", "tags": [{"code": "role.api", "confidence": .7}],
             "reason": "Valid API classification"},
        ]
        self.response = {"modelVersion": "fixture-model", "candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": json.dumps({"results": self.results})}]}}]}
        self.body = json.dumps(self.response)
        self.timestamp = "2026-09-28T11:57:00+00:00"

    def test_both_conflicts_preserve_original_and_valid_row(self):
        envelope = normalize(self.request, self.body, self.timestamp)
        original = copy.deepcopy(envelope)
        rows = validate_results(self.request, envelope, require_all=True)
        self.assertEqual(envelope, original)
        for row, result in zip(rows[:2], self.results[:2]):
            self.assertEqual((row["status"], row["tags"]), ("unknown", []))
            self.assertEqual(row["review"], {"code": "status_tags_mismatch", "original_result": result})
        self.assertEqual(rows[2]["status"], "classified")
        self.assertEqual(rows[2]["tags"], self.results[2]["tags"])
        self.assertNotIn("review", rows[2])

    def test_review_flag_cannot_be_injected_by_model(self):
        envelope = normalize(self.request, self.body, self.timestamp)
        envelope["results"][2]["review"] = {"code": "invented"}
        self.assertNotIn("review", validate_results(self.request, envelope)[2])

    def test_id_taxonomy_confidence_and_detail_validation_remain_strict(self):
        envelope = normalize(self.request, self.body, self.timestamp)
        for field, value in (("domain_id", 99), ("tags", [{"code": "invented", "confidence": .8}]),
                             ("tags", [{"code": "topic.it", "confidence": 2}])):
            candidate = copy.deepcopy(envelope)
            candidate["results"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_results(self.request, candidate, require_all=True)
        candidate = copy.deepcopy(envelope)
        candidate["results"][1]["domain_id"] = 1
        with self.assertRaises(DomainIdMismatch):
            validate_results(self.request, candidate, require_all=True)
        candidate = {**envelope, "phase": "detail"}
        with self.assertRaises(ValueError):
            validate_results({**self.request, "phase": "detail"}, candidate)

    def test_import_stores_review_original_and_metadata_separately(self):
        envelope = normalize(self.request, self.body, self.timestamp)
        envelope["raw_path"] = "/data/raw/gemini/saved.parquet"
        with patch("radar.__main__.connect") as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.side_effect = [(d["domain"],) for d in self.request["domains"]] * 2
            report = import_tags(self.request, envelope)
            self.assertEqual(report, {"validated_results": 3, "review_required": 2,
                                      "review_domain_ids": [1, 2], "missing_ids": []})
            import_tags(self.request, envelope)
            inserts = [c.args[1] for c in conn.execute.call_args_list if "INSERT INTO core.tag_result" in c.args[0]]
            self.assertEqual([p[9] for p in inserts[:3]], [p[9] for p in inserts[3:]])
            for params, original in zip(inserts[:2], self.results[:2]):
                self.assertEqual(params[2], "unknown")
                self.assertEqual(params[6].obj, [])
                evidence = params[7].obj
                self.assertEqual(evidence["review"]["original_result"], original)
                self.assertEqual(evidence["raw_path"], envelope["raw_path"])
                self.assertEqual(evidence["api"]["model_version"], "fixture-model")
            self.assertEqual(inserts[2][2], "classified")
            self.assertNotIn("review", inserts[2][7].obj)

    def test_batch_continues_and_saved_conflict_replays_without_api(self):
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "synthetic-only",
        }), patch("radar.__main__.connect") as connect, patch(
            "radar.__main__.prepare_tags", side_effect=[self.request, {"domains": [], "status": "no_candidates"}]
        ), patch("radar.gemini.send", return_value=self.body) as api, patch("radar.gemini.time.sleep"):
            def execute(sql, params=()):
                cursor = MagicMock()
                cursor.fetchone.return_value = (True,) if "pg_try_advisory_lock" in sql else (f"d{params[0]}.example",)
                return cursor
            connect.return_value.__enter__.return_value.execute.side_effect = execute
            report = tag_pending("review", max_requests=2)
            self.assertEqual(report["status"], "no_candidates")
            self.assertEqual(report["completed_batches"], 1)
            raw = Path(folder) / "raw/gemini/review-000.parquet"
            original_bytes = raw.read_bytes()
            self.assertEqual(json.loads(pq.read_table(raw).column("payload")[0].as_py())["response"], self.body)
            replay = tag_batch("review-000")
            self.assertEqual(replay["review_required"], 2)
            self.assertEqual(replay["api_calls"], 0)
            self.assertEqual(raw.read_bytes(), original_bytes)
            api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
