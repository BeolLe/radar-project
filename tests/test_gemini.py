import copy
import json
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from radar.gemini import api_payload, normalize, reserve_budget, send, tag_batch, tag_pending
from radar.tagging import make_request
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
            request = prepare_tags("preliminary", 3)
            self.assertEqual([d["domain_id"] for d in request["domains"]], [10, 20, 1])
            country_call, global_call = conn.execute.call_args_list
            self.assertIn("s.location='KR' THEN 0 ELSE 1", country_call.args[0])
            self.assertIn("s.location<>'WORLD'", country_call.args[0])
            self.assertEqual(global_call.args[1], ([10, 20], "preliminary", 1))

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

    def test_unknown_is_completed_without_tags(self):
        self.response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({"results": [
            {"domain_id": 1, "status": "unknown", "tags": [], "reason": "근거 부족"}]})
        rows = normalize(self.request, json.dumps(self.response), self.timestamp)["results"]
        self.assertEqual(rows[0]["status"], "unknown")

    def test_http_error_no_retry_or_secret_in_error(self):
        with patch("radar.gemini.urlopen", side_effect=HTTPError("url", 429, "secret", {}, None)) as http:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429") as caught:
                send(api_payload(self.request), "test-secret")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(http.call_count, 1)
            self.assertNotIn("test-secret", http.call_args.args[0].full_url)

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
        self.assertEqual(kwargs["image"], "ghcr.io/beolle/radar-project-pipeline:v0.4.0")
        env = kwargs["pod_template_dict"]["spec"]["containers"][0]["env"]
        self.assertFalse(any(e["name"] == "GEMINI_API_KEY" for e in env))
        self.assertNotIn("radar-gemini", str(kwargs))
        self.assertEqual(len(kwargs["callbacks"]), 1)


if __name__ == "__main__":
    unittest.main()
