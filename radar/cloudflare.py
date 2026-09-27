"""Cloudflare POPULAR daily top-100 adapter; no database writes here."""
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import validate_snapshot

ENDPOINT = "https://api.cloudflare.com/client/v4/radar/ranking/top"
MAX_BYTES = 2 * 1024 * 1024


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
    request = Request(ENDPOINT + "?" + urlencode(params), headers={
        "Authorization": "Bearer " + token, "Accept": "application/json",
        "User-Agent": "radar-project/0.2 (daily collector)",
    })
    opener = build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Cloudflare top-100 response exceeds 2 MiB")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError("Cloudflare response must be a JSON object")
            return result
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Cloudflare returned invalid JSON") from None
    raise RuntimeError("Cloudflare request did not complete")


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
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise ValueError("Set CLOUDFLARE_API_TOKEN without whitespace")
    payloads = []
    for location in locations:
        payload = daily_snapshot(request_top(token, location, day), location, day)
        day = payload["date"]  # Pin all remaining locations to the first dataset's date.
        payloads.append(payload)
    return payloads
