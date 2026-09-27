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


def response(day="2026-01-20"):
    return {"success": True, "errors": [], "result": {
        "meta": {"top_0": {"date": day}, "lastUpdated": day + "T12:00:00Z"},
        "top_0": [{"domain": f"domain-{rank}.example", "rank": rank,
                   "categories": [{"id": 1, "name": "Synthetic", "superCategoryId": 0}]}
                  for rank in range(1, 101)],
    }}


class CloudflareTests(unittest.TestCase):
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
        rank["result"]["top_0"][1]["rank"] = 1
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
