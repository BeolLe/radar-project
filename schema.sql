-- Fresh dedicated database only. Later schema changes require migrations.
CREATE SCHEMA IF NOT EXISTS stage;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS stage.observation (
    batch_hash text NOT NULL,
    row_number integer NOT NULL,
    source_id text NOT NULL,
    domain text NOT NULL,
    value integer NOT NULL CHECK (value > 0),
    PRIMARY KEY (batch_hash, row_number)
);

CREATE TABLE IF NOT EXISTS core.snapshot (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('weekly', 'daily')),
    period_date date NOT NULL,
    location text NOT NULL,
    batch_hash text NOT NULL UNIQUE,
    raw_path text NOT NULL,
    source_ids jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, period_date, location),
    CHECK (kind <> 'weekly' OR location = 'WORLD')
);
CREATE TABLE IF NOT EXISTS core.domain (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS core.observation (
    snapshot_id bigint NOT NULL REFERENCES core.snapshot,
    domain_id bigint NOT NULL REFERENCES core.domain,
    value integer NOT NULL CHECK (value > 0),
    PRIMARY KEY (snapshot_id, domain_id)
);
-- value means a bucket upper bound for weekly, an exact rank for daily.
CREATE INDEX IF NOT EXISTS observation_domain_history
    ON core.observation (domain_id, snapshot_id);
CREATE INDEX IF NOT EXISTS observation_ranking
    ON core.observation (snapshot_id, value, domain_id);

CREATE TABLE IF NOT EXISTS mart.domain_signal (
    snapshot_id bigint NOT NULL REFERENCES core.snapshot,
    domain_id bigint NOT NULL REFERENCES core.domain,
    signal text NOT NULL CHECK (signal IN
      ('first_seen', 'reentry', 'improved', 'consecutive_improvement')),
    rule_version text NOT NULL DEFAULT 'v1',
    PRIMARY KEY (snapshot_id, domain_id, signal)
);

CREATE TABLE IF NOT EXISTS core.tag_result (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain_id bigint NOT NULL REFERENCES core.domain,
    phase text NOT NULL CHECK (phase IN ('preliminary', 'detail')),
    status text NOT NULL CHECK (status IN ('classified', 'unknown', 'fetch_failed')),
    model text NOT NULL,
    prompt_version text NOT NULL,
    taxonomy_version text NOT NULL CHECK (taxonomy_version = 'v1'),
    tags jsonb NOT NULL CHECK (jsonb_typeof(tags) = 'array'),
    evidence jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    checked_at timestamptz NOT NULL,
    result_hash text NOT NULL UNIQUE
);
-- One successful pass per phase; unknown/failed results remain auditable.
CREATE UNIQUE INDEX IF NOT EXISTS tag_once_per_phase
    ON core.tag_result (domain_id, phase) WHERE status = 'classified';
CREATE INDEX IF NOT EXISTS tag_history ON core.tag_result (domain_id, imported_at DESC);
CREATE OR REPLACE VIEW mart.current_tag AS
SELECT DISTINCT ON (domain_id) * FROM core.tag_result
WHERE status = 'classified'
ORDER BY domain_id, (phase = 'detail') DESC, imported_at DESC, id DESC;
