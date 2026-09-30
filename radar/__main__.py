"""Run with uv run --env-file .env python -m radar --help."""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .domain import BUCKETS, previous_date, validate_snapshot
from .tagging import LIMITS, MODEL, make_request, validate_results

ROOT = Path(__file__).resolve().parents[1]


def connect():
    import psycopg
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("Set DATABASE_URL to a dedicated local/test PostgreSQL database")
    return psycopg.connect(url, autocommit=True)


def digest(payload) -> str:
    result = hashlib.sha256()
    for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False).iterencode(payload):
        result.update(chunk.encode())
    return result.hexdigest()


def archive(payload: dict, batch: str) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq
    directory = Path(os.environ.get("RADAR_DATA_DIR", "data")) / "raw"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{batch}.parquet"
    if target.exists():
        if pq.read_schema(target).metadata.get(b"batch_hash") != batch.encode():
            raise ValueError("Existing raw file metadata mismatch")
        return target
    metadata = {**payload, "sources": [{k: v for k, v in s.items() if k != "rows"}
                                       for s in payload["sources"]]}
    schema = pa.schema([("source_id", pa.string()), ("bucket", pa.int64()), ("payload", pa.string())],
                       metadata={b"batch_hash": batch.encode(),
                                 b"source_metadata": json.dumps(metadata).encode()})
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".parquet", delete=False) as f:
        temporary = Path(f.name)
    try:
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            for source in payload["sources"]:
                for start in range(0, len(source["rows"]), 10_000):
                    rows = [{"source_id": source["id"], "bucket": source.get("bucket"),
                             "payload": json.dumps(row, ensure_ascii=False)}
                            for row in source["rows"][start:start + 10_000]]
                    writer.write_table(pa.Table.from_pylist(rows, schema=schema))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def ingest(payload: dict) -> dict:
    from psycopg.types.json import Jsonb
    validate_snapshot(payload)
    batch = digest(payload)
    raw = archive(payload, batch)
    # Stage is reconstructed from raw Parquet, not from an in-memory alternate path.
    meta, rows, _ = validate_snapshot(read_archive(raw, batch))
    del _
    with connect() as conn:
        # ponytail: one ingestion writer for this draft; partition locks by stream if needed.
        conn.execute("SELECT pg_advisory_lock(73194201)")
        existing = conn.execute("SELECT id, batch_hash FROM core.snapshot "
                                "WHERE kind=%s AND period_date=%s AND location=%s",
                                (meta["kind"], meta["date"], meta["location"])).fetchone()
        if existing:
            if existing[1] != batch:
                raise ValueError("Revision detected: draft refuses to overwrite a published period")
            return {"snapshot_id": existing[0], "status": "already_published"}
        latest = conn.execute("SELECT max(period_date) FROM core.snapshot WHERE kind=%s AND location=%s",
                              (meta["kind"], meta["location"])).fetchone()[0]
        if latest and meta["date"] < latest:
            raise ValueError("Load periods chronologically; historical mart rebuild is not implemented")
        # Stage commits first. A failed publish retains its raw file and staging rows.
        with conn.transaction():
            conn.execute("DELETE FROM stage.observation WHERE batch_hash=%s", (batch,))
            with conn.cursor().copy("COPY stage.observation FROM STDIN") as copy:
                for n, row in enumerate(rows):
                    copy.write_row((batch, n, row["source_id"], row["domain"], row["value"]))
        with conn.transaction():
            snapshot_id = conn.execute(
                "INSERT INTO core.snapshot (kind,period_date,location,batch_hash,raw_path,source_ids) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (meta["kind"], meta["date"], meta["location"], batch, str(raw),
                 Jsonb(meta["source_ids"]))
            ).fetchone()[0]
            conn.execute("INSERT INTO core.domain(name) SELECT DISTINCT domain FROM stage.observation "
                         "WHERE batch_hash=%s ON CONFLICT (name) DO NOTHING", (batch,))
            conn.execute("INSERT INTO core.observation SELECT %s,d.id,min(s.value) "
                         "FROM stage.observation s JOIN core.domain d ON d.name=s.domain "
                         "WHERE s.batch_hash=%s GROUP BY d.id", (snapshot_id, batch))
            previous = conn.execute("SELECT id FROM core.snapshot WHERE kind=%s AND location=%s "
                                    "AND period_date=%s",
                                    (meta["kind"], meta["location"], previous_date(meta))).fetchone()
            if previous:
                build_signals(conn, snapshot_id, previous[0], meta)
            conn.execute("DELETE FROM stage.observation WHERE batch_hash=%s", (batch,))
    return {"snapshot_id": snapshot_id, "status": "published", "raw": str(raw)}


def build_signals(conn, current: int, previous: int, meta: dict):
    conn.execute("""
      INSERT INTO mart.domain_signal(snapshot_id,domain_id,signal)
      SELECT %s,c.domain_id, CASE WHEN EXISTS (
        SELECT 1 FROM core.observation o JOIN core.snapshot s ON s.id=o.snapshot_id
        WHERE o.domain_id=c.domain_id AND s.kind=%s AND s.location=%s AND s.period_date<%s
      ) THEN 'reentry' ELSE 'first_seen' END
      FROM core.observation c LEFT JOIN core.observation p
        ON p.snapshot_id=%s AND p.domain_id=c.domain_id
      WHERE c.snapshot_id=%s AND p.domain_id IS NULL
    """, (current, meta["kind"], meta["location"], meta["date"], previous, current))
    conn.execute("""
      INSERT INTO mart.domain_signal(snapshot_id,domain_id,signal)
      SELECT %s,c.domain_id,'improved' FROM core.observation c JOIN core.observation p
        ON p.domain_id=c.domain_id AND p.snapshot_id=%s
      WHERE c.snapshot_id=%s AND c.value<p.value
    """, (current, previous, current))
    conn.execute("""
      INSERT INTO mart.domain_signal(snapshot_id,domain_id,signal)
      SELECT %s,c.domain_id,'consecutive_improvement' FROM mart.domain_signal c
      JOIN mart.domain_signal p ON p.domain_id=c.domain_id AND p.snapshot_id=%s
      WHERE c.snapshot_id=%s AND c.signal='improved' AND p.signal='improved'
    """, (current, previous, current))


def prepare_tags(phase: str, limit: int, exclude_ids=()) -> dict:
    if not 1 <= limit <= LIMITS[phase]:
        raise ValueError(f"Limit for {phase} must be 1..{LIMITS[phase]}")
    with connect() as conn:
        # Unknown attempts are not silently retried forever. They need explicit later review.
        # Latest KR list and its own ranks win, not historical or foreign best ranks.
        rows = conn.execute("""
          WITH latest AS (
            SELECT location,max(period_date) AS period_date FROM core.snapshot
            WHERE kind='daily' AND location<>'WORLD' GROUP BY location
          )
          SELECT d.id,d.name FROM core.domain d
          JOIN core.observation o ON o.domain_id=d.id
          JOIN core.snapshot s ON s.id=o.snapshot_id
          JOIN latest l ON l.location=s.location AND l.period_date=s.period_date
          WHERE s.kind='daily' AND d.id<>ALL(%s::bigint[]) AND NOT EXISTS (
            SELECT 1 FROM core.tag_result t WHERE t.domain_id=d.id AND t.phase=%s
          ) GROUP BY d.id
          ORDER BY min(CASE WHEN s.location='KR' THEN 0 ELSE 1 END),
            COALESCE(min(o.value) FILTER (WHERE s.location='KR'),min(o.value)),d.id
          LIMIT %s
        """, (list(exclude_ids), phase, limit)).fetchall()
        if len(rows) < limit:
            rows += conn.execute("""
              SELECT d.id,d.name FROM core.domain d WHERE d.id<>ALL(%s::bigint[])
              AND NOT EXISTS (
                SELECT 1 FROM core.tag_result t WHERE t.domain_id=d.id AND t.phase=%s
              ) ORDER BY d.id LIMIT %s
            """, ([*exclude_ids, *(row[0] for row in rows)], phase, limit - len(rows))).fetchall()
    if not rows:
        return {"phase": phase, "domains": [], "status": "no_candidates"}
    return make_request(phase, [{"domain_id": row[0], "domain": row[1],
                                "url": "https://" + row[1]} for row in rows])


def import_tags(request: dict, envelope: dict) -> dict:
    from psycopg.types.json import Jsonb
    if (request.get("model") != MODEL or request.get("taxonomy_version") != "v1"
            or request.get("prompt_version") != "v1"):
        raise ValueError("Unsupported model or schema version")
    make_request(request["phase"], request["domains"])
    rows = validate_results(request, envelope)
    with connect() as conn, conn.transaction():
        for domain in request["domains"]:
            found = conn.execute("SELECT name FROM core.domain WHERE id=%s",
                                 (domain["domain_id"],)).fetchone()
            if not found or found[0] != domain["domain"]:
                raise ValueError("Request domain mapping does not match this database")
        for row in rows:
            result_hash = digest({"request": request, "result": {
                **row, "checked_at": row["checked_at"].isoformat()}})
            conn.execute("""
              INSERT INTO core.tag_result
                (domain_id,phase,status,model,prompt_version,taxonomy_version,tags,evidence,
                 checked_at,result_hash)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT (result_hash) DO NOTHING
            """, (row["domain_id"], request["phase"], row["status"], request["model"],
                  request["prompt_version"], request["taxonomy_version"], Jsonb(row["tags"]),
                  Jsonb({"tool": row["evidence"], "reason": row.get("reason", ""),
                         **({"review": row["review"]} if "review" in row else {}),
                         **({"raw_path": envelope["raw_path"]} if "raw_path" in envelope else {}),
                         **({"api": envelope["api_metadata"]} if "api_metadata" in envelope else {})}),
                  row["checked_at"], result_hash))
    reviews = [row["domain_id"] for row in rows if "review" in row]
    return {"validated_results": len(rows),
            **({"review_required": len(reviews), "review_domain_ids": reviews} if reviews else {}),
            "missing_ids": sorted(
        {d["domain_id"] for d in request["domains"]} - {r["domain_id"] for r in rows})}


def read_archive(path: Path, expected_hash: str) -> dict:
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(path)
    payload = json.loads(parquet.schema_arrow.metadata[b"source_metadata"])
    by_id = {source["id"]: source for source in payload["sources"]}
    for source in by_id.values():
        source["rows"] = []
    for batch in parquet.iter_batches(batch_size=10_000, columns=["source_id", "payload"]):
        for row in batch.to_pylist():
            by_id[row["source_id"]]["rows"].append(json.loads(row["payload"]))
    if digest(payload) != expected_hash:
        raise ValueError("Raw Parquet content hash mismatch")
    return payload


def demo_snapshots():
    """Reserved .example domains only; no real Cloudflare data or AI claims."""
    weeks = [
        {"alpha.example": 500000, "beta.example": 100000, "return.example": 1000000},
        {"alpha.example": 200000, "beta.example": 100000, "new.example": 1000000},
        {"alpha.example": 100000, "beta.example": 100000, "return.example": 500000},
    ]
    for day, values in zip(("2026-01-05", "2026-01-12", "2026-01-19"), weeks):
        sources = []
        for bound in BUCKETS:
            rows = [{"domain": d} for d, b in values.items() if b <= bound]
            sources.append({"id": f"demo-{day}-{bound}", "bucket": bound,
                            "expected_rows": len(rows), "rows": rows})
        yield {"kind": "weekly", "date": day, "location": "WORLD", "sources": sources}
    for location in ("KR", "JP"):
        yield {"kind": "daily", "date": "2026-01-19", "location": location,
               "sources": [{"id": f"demo-{location}", "expected_rows": 2,
                            "rows": [{"domain": "alpha.example", "rank": 1},
                                     {"domain": "beta.example", "rank": 2}]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create schema in the dedicated database")
    load = commands.add_parser("ingest", help="Import one complete source-contract JSON")
    load.add_argument("file", type=Path)
    collect = commands.add_parser("collect-daily", help="Fetch and ingest complete Cloudflare POPULAR top-100 lists")
    collect.add_argument("--locations", nargs="+", default=["WORLD", "KR"])
    collect.add_argument("--date", help="Exact dataset date YYYY-MM-DD; default: latest returned by API")
    all_daily = commands.add_parser("collect-all-daily", help="Discover all locations and publish independently validated lists")
    all_daily.add_argument("--date", required=True, help="Exact dataset date YYYY-MM-DD")
    weekly = commands.add_parser("collect-weekly", help="Collect all twelve global buckets for the latest Monday ending on/before as-of")
    weekly.add_argument("--as-of", required=True, help="Pinned scheduler date YYYY-MM-DD; no older-week fallback")
    commands.add_parser("demo", help="Import small synthetic snapshots; no network calls")
    commands.add_parser("demo-input", help="Print a valid synthetic weekly source contract")
    prepare = commands.add_parser("prepare-tags", help="Print offline request draft; no API call")
    prepare.add_argument("--phase", choices=LIMITS, required=True)
    prepare.add_argument("--limit", type=int)
    load_tags = commands.add_parser("import-tags", help="Import an adapter-normalized response")
    load_tags.add_argument("request", type=Path)
    load_tags.add_argument("response", type=Path)
    tag = commands.add_parser("tag-batch", help="Run/replay one preliminary Gemini batch, no HTTP retries")
    tag.add_argument("--batch-id", required=True, help="Unique durable execution ID; reuse to replay without API")
    tag.add_argument("--limit", type=int, default=100)
    tag.add_argument("--daily-limit", type=int, default=500)
    pending = commands.add_parser("tag-pending", help="Preliminary batches with a persistent daily call budget")
    pending.add_argument("--run-id", required=True)
    pending.add_argument("--max-requests", type=int, default=200)
    pending.add_argument("--limit", type=int, default=100)
    pending.add_argument("--daily-limit", type=int, default=500)
    args = parser.parse_args()
    if args.command == "init-db":
        with connect() as conn, conn.transaction():
            conn.execute((ROOT / "schema.sql").read_text())
        result = {"status": "schema_ready"}
    elif args.command == "demo-input":
        result = next(demo_snapshots())
    elif args.command == "ingest":
        result = ingest(json.loads(args.file.read_text()))
    elif args.command == "collect-daily":
        from .cloudflare import collect_daily
        # Validate every requested list before publishing any of this collection.
        payloads = collect_daily(args.locations, args.date)
        result = [{"date": payload["date"], "location": payload["location"],
                   "rows": len(payload["sources"][0]["rows"]), **ingest(payload)} for payload in payloads]
    elif args.command == "collect-all-daily":
        from .cloudflare import collect_all_daily
        payloads, report = collect_all_daily(args.date)
        for payload in payloads:
            print(json.dumps({"event": "daily_publish", "date": payload["date"],
                              "location": payload["location"], **ingest(payload)}), flush=True)
        result = {"date": args.date, "locations": len(report), "published": len(payloads),
                  "no_data": [row for row in report if row["status"] == "no_data"],
                  "failures": [row for row in report if row["status"] == "failed"]}
        if result["failures"]:
            print(json.dumps(result, ensure_ascii=False), flush=True)
            raise SystemExit(1)
    elif args.command == "collect-weekly":
        from .cloudflare import collect_weekly
        payload = collect_weekly(args.as_of)
        result = {"date": payload["date"], "location": "WORLD", "buckets": 12, **ingest(payload)}
    elif args.command == "demo":
        result = [ingest(p) for p in demo_snapshots()]
    elif args.command == "prepare-tags":
        result = prepare_tags(args.phase, args.limit or LIMITS[args.phase])
    elif args.command == "tag-batch":
        from .gemini import tag_batch
        result = tag_batch(args.batch_id, args.limit, args.daily_limit)
    elif args.command == "tag-pending":
        from .gemini import tag_pending
        result = tag_pending(args.run_id, args.max_requests, args.limit, args.daily_limit)
    else:
        result = import_tags(json.loads(args.request.read_text()), json.loads(args.response.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
