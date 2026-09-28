import copy
import json
import os
from pathlib import Path
import runpy
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from radar.gemini import GeminiUnavailable, api_payload, normalize, reserve_budget, send, tag_batch, tag_pending
from radar.tagging import DomainIdMismatch, make_request
from radar.__main__ import prepare_tags


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.request = make_request("preliminary", [{"domain_id": 1, "domain": "example.com",
                                                      "url": "https://example.com"}])
        self.result = {"domain_id": 1, "status": "classified", "reason": "알려진 서비스",
                       "tags": [{"code": "topic.it", "confidence": 0.8}]}
        self.response = {"modelVersion": "actual-model-version", "responseId": "response-1",
                         "usageMetadata": {"totalTokenCount": 42}, "candidates": [{
                             "finishReason": "STOP", "content": {"parts": [
                                 {"text": json.dumps({"results": [self.result]})}]}}]}
        self.timestamp = "2026-09-28T05:00:00+00:00"

    def test_structured_request_has_no_retrieval(self):
        payload = api_payload(self.request)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        schema = payload["generationConfig"]["responseJsonSchema"]["properties"]["results"]
        self.assertEqual((schema["minItems"], schema["maxItems"]), (1, 1))
        self.assertEqual(schema["items"]["properties"]["domain_id"], {"type": "integer", "enum": [1]})
        self.assertIn("web or ws", payload["systemInstruction"]["parts"][0]["text"])
        envelope = normalize(self.request, json.dumps(self.response), self.timestamp)
        self.assertEqual(envelope["api_metadata"]["model_version"], "actual-model-version")
        self.assertEqual(envelope["checked_at"], self.timestamp)
        self.assertEqual(envelope["tool_evidence"], {})

    def test_country_candidates_precede_global_without_duplicate_ids(self):
        with patch("radar.__main__.connect") as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchall.side_effect = [[(10, "kr.example"), (20, "jp.example")],
                                                             [(1, "global.example")]]
            request = prepare_tags("preliminary", 3, [99])
            self.assertEqual([d["domain_id"] for d in request["domains"]], [10, 20, 1])
            country_call, global_call = conn.execute.call_args_list
            self.assertIn("s.location='KR' THEN 0 ELSE 1", country_call.args[0])
            self.assertIn("max(period_date)", country_call.args[0])
            self.assertIn("l.period_date=s.period_date", country_call.args[0])
            self.assertIn("FILTER (WHERE s.location='KR')", country_call.args[0])
            self.assertEqual(country_call.args[1], ([99], "preliminary", 3))
            self.assertEqual(global_call.args[1], ([99, 10, 20], "preliminary", 1))

    def test_refuses_incomplete_unknown_id_duplicate_bad_tags(self):
        variants = [[], [self.result, self.result], [{**self.result, "domain_id": 2}],
                    [{**self.result, "tags": [{"code": "invented", "confidence": .5}]}],
                    [{**self.result, "tags": [{"code": "topic.it", "confidence": True}]}],
                    [{**self.result, "reason": ""}]]
        for rows in variants:
            response = copy.deepcopy(self.response)
            response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({"results": rows})
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize(self.request, json.dumps(response), self.timestamp)
        for finish in ("MAX_TOKENS", "SAFETY", None):
            response = copy.deepcopy(self.response)
            response["candidates"][0]["finishReason"] = finish
            with self.assertRaises(ValueError):
                normalize(self.request, json.dumps(response), self.timestamp)

    def test_priority_query_uses_latest_kr_ranks_with_real_rows(self):
        # stdlib relational check; PostgreSQL-specific syntax is also covered by the opt-in test.
        with sqlite3.connect(":memory:") as database, patch("radar.__main__.connect") as connect:
            database.executescript("""
              ATTACH DATABASE ':memory:' AS core;
              CREATE TABLE core.domain(id INTEGER PRIMARY KEY, name TEXT);
              CREATE TABLE core.snapshot(id INTEGER, kind TEXT, location TEXT, period_date TEXT);
              CREATE TABLE core.observation(snapshot_id INTEGER, domain_id INTEGER, value INTEGER);
              CREATE TABLE core.tag_result(domain_id INTEGER, phase TEXT);
              INSERT INTO core.domain VALUES (1,'old-kr.example'),(2,'kr-second.example'),
                (3,'kr-first.example'),(4,'jp.example'),(5,'global.example');
              INSERT INTO core.snapshot VALUES (1,'daily','KR','2026-09-26'),
                (2,'daily','KR','2026-09-27'),(3,'daily','JP','2026-09-27'),
                (4,'weekly','WORLD','2026-09-21');
              INSERT INTO core.observation VALUES (1,1,1),(2,3,1),(2,2,2),
                (3,2,1),(3,4,2),(4,5,200);
            """)
            def execute(sql, params):
                sql = sql.replace("d.id<>ALL(%s::bigint[])",
                                  "d.id NOT IN (SELECT value FROM json_each(%s))").replace("%s", "?")
                return database.execute(sql, [json.dumps(p) if isinstance(p, list) else p for p in params])
            connect.return_value.__enter__.return_value.execute.side_effect = execute
            def ids(excluded=()):
                return [d["domain_id"] for d in prepare_tags("preliminary", 100, excluded)["domains"]]
            self.assertEqual(ids(), [3, 2, 4, 1, 5])
            self.assertEqual(ids([3]), [2, 4, 1, 5])
            database.execute("INSERT INTO core.tag_result VALUES (3,'preliminary')")
            self.assertEqual(ids(), [2, 4, 1, 5])
    def test_unknown_is_completed_without_tags(self):
        self.response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({"results": [
            {"domain_id": 1, "status": "unknown", "tags": [], "reason": "근거 부족"}]})
        rows = normalize(self.request, json.dumps(self.response), self.timestamp)["results"]
        self.assertEqual(rows[0]["status"], "unknown")

    def test_id_errors_are_distinct_and_never_coerced(self):
        for values, field in (([666], "unexpected_ids"), ([1, 1], "duplicate_ids"),
                              ([], "missing_ids"), (["1"], "invalid_types"),
                              ([True], "invalid_types"), ([1.0], "invalid_types"),
                              ([None], "invalid_types")):
            with self.subTest(values=values):
                response = copy.deepcopy(self.response)
                response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
                    "results": [{**self.result, "domain_id": value} for value in values]})
                with self.assertRaises(DomainIdMismatch) as caught:
                    normalize(self.request, json.dumps(response), self.timestamp)
                self.assertTrue(caught.exception.details[field])

    def test_observed_6666_to_666_retries_whole_batch_and_keeps_both_responses(self):
        import pyarrow.parquet as pq
        ids = [*range(1000, 1099), 6666]
        request = make_request("preliminary", [{"domain_id": i, "domain": f"d{i}.example",
                                               "url": f"https://d{i}.example"} for i in ids])
        correct = copy.deepcopy(self.response)
        correct["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": i} for i in ids]})
        wrong = copy.deepcopy(correct)
        wrong["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": i if i != 6666 else 666} for i in ids]})
        wrong_body, correct_body = json.dumps(wrong), json.dumps(correct)
        with self.assertRaises(DomainIdMismatch) as caught:
            normalize(request, wrong_body, self.timestamp)
        self.assertEqual(caught.exception.details, {"invalid_types": [], "duplicate_ids": [],
                                                   "unexpected_ids": [666], "missing_ids": [6666]})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", return_value=request
        ), patch("radar.__main__.import_tags", return_value={"validated_results": 100}) as importer, patch(
            "radar.gemini.send", side_effect=[wrong_body, correct_body]
        ) as api, patch("radar.gemini.time.sleep") as sleep:
            result = tag_batch("observed", 100)
            self.assertEqual(result["api_calls"], 2)
            self.assertEqual(result["validated_results"], 100)
            importer.assert_called_once()
            self.assertEqual([r["domain_id"] for r in importer.call_args.args[1]["results"]], ids)
            self.assertEqual(api.call_args_list[0].args[0], api.call_args_list[1].args[0])
            schema = api.call_args.args[0]["generationConfig"]["responseJsonSchema"]["properties"]["results"]
            self.assertEqual(schema["items"]["properties"]["domain_id"]["enum"], ids)
            self.assertEqual((schema["minItems"], schema["maxItems"]), (100, 100))
            sleep.assert_called_once_with(30)
            raw = json.loads(pq.read_table(Path(result["raw"])).column("payload")[0].as_py())
            self.assertEqual(raw["attempts"][0]["response"], wrong_body)
            self.assertEqual(raw["attempts"][0]["id_errors"], caught.exception.details)
            self.assertEqual(raw["attempts"][1]["response"], correct_body)
            self.assertEqual(raw["response"], correct_body)
            ledger = next((Path(folder) / "raw/gemini/quota").glob("*.parquet"))
            self.assertEqual(len(json.loads(pq.read_table(ledger).column("payload")[0].as_py())["batches"]), 2)
            self.assertEqual(tag_batch("observed", 100)["api_calls"], 0)
            self.assertEqual(api.call_count, 2)

    def test_id_and_503_share_retry_limit_then_skip_to_next_batch(self):
        import pyarrow.parquet as pq
        wrong = copy.deepcopy(self.response)
        wrong["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": 666}]})
        wrong_body = json.dumps(wrong)
        other = make_request("preliminary", [{"domain_id": 2, "domain": "other.example",
                                              "url": "https://other.example"}])
        correct = copy.deepcopy(self.response)
        correct["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": 2}]})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", side_effect=[self.request, other]
        ) as prepare, patch("radar.__main__.import_tags", return_value={"validated_results": 1}) as importer, patch(
            "radar.gemini.send", side_effect=[wrong_body, GeminiUnavailable("503"), wrong_body, json.dumps(correct)]
        ) as api, patch("radar.gemini.time.sleep"):
            result = tag_pending("mixed", max_requests=4, limit=1)
            self.assertEqual(result, {"completed_batches": 1, "skipped_batches": 1,
                                      "api_calls": 4, "status": "run_limit_reached"})
            self.assertEqual(prepare.call_args.args, ("preliminary", 1, [1]))
            importer.assert_called_once()
            self.assertEqual(importer.call_args.args[0], other)
            path = Path(folder) / "raw/gemini/mixed-000.parquet"
            raw = json.loads(pq.read_table(path).column("payload")[0].as_py())
            self.assertEqual(raw["status"], "skipped_id_mismatch")
            self.assertEqual([a["status"] for a in raw["attempts"]], ["id_mismatch", "http_503", "id_mismatch"])
            self.assertEqual(raw["attempts"][0]["response"], wrong_body)
            replay = tag_batch("mixed-000", 1)
            self.assertEqual((replay["status"], replay["api_calls"]), ("skipped_id_mismatch", 0))
            self.assertEqual(replay["domain_ids"], [1])
            self.assertEqual(api.call_count, 4)

    def test_id_failure_respects_daily_budget_and_old_raw_replays_without_mutation(self):
        wrong = copy.deepcopy(self.response)
        wrong["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": 666}]})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", return_value=self.request
        ), patch("radar.__main__.import_tags") as importer, patch(
            "radar.gemini.send", return_value=json.dumps(wrong)
        ) as api, patch("radar.gemini.time.sleep"):
            result = tag_batch("quota", 1, daily_limit=1)
            self.assertEqual(result, {"status": "daily_budget_exhausted", "api_calls": 1})
            # Pre-v0.4.2 archived responses have no retry/status metadata.
            from radar.gemini import save
            path = Path(folder) / "raw/gemini/legacy.parquet"
            save(path, {"request": self.request, "response": json.dumps(wrong), "checked_at": self.timestamp})
            before = path.read_bytes()
            replay = tag_batch("legacy", 1)
            self.assertEqual((replay["status"], replay["api_calls"]), ("skipped_id_mismatch", 0))
            self.assertEqual(replay["id_errors"]["unexpected_ids"], [666])
            self.assertEqual(path.read_bytes(), before)
            importer.assert_not_called()
            self.assertEqual(api.call_count, 1)

    def test_id_retry_timeout_keeps_bad_evidence_without_reusing_it(self):
        import pyarrow.parquet as pq
        wrong = copy.deepcopy(self.response)
        wrong["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
            "results": [{**self.result, "domain_id": 666}]})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", return_value=self.request
        ), patch("radar.__main__.import_tags") as importer, patch(
            "radar.gemini.send", side_effect=[json.dumps(wrong), RuntimeError("timeout")]
        ) as api, patch("radar.gemini.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                tag_batch("uncertain-retry", 1)
            raw = json.loads(pq.read_table(Path(folder) / "raw/gemini/uncertain-retry.parquet").column("payload")[0].as_py())
            self.assertEqual(raw["attempts"][0]["response"], json.dumps(wrong))
            self.assertIsNone(raw["response"])
            self.assertEqual(raw["attempts"][1]["status"], "reserved")
            with self.assertRaisesRegex(RuntimeError, "already reserved"):
                tag_batch("uncertain-retry", 1)
            self.assertEqual(api.call_count, 2)
            importer.assert_not_called()

    def test_http_error_no_retry_or_secret_in_error(self):
        with patch("radar.gemini.urlopen", side_effect=HTTPError("url", 429, "secret", {}, None)) as http:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429") as caught:
                send(api_payload(self.request), "test-secret")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(http.call_count, 1)
            self.assertNotIn("test-secret", http.call_args.args[0].full_url)
        with patch("radar.gemini.urlopen", side_effect=HTTPError("url", 503, "secret", {}, None)):
            with self.assertRaises(GeminiUnavailable) as caught:
                send(api_payload(self.request), "test-secret")
            self.assertNotIn("secret", str(caught.exception))

    def test_503_retries_are_bounded_archived_and_budgeted(self):
        import pyarrow.parquet as pq
        for failures, budget, expected in ((2, 200, "success"), (3, 200, "skipped_503"),
                                            (3, 2, "daily_budget_exhausted")):
            with self.subTest(failures=failures, budget=budget), tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret"}
            ), patch("radar.__main__.connect"), patch(
                "radar.__main__.prepare_tags", return_value=self.request
            ), patch("radar.__main__.import_tags", return_value={"validated_results": 1}) as importer, patch(
                "radar.gemini.send", side_effect=[GeminiUnavailable("503")] * failures +
                [json.dumps(self.response)]
            ) as api, patch("radar.gemini.time.sleep") as sleep:
                result = tag_batch("retry", 1, budget)
                self.assertEqual(result.get("status", "success"), expected)
                self.assertEqual(result["api_calls"], min(3, budget))
                self.assertEqual(api.call_count, min(3, budget))
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60])
                root = Path(folder) / "raw/gemini"
                raw = json.loads(pq.read_table(root / "retry.parquet").column("payload")[0].as_py())
                ledger = json.loads(pq.read_table(next((root / "quota").glob("*.parquet"))).column("payload")[0].as_py())
                self.assertEqual(len(ledger["batches"]), api.call_count)
                self.assertEqual(len(raw["attempts"]), api.call_count)
                self.assertNotIn("test-secret", json.dumps(raw))
                if expected == "success":
                    self.assertEqual(raw["attempts"][-1]["status"], "response_saved")
                    self.assertEqual(tag_batch("retry", 1)["api_calls"], 0)
                else:
                    importer.assert_not_called()
                    self.assertTrue(all(a["status"] == "http_503" for a in raw["attempts"]))
                    if expected == "skipped_503":
                        replay = tag_batch("retry", 1)
                        self.assertEqual(replay["domain_ids"], [1])
                        self.assertEqual(replay["api_calls"], 0)
                self.assertEqual(api.call_count, min(3, budget))

    def test_failed_batch_skips_to_next_without_exceeding_run_budget(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch("radar.__main__.prepare_tags") as prepare, patch(
            "radar.__main__.import_tags", return_value={"validated_results": 1}
        ) as importer, patch("radar.gemini.send") as api, patch("radar.gemini.time.sleep"):
            other = make_request("preliminary", [{"domain_id": 2, "domain": "other.example",
                                                  "url": "https://other.example"}])
            prepare.side_effect = [self.request, other, self.request]
            response = copy.deepcopy(self.response)
            response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
                "results": [{**self.result, "domain_id": 2}]})
            api.side_effect = [GeminiUnavailable("503")] * 3 + [json.dumps(response), GeminiUnavailable("503")]
            result = tag_pending("skip", max_requests=4, limit=1)
            self.assertEqual(result, {"completed_batches": 1, "skipped_batches": 1,
                                      "api_calls": 4, "status": "run_limit_reached"})
            self.assertEqual(prepare.call_args_list[1].args, ("preliminary", 1, [1]))
            self.assertEqual(importer.call_args.args[0], other)
            # New run can revisit failed domains; max_requests=1 also bounds retries.
            result = tag_pending("next-run", max_requests=1, limit=1)
            self.assertEqual(result["api_calls"], 1)
            self.assertEqual(result["skipped_batches"], 1)
            self.assertEqual(prepare.call_args.args, ("preliminary", 1, []))
            self.assertEqual(api.call_count, 5)

    def test_budget_counts_reservations_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            self.assertTrue(reserve_budget(directory, "one", 2))
            with self.assertRaises(RuntimeError):
                reserve_budget(directory, "one", 2)
            self.assertTrue(reserve_budget(directory, "two", 2))
            self.assertFalse(reserve_budget(directory, "three", 2))

    def test_saved_response_replays_after_db_failure_without_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect") as connect, patch(
            "radar.__main__.prepare_tags", return_value=self.request
        ), patch("radar.__main__.import_tags") as importer, patch(
            "radar.gemini.send", return_value=json.dumps(self.response)
        ) as api:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (True,)
            importer.side_effect = RuntimeError("DB unavailable")
            with self.assertRaises(RuntimeError):
                tag_batch("test", 1)
            importer.side_effect = None
            importer.return_value = {"validated_results": 1, "missing_ids": []}
            result = tag_batch("test", 1)
            self.assertEqual(result["validated_results"], 1)
            self.assertEqual(api.call_count, 1)
            self.assertTrue(Path(result["raw"]).exists())
            self.assertEqual(importer.call_args.args[1]["api_metadata"]["usage"]["totalTokenCount"], 42)

    def test_uncertain_request_cannot_be_retried_automatically(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", return_value=self.request
        ), patch("radar.gemini.send", side_effect=RuntimeError("timeout")) as api:
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                tag_batch("uncertain", 1)
            with self.assertRaisesRegex(RuntimeError, "already reserved"):
                tag_batch("uncertain", 1)
            self.assertEqual(api.call_count, 1)

    def test_invalid_raw_retained_without_import(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect"), patch(
            "radar.__main__.prepare_tags", return_value=self.request
        ), patch("radar.gemini.send", return_value="not JSON") as api, patch(
            "radar.__main__.import_tags"
        ) as importer:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    tag_batch("invalid", 1)
            self.assertTrue((Path(folder) / "raw/gemini/invalid.parquet").exists())
            self.assertEqual(api.call_count, 1)
            importer.assert_not_called()

    def test_loop_stops_at_budget_and_paces_calls(self):
        with patch("radar.gemini.tag_batch", side_effect=[{"validated_results": 100},
                  {"status": "daily_budget_exhausted"}]) as batch, patch("radar.gemini.time.sleep") as sleep:
            result = tag_pending("test", 200)
            self.assertEqual(result["completed_batches"], 1)
            self.assertEqual(batch.call_count, 2)
            sleep.assert_called_once_with(60)
        for value in ("../escape", "", "a" * 101):
            with self.assertRaises(ValueError):
                tag_batch(value)

    def test_no_candidates_or_lock_conflict_never_calls_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "RADAR_DATA_DIR": folder, "GEMINI_API_KEY": "test-secret",
        }), patch("radar.__main__.connect") as connect, patch(
            "radar.__main__.prepare_tags", return_value={"domains": [], "status": "no_candidates"}
        ), patch("radar.gemini.send") as api:
            cursor = connect.return_value.__enter__.return_value.execute.return_value
            cursor.fetchone.return_value = (False,)
            with self.assertRaisesRegex(RuntimeError, "Another Radar"):
                tag_batch("busy", 1)
            cursor.fetchone.return_value = (True,)
            self.assertEqual(tag_batch("empty", 1)["status"], "no_candidates")
            api.assert_not_called()
            self.assertFalse((Path(folder) / "raw/gemini/quota").exists())

    def test_paused_dag_has_zero_retries_and_scoped_secrets(self):
        dag = MagicMock()
        operator = MagicMock()
        modules = {
            "airflow": types.SimpleNamespace(DAG=dag),
            "airflow.sdk": types.SimpleNamespace(Param=lambda value, **kw: value),
            "airflow.providers.cncf.kubernetes.operators.pod": types.SimpleNamespace(
                KubernetesPodOperator=operator),
            "airflow.providers.cncf.kubernetes.callbacks": types.SimpleNamespace(
                KubernetesPodOperatorCallback=object),
        }
        with patch.dict("sys.modules", modules):
            runpy.run_path(str(Path(__file__).resolve().parents[1] / "airflow/radar_tagging.py"))
        self.assertTrue(dag.call_args.kwargs["is_paused_upon_creation"])
        self.assertEqual(dag.call_args.kwargs["default_args"], {"retries": 0})
        kwargs = operator.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "radar")
        self.assertEqual(kwargs["image"], "ghcr.io/beolle/radar-project-pipeline:v0.4.1")
        env = kwargs["pod_template_dict"]["spec"]["containers"][0]["env"]
        self.assertFalse(any(e["name"] == "GEMINI_API_KEY" for e in env))
        self.assertNotIn("radar-gemini", str(kwargs))
        self.assertEqual(len(kwargs["callbacks"]), 1)


if __name__ == "__main__":
    unittest.main()
