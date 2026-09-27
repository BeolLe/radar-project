"""Cloudflare ranking adapters; no database writes here."""
import csv
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import StringIO
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import FULL_BUCKETS, validate_snapshot

ENDPOINT = "https://api.cloudflare.com/client/v4/radar/ranking/top"
MAX_BYTES = 2 * 1024 * 1024
API_ROOT = "https://api.cloudflare.com/client/v4/radar"


class NoRedirect(HTTPRedirectHandler):
    # Bearer credentials must never follow an API redirect to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected a YYYY-MM-DD dataset date")
    if date.fromisoformat(value) > datetime.now(timezone.utc).date():
        raise ValueError("Refusing a future dataset date")
    return value


def request_top(token: str, location: str, day: str | None) -> dict:
    params = {"rankingType": "POPULAR", "limit": 100, "format": "JSON"}
    if location != "WORLD":
        params["location"] = location
    if day is not None:
        params["date"] = day
    return request_json(token, "/ranking/top", params)


def request_bytes(token: str, path: str, params: dict, max_bytes=MAX_BYTES) -> bytes:
    if not re.fullmatch(r"/(ranking/top|entities/locations|datasets(?:/[0-9]+)?)", path):
        raise ValueError("Unsupported Radar API path")
    request = Request(API_ROOT + path + "?" + urlencode(params), headers={
        "Authorization": "Bearer " + token,
        "Accept": "text/csv" if path.startswith("/datasets/") else "application/json",
        "User-Agent": "radar-project/0.3 (ranking collector)",
    })
    opener = build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("Cloudflare response exceeds the configured size limit")
            return body
        except HTTPError as error:
            status, retry_after = error.code, error.headers.get("Retry-After")
            error.close()
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"Cloudflare HTTP {status}; no snapshot published by fetch") from None
            delay = 2 ** attempt
            if retry_after:
                try:
                    delay = int(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        raise RuntimeError("Invalid Cloudflare Retry-After; retry manually later") from None
                if delay > 30:
                    raise RuntimeError("Cloudflare requests a longer cooldown; retry manually later") from None
            time.sleep(max(0, delay))
        except (URLError, TimeoutError, OSError):
            # Do not print request headers, tokens, or provider response bodies.
            if attempt == 2:
                raise RuntimeError("Cloudflare network request failed after 3 attempts") from None
            time.sleep(2 ** attempt)
    raise RuntimeError("Cloudflare request did not complete")


def request_json(token: str, path: str, params: dict) -> dict:
    try:
        result = json.loads(request_bytes(token, path, params))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Cloudflare returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ValueError("Cloudflare response must be a JSON object")
    return result


def api_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise ValueError("Set CLOUDFLARE_API_TOKEN without whitespace")
    return token


def result_rows(token: str, path: str, key: str, params: dict):
    """Stop only on an empty page; never treat a server-side page cap as EOF."""
    offset, seen = 0, set()
    for _ in range(20):
        envelope = request_json(token, path, {**params, "limit": 100, "offset": offset})
        if envelope.get("success") is not True or envelope.get("errors"):
            raise ValueError("Cloudflare catalog request failed")
        rows = envelope.get("result", {}).get(key)
        if not isinstance(rows, list):
            raise ValueError("Missing Cloudflare catalog array")
        if not rows:
            return
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Malformed Cloudflare catalog row")
            identity = row.get("id") if key == "datasets" else row.get("alpha2")
            if not isinstance(identity, (str, int)) or identity in seen:
                raise ValueError("Duplicate/missing catalog identity; refusing incomplete pagination")
            seen.add(identity)
            yield row
        offset += len(rows)
    raise ValueError("Cloudflare catalog exceeded the 20-page safety bound")


def daily_snapshot(envelope: dict, location: str, requested_date: str | None) -> dict:
    if envelope.get("success") is not True or envelope.get("errors"):
        raise ValueError("Cloudflare reported an unsuccessful response")
    try:
        result = envelope["result"]
        actual_date = check_date(result["meta"]["top_0"]["date"])
        rows = result["top_0"]
    except (KeyError, TypeError):
        raise ValueError("Missing Cloudflare top_0 rows or dataset date") from None
    if requested_date is not None and actual_date != requested_date:
        raise ValueError("Cloudflare returned a different dataset date; refusing fallback")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Cloudflare top_0 must be an array of objects")
    payload = {"kind": "daily", "date": actual_date, "location": location, "sources": [{
        "id": f"cloudflare-popular-{actual_date}-{location}",
        "endpoint": ENDPOINT, "ranking_type": "POPULAR", "expected_rows": 100, "rows": rows,
    }]}
    validate_snapshot(payload)
    # Ignore transport/update timestamps; unchanged rankings must remain idempotent.
    # Preserve provider row fields (including categories) in raw Parquet, not AI tags.
    payload["sources"][0]["rows"] = sorted(rows, key=lambda row: row["rank"])
    return payload


def collect_daily(locations: list[str], day: str | None = None) -> list[dict]:
    if (not locations or len(set(locations)) != len(locations)
            or any(not re.fullmatch(r"WORLD|[A-Z]{2}", location) for location in locations)):
        raise ValueError("Provide distinct WORLD/uppercase alpha-2 locations")
    if day is not None:
        check_date(day)
    token = api_token()
    payloads = []
    for location in locations:
        payload = daily_snapshot(request_top(token, location, day), location, day)
        day = payload["date"]  # Pin all remaining locations to the first dataset's date.
        payloads.append(payload)
    return payloads


def collect_all_daily(day: str) -> tuple[list[dict], list[dict]]:
    """Each location is a separate snapshot; failed/empty lists never imply rank exits."""
    check_date(day)
    token = api_token()
    locations = sorted(row["alpha2"] for row in result_rows(token, "/entities/locations", "locations", {}))
    if not locations or any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code) for code in locations):
        raise ValueError("Invalid or empty location catalog")
    payloads, report = [], []
    for location in ["WORLD", *locations]:
        try:
            envelope = request_top(token, location, day)
            payload = daily_snapshot(envelope, location, day)
            payloads.append(payload)
            status = {"location": location, "status": "validated", "rows": 100}
        except (ValueError, RuntimeError, KeyError, TypeError) as error:
            # No undocumented 400/404/short-list exception is silently called 'unsupported'.
            status = {"location": location, "status": "failed", "error": str(error)}
            if isinstance(error, RuntimeError) and any(code in str(error) for code in ("HTTP 401", "HTTP 403", "HTTP 429", "cooldown")):
                raise RuntimeError(f"Collection stopped at {location}: {error}") from None
        report.append(status)
        print(json.dumps({"event": "daily_fetch", "date": day, **status}), flush=True)
        time.sleep(1)  # Serial requests share one account budget; no per-country fan-out.
    return payloads, report


def weekly_plan(token: str, as_of: str) -> list[dict]:
    check_date(as_of)
    day = date.fromisoformat(as_of)
    end_date = (day - timedelta(days=day.weekday())).isoformat()
    # ponytail: current collection only; historical backfill needs date-filtered pagination.
    envelope = request_json(token, "/datasets", {"datasetType": "RANKING_BUCKET", "limit": 100, "offset": 0})
    if envelope.get("success") is not True or envelope.get("errors"):
        raise ValueError("Cloudflare weekly catalog request failed")
    items = envelope.get("result", {}).get("datasets")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("Missing weekly catalog array")
    selected = {}
    for item in items:
        meta = item.get("meta", {})
        if (item.get("type") != "RANKING_BUCKET" or not isinstance(meta, dict)
                or meta.get("top") not in FULL_BUCKETS):
            continue
        start, end = check_date(meta.get("targetDateStart")), check_date(meta.get("targetDateEnd"))
        if date.fromisoformat(end) - date.fromisoformat(start) != timedelta(days=7):
            raise ValueError("Weekly catalog period is not seven days")
        if end != end_date:
            continue
        bucket = meta["top"]
        if type(item.get("id")) is not int or item["id"] <= 0 or bucket in selected:
            raise ValueError("Invalid/ambiguous weekly dataset identity")
        selected[bucket] = item
    if set(selected) != set(FULL_BUCKETS):
        raise ValueError(f"Weekly period ending {end_date} does not contain all twelve buckets; no older fallback")
    if len({item["meta"]["targetDateStart"] for item in selected.values()}) != 1:
        raise ValueError("Mixed weekly source periods")
    if len({item["id"] for item in selected.values()}) != len(FULL_BUCKETS):
        raise ValueError("Weekly buckets must have distinct dataset IDs")
    return [selected[bucket] for bucket in FULL_BUCKETS]


def bucket_rows(body: bytes, expected: int) -> list[dict]:
    reader = csv.DictReader(StringIO(body.decode("utf-8-sig")), strict=True)
    if reader.fieldnames != ["domain"]:
        raise ValueError("Expected a single domain column in weekly CSV")
    rows = []
    for row in reader:
        if set(row) != {"domain"} or not row["domain"]:
            raise ValueError("Malformed weekly CSV row")
        rows.append(row)
        if len(rows) > expected:
            raise ValueError("Weekly CSV exceeds its declared bucket size")
    if len(rows) != expected:
        raise ValueError("Weekly CSV count differs from its declared bucket size")
    return sorted(rows, key=lambda row: row["domain"])


def collect_weekly(as_of: str) -> dict:
    token = api_token()
    plan = weekly_plan(token, as_of)
    sources = []
    for item in plan:
        bucket, dataset_id = item["meta"]["top"], item["id"]
        print(json.dumps({"event": "weekly_fetch", "bucket": bucket, "dataset_id": dataset_id}), flush=True)
        body = request_bytes(token, f"/datasets/{dataset_id}", {}, max_bytes=bucket * 260 + 1024)
        sources.append({"id": f"cloudflare-dataset-{dataset_id}", "bucket": bucket,
                        "expected_rows": bucket, "rows": bucket_rows(body, bucket),
                        "catalog": item})
        del body
    payload = {"kind": "weekly", "date": plan[0]["meta"]["targetDateEnd"], "location": "WORLD",
               "period_start": plan[0]["meta"]["targetDateStart"], "sources": sources}
    validate_snapshot(payload)
    return payload
