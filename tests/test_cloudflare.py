"""Synthetic API contracts; never calls Cloudflare or claims live API validation."""
import copy
from io import BytesIO, StringIO
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from radar import cloudflare
from radar.__main__ import archive, digest, main, read_archive
from radar.domain import FULL_BUCKETS, validate_snapshot


def catalog(end="2026-01-19", start="2026-01-12"):
    return {"success": True, "result": {"datasets": [
        {"id": index + 1, "type": "RANKING_BUCKET", "meta": {
            "top": bucket, "targetDateStart": start, "targetDateEnd": end}}
        for index, bucket in enumerate(FULL_BUCKETS)]}}


def response(day="2026-01-20"):
    return {"success": True, "errors": [], "result": {
        "meta": {"top_0": {"date": day}, "lastUpdated": day + "T12:00:00Z"},
        "top_0": [{"domain": f"domain-{rank}.example", "rank": rank,
                   "categories": [{"id": 1, "name": "Synthetic", "superCategoryId": 0}]}
                  for rank in range(1, 101)],
    }}


class CloudflareTests(unittest.TestCase):
    def test_daily_boundary_ties_keep_all_rows_and_raw_identity(self):
        # Observed MS boundaries: 2026-09-28 has five rank-98 entries (102 rows),
        # 2026-09-29 has three rank-99 entries (101 rows). Domains are synthetic.
        for boundary, count in ((98, 102), (99, 101)):
            with self.subTest(count=count):
                envelope = response()
                rows = [{"domain": f"domain-{i}.example", "rank": min(i, boundary)}
                        for i in range(1, count + 1)]
                envelope["result"]["top_0"] = rows
                payload = cloudflare.daily_snapshot(envelope, "MS", "2026-01-20")
                self.assertEqual(payload["sources"][0]["expected_rows"], count)
                self.assertEqual(len(validate_snapshot(payload)[2]), count)
                self.assertEqual(validate_snapshot(payload)[2][f"domain-{count}.example"], boundary)
                rows.reverse()
                self.assertEqual(digest(payload), digest(cloudflare.daily_snapshot(
                    envelope, "MS", "2026-01-20")))
                with tempfile.TemporaryDirectory(prefix="radar-boundary-") as directory, patch.dict(
                    os.environ, {"RADAR_DATA_DIR": directory}
                ):
                    restored = read_archive(archive(payload, digest(payload)), digest(payload))
                    self.assertEqual(restored, payload)
                    self.assertEqual(len(validate_snapshot(restored)[1]), count)
                damaged = copy.deepcopy(payload)
                damaged["sources"][0]["rows"].pop()
                with self.assertRaisesRegex(ValueError, "Source count"):
                    validate_snapshot(damaged)

    def test_daily_excess_does_not_bypass_validation(self):
        for mutate in (
            lambda e: e["result"]["top_0"][0].update(domain="domain-2.example"),
            lambda e: e["result"]["top_0"][0].update(domain="https://bad.example"),
            lambda e: e["result"]["top_0"][0].update(rank=101),
            lambda e: e["result"]["top_0"][0].update(rank=0),
            lambda e: e["result"]["top_0"][0].update(rank=True),
            lambda e: e["result"]["top_0"][0].update(rank="1"),
            lambda e: e["result"]["meta"]["top_0"].update(date="2026-01-19"),
            lambda e: e.update(success=False),
        ):
            envelope = response()
            envelope["result"]["top_0"].append({"domain": "extra.example", "rank": 100})
            mutate(envelope)
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                cloudflare.daily_snapshot(envelope, "MS", "2026-01-20")

    def test_daily_callers_report_actual_row_count(self):
        envelope = response()
        envelope["result"]["top_0"].append({"domain": "extra.example", "rank": 100})
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.result_rows", return_value=[{"alpha2": "MS"}]
        ), patch("radar.cloudflare.request_top", return_value=envelope), patch(
            "radar.cloudflare.time.sleep"
        ), patch("radar.__main__.ingest", return_value={"status": "published"}) as ingest, patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            with patch("sys.argv", ["radar", "collect-daily", "--locations", "MS", "--date", "2026-01-20"]):
                main()
            self.assertEqual(json.loads(output.getvalue())[0]["rows"], 101)
            output.seek(0)
            output.truncate()
            with patch("sys.argv", ["radar", "collect-all-daily", "--date", "2026-01-20"]):
                main()
            fetches = [json.loads(line) for line in output.getvalue().splitlines()
                       if line.startswith('{"event": "daily_fetch"')]
            self.assertEqual([row["rows"] for row in fetches], [101, 101])
            self.assertEqual(ingest.call_count, 3)
            self.assertTrue(all(len(call.args[0]["sources"][0]["rows"]) == 101
                                for call in ingest.call_args_list))

    def test_daily_100_row_hash_is_unchanged(self):
        envelope = response()
        legacy = {"kind": "daily", "date": "2026-01-20", "location": "KR", "sources": [{
            "id": "cloudflare-popular-2026-01-20-KR", "endpoint": cloudflare.ENDPOINT,
            "ranking_type": "POPULAR", "expected_rows": 100, "rows": envelope["result"]["top_0"],
        }]}
        self.assertEqual(digest(cloudflare.daily_snapshot(envelope, "KR", "2026-01-20")), digest(legacy))

    def test_observed_ties_preserve_ranks_and_stable_raw(self):
        for location, tied_rank in (("AI", 87), ("BI", 79)):
            with self.subTest(location=location):
                envelope = response()
                envelope["result"]["top_0"][tied_rank]["rank"] = tied_rank
                payload = cloudflare.daily_snapshot(envelope, location, "2026-01-20")
                _, _, values = validate_snapshot(payload)
                self.assertEqual(values[f"domain-{tied_rank + 1}.example"], tied_rank)
                envelope["result"]["top_0"].reverse()
                self.assertEqual(digest(payload), digest(cloudflare.daily_snapshot(
                    envelope, location, "2026-01-20")))
                with tempfile.TemporaryDirectory(prefix="radar-ties-") as directory, patch.dict(
                    os.environ, {"RADAR_DATA_DIR": directory}
                ):
                    self.assertEqual(read_archive(archive(payload, digest(payload)), digest(payload)), payload)

    def test_observed_no_data_is_not_ingested_or_failed(self):
        empty = {"success": True, "result": {
            "meta": {"dateRange": None, "top_0": {"date": None}}, "top_0": []}}
        self.assertIsNone(cloudflare.daily_snapshot(empty, "AN", "2026-01-20"))
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.result_rows", return_value=[{"alpha2": "AN"}]
        ), patch("radar.cloudflare.request_top", side_effect=[response(), empty]), patch(
            "radar.cloudflare.time.sleep"
        ), patch("radar.__main__.ingest", return_value={"status": "already_published"}) as ingest, patch(
            "sys.stdout", new_callable=StringIO
        ) as output, patch("sys.argv", ["radar", "collect-all-daily", "--date", "2026-01-20"]):
            main()
        ingest.assert_called_once()
        self.assertEqual(ingest.call_args.args[0]["location"], "WORLD")
        summary = json.loads(output.getvalue()[output.getvalue().rfind('\n{') + 1:])
        self.assertEqual(summary["published"], 1)
        self.assertEqual(summary["no_data"], [{"location": "AN", "status": "no_data", "rows": 0}])
        self.assertEqual(summary["failures"], [])
        # The explicit-location command shares the same empty-result handling.
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.request_top", return_value=empty
        ), patch("sys.argv", ["radar", "collect-daily", "--locations", "AN", "--date", "2026-01-20"]), patch(
            "radar.__main__.ingest"
        ) as ingest, patch("sys.stdout", new_callable=StringIO) as output:
            main()
        ingest.assert_not_called()
        self.assertIn('"status": "no_data"', output.getvalue())

    def test_empty_or_malformed_is_not_automatically_no_data(self):
        empty = {"success": True, "result": {
            "meta": {"dateRange": None, "top_0": {"date": None}}, "top_0": []}}
        for mutate in (
            lambda e: e.update(success=False),
            lambda e: e.update(errors=[{"code": 1}]),
            lambda e: e["result"].pop("top_0"),
            lambda e: e["result"].update(top_0=None),
            lambda e: e["result"]["meta"]["top_0"].pop("date"),
            lambda e: e["result"]["meta"].pop("dateRange"),
            lambda e: e["result"]["meta"].update(dateRange=[]),
            lambda e: e["result"]["meta"]["top_0"].update(date="2026-01-20"),
            lambda e: e["result"].update(top_0=response()["result"]["top_0"]),
        ):
            envelope = copy.deepcopy(empty)
            mutate(envelope)
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                cloudflare.daily_snapshot(envelope, "AN", "2026-01-20")

    def test_weekly_catalog_pins_period_and_rejects_incomplete_latest(self):
        with patch("radar.cloudflare.request_json", return_value=catalog()) as fetch:
            plan = cloudflare.weekly_plan("synthetic-token", "2026-01-20")
            self.assertEqual([row["meta"]["top"] for row in plan], list(FULL_BUCKETS))
            self.assertEqual(fetch.call_args.args[2]["limit"], 100)
        older = catalog("2026-01-12", "2026-01-05")["result"]["datasets"]
        for mutate in (lambda rows: rows.pop(), lambda rows: rows.append(copy.deepcopy(rows[0])),
                       lambda rows: rows[0]["meta"].update(targetDateStart="2026-01-11")):
            envelope = catalog()
            mutate(envelope["result"]["datasets"])
            envelope["result"]["datasets"].extend(older)
            with patch("radar.cloudflare.request_json", return_value=envelope), self.assertRaises(ValueError):
                cloudflare.weekly_plan("synthetic-token", "2026-01-20")
        with patch("radar.cloudflare.request_json", return_value=catalog()), self.assertRaises(ValueError):
            cloudflare.weekly_plan("synthetic-token", "2026-01-27")

    def test_weekly_csv_and_twelve_bucket_adapter(self):
        self.assertEqual(cloudflare.bucket_rows(b"\xef\xbb\xbfdomain\nb.example\na.example\n", 2),
                         [{"domain": "a.example"}, {"domain": "b.example"}])
        for body in (b"rank,domain\n1,a.example\n", b"domain\n", b"domain\na.example,extra\n"):
            with self.assertRaises(ValueError):
                cloudflare.bucket_rows(body, 1)
        self.assertEqual(len(cloudflare.bucket_rows(b"domain\na.example\nb.example\n", 1)), 2)
        with self.assertRaises(ValueError):
            cloudflare.bucket_rows(b"domain\na.example\n", 2)
        # Scale only fixture counts; exercise the real twelve-bucket payload validator.
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.request_json", return_value=catalog()
        ), patch("radar.cloudflare.request_bytes", return_value=b"domain\na.example\n") as fetch, patch(
            "radar.cloudflare.bucket_rows", return_value=[{"domain": "a.example"}]
        ), patch("radar.cloudflare.validate_snapshot") as validate, patch("sys.stdout", new_callable=StringIO):
            payload = cloudflare.collect_weekly("2026-01-20")
            self.assertEqual(payload["date"], "2026-01-19")
            self.assertEqual(payload["period_start"], "2026-01-12")
            self.assertEqual(fetch.call_count, 12)
            self.assertEqual(fetch.call_args.args[1], "/datasets/12")
            validate.assert_called_once_with(payload)
        for source in payload["sources"]:
            source["expected_rows"] = 1
        _, rows, values = validate_snapshot(payload)
        self.assertEqual(len(rows), 12)
        self.assertEqual(values, {"a.example": 200})
        payload["sources"][1]["rows"] = [{"domain": "other.example"}]
        with self.assertRaises(ValueError):
            validate_snapshot(payload)

    def test_weekly_excess_and_single_labels_are_preserved(self):
        # Scaled fixture reproduces excess rows and the observed single labels.
        body = b"domain\nws\nrun.app\napi.example.com\nweb\n"
        parse_rows = cloudflare.bucket_rows
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.weekly_plan", return_value=catalog()["result"]["datasets"]
        ), patch("radar.cloudflare.request_bytes", return_value=body), patch(
            "radar.cloudflare.bucket_rows", side_effect=lambda data, bucket: parse_rows(data, 3)
        ), patch("sys.stdout", new_callable=StringIO) as output:
            payload = cloudflare.collect_weekly("2026-01-20")
        self.assertIn('"rows": 4', output.getvalue())
        for source in payload["sources"]:
            self.assertEqual(source["expected_rows"], 4)
            self.assertEqual(source["bucket"], source["catalog"]["meta"]["top"])
        _, rows, values = validate_snapshot(payload)
        self.assertEqual(len(rows), 48)
        self.assertEqual(values, {"web": 200, "ws": 200, "run.app": 200, "api.example.com": 200})
        with tempfile.TemporaryDirectory(prefix="radar-weekly-raw-") as directory, patch.dict(
            os.environ, {"RADAR_DATA_DIR": directory}
        ):
            restored = read_archive(archive(payload, digest(payload)), digest(payload))
            self.assertEqual(restored, payload)
            self.assertEqual(validate_snapshot(restored)[2], values)
        for mutate in (
            lambda p: p["sources"][0]["rows"].pop(),
            lambda p: p["sources"][1]["rows"][0].update(domain="other.example"),
            lambda p: p["sources"][0]["rows"][0].update(domain="run.app"),
            lambda p: p["sources"][0]["rows"][0].update(domain="bad name"),
            lambda p: p["sources"][0]["rows"][0].update(domain="127.0.0.1"),
        ):
            invalid = copy.deepcopy(payload)
            mutate(invalid)
            with self.assertRaises(ValueError):
                validate_snapshot(invalid)

    def test_weekly_exact_size_payload_hash_is_unchanged(self):
        legacy = {"kind": "weekly", "date": "2026-01-19", "location": "WORLD",
                  "period_start": "2026-01-12", "sources": []}
        for item in catalog()["result"]["datasets"][:1]:
            bucket = item["meta"]["top"]
            legacy["sources"].append({"id": f"cloudflare-dataset-{item['id']}", "bucket": bucket,
                "expected_rows": bucket, "rows": [{"domain": f"d{i}.example"} for i in range(bucket)],
                "catalog": item})
        # Avoid a million-domain fixture in the normal suite: test one catalog source
        # through the adapter, with full-set validation covered by the other tests.
        item = catalog()["result"]["datasets"][0]
        body = ("domain\n" + "\n".join(row["domain"] for row in legacy["sources"][0]["rows"]) + "\n").encode()
        legacy["sources"][0]["rows"].sort(key=lambda row: row["domain"])
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.weekly_plan", return_value=[item]
        ), patch("radar.cloudflare.request_bytes", return_value=body), patch(
            "radar.cloudflare.validate_snapshot"
        ), patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(digest(cloudflare.collect_weekly("2026-01-20")), digest(legacy))

    def test_location_pagination_waits_for_empty_page(self):
        pages = [{"success": True, "result": {"locations": rows}}
                 for rows in ([{"alpha2": "KR"}], [{"alpha2": "JP"}], [])]
        with patch("radar.cloudflare.request_json", side_effect=pages) as fetch:
            rows = list(cloudflare.result_rows("synthetic-token", "/entities/locations", "locations", {}))
            self.assertEqual([row["alpha2"] for row in rows], ["KR", "JP"])
            self.assertEqual([call.args[2]["offset"] for call in fetch.call_args_list], [0, 1, 2])
        with patch("radar.cloudflare.request_json", return_value=pages[0]), self.assertRaises(ValueError):
            list(cloudflare.result_rows("synthetic-token", "/entities/locations", "locations", {}))

    def test_all_locations_partial_failure_is_not_success_or_exit(self):
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.result_rows", return_value=[{"alpha2": "KR"}, {"alpha2": "AQ"}]
        ), patch("radar.cloudflare.request_top", side_effect=[response(), {"success": False}, response()]), patch(
            "radar.cloudflare.time.sleep"
        ), patch("sys.stdout", new_callable=StringIO):
            payloads, report = cloudflare.collect_all_daily("2026-01-20")
        self.assertEqual([payload["location"] for payload in payloads], ["WORLD", "KR"])
        self.assertEqual([row["status"] for row in report], ["validated", "failed", "validated"])
        with patch("radar.cloudflare.collect_all_daily", return_value=(payloads, report)), patch(
            "radar.__main__.ingest", return_value={"status": "already_published"}
        ) as ingest, patch("sys.stdout", new_callable=StringIO), patch(
            "sys.argv", ["radar", "collect-all-daily", "--date", "2026-01-20"]
        ), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(ingest.call_count, 2)

    def test_raw_roundtrip_and_stable_identity(self):
        envelope = response()
        payload = cloudflare.daily_snapshot(envelope, "KR", "2026-01-20")
        self.assertEqual(payload["date"], "2026-01-20")
        self.assertEqual(payload["sources"][0]["expected_rows"], 100)
        with tempfile.TemporaryDirectory(prefix="radar-cf-test-") as directory, patch.dict(
            os.environ, {"RADAR_DATA_DIR": directory}
        ):
            raw = archive(payload, digest(payload))
            self.assertEqual(read_archive(raw, digest(payload)), payload)
        changed_transport = copy.deepcopy(envelope)
        changed_transport["result"]["meta"]["lastUpdated"] = "2026-01-21T12:00:00Z"
        changed_transport["result"]["top_0"].reverse()
        self.assertEqual(digest(payload), digest(cloudflare.daily_snapshot(changed_transport, "KR", None)))
        changed_transport["result"]["top_0"][0]["domain"] = "different.example"
        self.assertNotEqual(digest(payload), digest(cloudflare.daily_snapshot(changed_transport, "KR", None)))

    def test_refuse_partial_duplicate_or_wrong_period(self):
        invalid = []
        short = response()
        short["result"]["top_0"].pop()
        invalid.append(short)
        duplicate = response()
        duplicate["result"]["top_0"][1]["domain"] = "domain-1.example"
        invalid.append(duplicate)
        rank = response()
        rank["result"]["top_0"][1]["rank"] = 101
        invalid.append(rank)
        missing_date = response()
        del missing_date["result"]["meta"]["top_0"]
        invalid.append(missing_date)
        invalid.extend([response("2026-01-19"), {"success": False}, {"success": True, "errors": [1]}])
        for envelope in invalid:
            with self.subTest(envelope=envelope), self.assertRaises((ValueError, KeyError)):
                cloudflare.daily_snapshot(envelope, "KR", "2026-01-20")

    def test_pin_date_and_fail_before_publish(self):
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.request_top", return_value=response()
        ) as fetch:
            payloads = cloudflare.collect_daily(["WORLD", "KR"])
            self.assertEqual([p["location"] for p in payloads], ["WORLD", "KR"])
            self.assertEqual(fetch.call_args_list[0].args, ("synthetic-token", "WORLD", None))
            self.assertEqual(fetch.call_args_list[1].args, ("synthetic-token", "KR", "2026-01-20"))
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.request_top", side_effect=[response(), response("2026-01-19")]
        ), patch("sys.argv", ["radar", "collect-daily"]), patch("radar.__main__.ingest") as ingest:
            with self.assertRaises(ValueError):
                main()
            ingest.assert_not_called()

    def test_cli_uses_existing_ingest(self):
        with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "synthetic-token"}), patch(
            "radar.cloudflare.request_top", return_value=response()
        ), patch("sys.argv", ["radar", "collect-daily", "--locations", "KR", "--date", "2026-01-20"]), patch(
            "radar.__main__.ingest", return_value={"status": "published"}
        ) as ingest, patch("sys.stdout", new_callable=StringIO) as output:
            main()
            self.assertEqual(ingest.call_args.args[0]["location"], "KR")
            self.assertEqual(json.loads(output.getvalue())[0]["rows"], 100)

    def test_inputs_fail_before_network(self):
        with patch("radar.cloudflare.request_top") as fetch:
            for locations in ([], ["WORLD", "WORLD"], ["kr"], ["http://evil.example"]):
                with self.assertRaises(ValueError):
                    cloudflare.collect_daily(locations)
            with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}), self.assertRaises(ValueError):
                cloudflare.collect_daily(["WORLD"])
            for day in ("20260120", "2026-02-30", "9999-12-31"):
                with self.assertRaises(ValueError):
                    cloudflare.collect_daily(["WORLD"], day)
            fetch.assert_not_called()

    def test_http_request_contract(self):
        with patch("radar.cloudflare.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(response()).encode()
            cloudflare.request_top("synthetic-token", "WORLD", None)
            args, kwargs = build.return_value.open.call_args
            self.assertEqual(kwargs["timeout"], 30)
            params = parse_qs(urlsplit(args[0].full_url).query)
            self.assertEqual(params, {"rankingType": ["POPULAR"], "limit": ["100"], "format": ["JSON"]})
            self.assertEqual(args[0].get_header("Authorization"), "Bearer synthetic-token")
            cloudflare.request_top("synthetic-token", "KR", "2026-01-20")
            params = parse_qs(urlsplit(build.return_value.open.call_args.args[0].full_url).query)
            self.assertEqual(params["location"], ["KR"])
            self.assertEqual(params["date"], ["2026-01-20"])
            self.assertIsInstance(build.call_args.args[0], cloudflare.NoRedirect)
            self.assertIsNone(cloudflare.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_retries_are_bounded_and_credentials_not_logged(self):
        for status, attempts in ((401, 1), (403, 1), (302, 1), (429, 3), (503, 3)):
            with self.subTest(status=status), patch("radar.cloudflare.build_opener") as build, patch("radar.cloudflare.time.sleep"):
                build.return_value.open.side_effect = [
                    HTTPError("https://api.cloudflare.com", status, "synthetic-token", {}, BytesIO())
                    for _ in range(attempts)]
                with self.assertRaises(RuntimeError) as caught:
                    cloudflare.request_top("synthetic-token", "KR", None)
                self.assertNotIn("synthetic-token", str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, attempts)
        with patch("radar.cloudflare.build_opener") as build, patch("radar.cloudflare.time.sleep") as sleep:
            build.return_value.open.side_effect = HTTPError("https://api.cloudflare.com", 429, "", {"Retry-After": "120"}, BytesIO())
            with self.assertRaises(RuntimeError):
                cloudflare.request_top("synthetic-token", "KR", None)
            sleep.assert_not_called()
        with patch("radar.cloudflare.build_opener") as build, patch("radar.cloudflare.time.sleep"):
            build.return_value.open.side_effect = URLError("synthetic-token")
            with self.assertRaises(RuntimeError) as caught:
                cloudflare.request_top("synthetic-token", "KR", None)
            self.assertNotIn("synthetic-token", str(caught.exception))
            self.assertEqual(build.return_value.open.call_count, 3)

    def test_reject_bad_transport_payload(self):
        for body in (b"<html>error</html>", b"[]", b"x" * (cloudflare.MAX_BYTES + 1)):
            with patch("radar.cloudflare.build_opener") as build, self.assertRaises(ValueError):
                build.return_value.open.return_value.__enter__.return_value.read.return_value = body
                cloudflare.request_top("synthetic-token", "WORLD", None)


if __name__ == "__main__":
    unittest.main()
