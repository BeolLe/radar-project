"""Input contract and deterministic rules, independent of PostgreSQL."""
from datetime import date, timedelta
import ipaddress
import re

BUCKETS = (100_000, 200_000, 500_000, 1_000_000)
FULL_BUCKETS = (200, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000, *BUCKETS)


def domain_name(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Domain must be a nonempty hostname without whitespace")
    value = value.rstrip(".").encode("idna").decode("ascii").lower()
    if len(value) > 253 or "." not in value:
        raise ValueError("Expected a public-style hostname, not a URL")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not domains")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
           for p in value.split(".")):
        raise ValueError("Invalid hostname")
    # Preserve subdomains; www and a registrable domain are not merged.
    return value


def validate_snapshot(payload: dict) -> tuple[dict, list[dict], dict[str, int]]:
    """Expected row counts are a caller-supplied source contract, not guessed here."""
    if payload.get("kind") not in {"weekly", "daily"}:
        raise ValueError("kind must be weekly or daily")
    kind = payload["kind"]
    period = date.fromisoformat(payload["date"])
    location = payload.get("location", "WORLD")
    if not isinstance(location, str) or not re.fullmatch(r"WORLD|[A-Z]{2}", location):
        raise ValueError("location must be WORLD or an uppercase alpha-2 code")
    if kind == "weekly" and location != "WORLD":
        raise ValueError("Weekly buckets are global only")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must not be empty")
    if kind == "weekly" and tuple(sorted(s.get("bucket", 0) for s in sources)) not in (BUCKETS, FULL_BUCKETS):
        raise ValueError("Expected the legacy four or all twelve weekly bucket sources")
    if kind == "daily" and len(sources) != 1:
        raise ValueError("One POPULAR daily list per snapshot")
    rows, values, sets, ids = [], {}, [], set()
    for source in sorted(sources, key=lambda s: s.get("bucket", 0)):
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in ids:
            raise ValueError("Every source needs a distinct nonempty id")
        ids.add(source_id)
        items, expected = source.get("rows"), source.get("expected_rows")
        if (not isinstance(items, list) or type(expected) is not int
                or expected <= 0 or len(items) != expected):
            raise ValueError("Source count does not match expected_rows")
        names = set()
        for item in items:
            name = domain_name(item["domain"])
            if name in names:
                raise ValueError("Duplicate domain inside one source")
            names.add(name)
            value = source["bucket"] if kind == "weekly" else item.get("rank")
            if type(value) is not int or value <= 0:
                raise ValueError("Rank/bucket must be a positive integer")
            # Provider ranks can tie and skip positions; never renumber them.
            if kind == "daily" and value > 100:
                raise ValueError("Daily ranks must be within 1..100")
            values[name] = min(values.get(name, value), value)
            rows.append({"source_id": source_id, "domain": name, "value": value})
        sets.append(names)
    if kind == "weekly" and any(not a <= b for a, b in zip(sets, sets[1:])):
        raise ValueError("Weekly source buckets are not cumulative")
    meta = {"kind": kind, "date": period, "location": location,
            "source_ids": sorted(ids), "period_days": 7 if kind == "weekly" else 1}
    return meta, rows, values


def previous_date(meta: dict) -> date:
    return meta["date"] - timedelta(days=meta["period_days"])
