-- 0002_world_schemas: the four business schemas from BUILD_PLAN §3.3.
--
--   label   — the operational label database agents read (catalog, contracts, statements)
--   staging — gated write path: agents propose here, humans approve (invariant 5)
--   truth   — the answer key; excluded from the SQL tool's allowlist at parser level
--             (invariant 3, enforced + tested in Phase 3)
--   app     — platform runtime (sessions, runs, spans, notes, eval results)
--
-- Money is NUMERIC(18,6) everywhere; FX rates carry NUMERIC(18,8) (invariant 1).
-- World tables use explicit BIGINT ids assigned deterministically by datagen — no
-- sequences — so the same WORLD_SEED yields byte-identical content. Runtime tables
-- (staging/app) are created at runtime and use UUIDs/sequences freely.

CREATE SCHEMA label;
CREATE SCHEMA staging;
CREATE SCHEMA truth;
CREATE SCHEMA app;

-- ── label ────────────────────────────────────────────────────────────────────

CREATE TABLE label.artists (
    id         BIGINT PRIMARY KEY,
    stage_name TEXT NOT NULL UNIQUE,
    legal_name TEXT NOT NULL,
    joined_at  DATE NOT NULL,
    country    TEXT NOT NULL
);

CREATE TABLE label.releases (
    id           BIGINT PRIMARY KEY,
    upc          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    imprint      TEXT NOT NULL,
    release_date DATE NOT NULL
);

CREATE TABLE label.tracks (
    id                BIGINT PRIMARY KEY,
    isrc              TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    primary_artist_id BIGINT NOT NULL REFERENCES label.artists (id),
    duration_s        INTEGER NOT NULL CHECK (duration_s > 0)
);

CREATE TABLE label.release_tracks (
    release_id BIGINT NOT NULL REFERENCES label.releases (id),
    track_id   BIGINT NOT NULL REFERENCES label.tracks (id),
    position   INTEGER NOT NULL CHECK (position > 0),
    PRIMARY KEY (release_id, track_id),
    UNIQUE (release_id, position)
);

CREATE TABLE label.contracts (
    id             BIGINT PRIMARY KEY,
    artist_id      BIGINT NOT NULL REFERENCES label.artists (id),
    doc_path       TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to   DATE,
    kind           TEXT NOT NULL CHECK (kind IN ('base', 'amendment'))
);

CREATE TABLE label.contract_terms (
    contract_id BIGINT PRIMARY KEY REFERENCES label.contracts (id),
    terms       JSONB NOT NULL
);

CREATE TABLE label.amendments (
    amendment_id           BIGINT PRIMARY KEY REFERENCES label.contracts (id),
    supersedes_contract_id BIGINT NOT NULL REFERENCES label.contracts (id),
    replaced_sections      TEXT[] NOT NULL
);

CREATE TABLE label.advances (
    id          BIGINT PRIMARY KEY,
    artist_id   BIGINT NOT NULL REFERENCES label.artists (id),
    contract_id BIGINT NOT NULL REFERENCES label.contracts (id),
    amount      NUMERIC(18,6) NOT NULL CHECK (amount >= 0),
    currency    TEXT NOT NULL,
    granted_at  DATE NOT NULL
);

CREATE TABLE label.expenses (
    id          BIGINT PRIMARY KEY,
    artist_id   BIGINT NOT NULL REFERENCES label.artists (id),
    class       TEXT NOT NULL,
    amount      NUMERIC(18,6) NOT NULL CHECK (amount >= 0),
    currency    TEXT NOT NULL,
    incurred_at DATE NOT NULL,
    recoupable  BOOLEAN NOT NULL
);

-- One row per recoupment account. xcollat_group_id is the account key referenced by
-- contract terms JSON (advances_recoupment.account): a cross-collateralized artist's
-- deals share one key; independent deals get one key each (see D-002).
CREATE TABLE label.recoup_accounts (
    artist_id        BIGINT NOT NULL REFERENCES label.artists (id),
    xcollat_group_id TEXT NOT NULL UNIQUE,
    opening_balance  NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (opening_balance >= 0),
    PRIMARY KEY (artist_id, xcollat_group_id)
);

CREATE TABLE label.distributors (
    id      BIGINT PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    dialect TEXT NOT NULL
);

CREATE TABLE label.statements (
    id             BIGINT PRIMARY KEY,
    distributor_id BIGINT NOT NULL REFERENCES label.distributors (id),
    period         TEXT NOT NULL CHECK (period ~ '^\d{4}-\d{2}$'),
    received_at    DATE NOT NULL,
    raw_path       TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('received', 'ingested')),
    UNIQUE (distributor_id, period)
);

-- statement_lines.isrc is deliberately NOT a foreign key: the unknown_isrc anomaly
-- requires lines that reference no catalog track (§3.4).
CREATE TABLE label.statement_lines (
    id           BIGINT PRIMARY KEY,
    statement_id BIGINT NOT NULL REFERENCES label.statements (id),
    period       TEXT NOT NULL,
    isrc         TEXT NOT NULL,
    upc          TEXT,
    store        TEXT NOT NULL,
    territory    TEXT NOT NULL,
    units        BIGINT NOT NULL,
    gross_amount NUMERIC(18,6) NOT NULL,
    currency     TEXT NOT NULL,
    line_hash    TEXT NOT NULL
);

CREATE INDEX statement_lines_period_idx ON label.statement_lines (period);
CREATE INDEX statement_lines_isrc_period_idx ON label.statement_lines (isrc, period);
CREATE INDEX statement_lines_statement_idx ON label.statement_lines (statement_id);

CREATE TABLE label.fx_rates (
    period   TEXT NOT NULL CHECK (period ~ '^\d{4}-\d{2}$'),
    currency TEXT NOT NULL,
    usd_rate NUMERIC(18,8) NOT NULL CHECK (usd_rate > 0),
    PRIMARY KEY (period, currency)
);

-- "Platform dashboard" reference numbers for discrepancy checks (dashboard_gap anomaly).
CREATE TABLE label.dashboard_streams (
    period  TEXT NOT NULL,
    isrc    TEXT NOT NULL,
    store   TEXT NOT NULL,
    streams BIGINT NOT NULL,
    PRIMARY KEY (period, isrc, store)
);

-- ── staging (agents propose; humans approve — invariant 5) ───────────────────

CREATE TABLE staging.statement_batches (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period           TEXT NOT NULL,
    submitted_by_run UUID,
    status           TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'approved', 'rejected')),
    summary          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE staging.proposed_allocations (
    batch_id    BIGINT NOT NULL REFERENCES staging.statement_batches (id),
    artist_id   BIGINT NOT NULL,
    period      TEXT NOT NULL,
    line_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    net_payable NUMERIC(18,6) NOT NULL,
    PRIMARY KEY (batch_id, artist_id)
);

CREATE TABLE staging.flags (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES staging.statement_batches (id),
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload  JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ── truth (the answer key — agents can never read this schema) ───────────────

CREATE TABLE truth.expected_ledger (
    artist_id     BIGINT NOT NULL,
    period        TEXT NOT NULL,
    gross         NUMERIC(18,6) NOT NULL,
    recouped      NUMERIC(18,6) NOT NULL,
    net_payable   NUMERIC(18,6) NOT NULL,
    balance_after NUMERIC(18,6) NOT NULL,
    PRIMARY KEY (artist_id, period)
);

CREATE TABLE truth.anomaly_registry (
    id                 BIGINT PRIMARY KEY,
    kind               TEXT NOT NULL,
    statement_line_id  BIGINT NOT NULL,
    expected_flag_kind TEXT,  -- NULL = borderline case: correct behavior is NOT flagging
    note               TEXT NOT NULL
);

CREATE TABLE truth.qa_answer_key (
    question_id TEXT PRIMARY KEY,
    answer      JSONB NOT NULL,
    tolerance   NUMERIC(18,6),
    category    TEXT NOT NULL
);

-- ── app (platform runtime; populated from Phase 2 on) ────────────────────────

CREATE TABLE app.sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app.messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES app.sessions (id),
    role       TEXT NOT NULL,
    content    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app.runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  UUID REFERENCES app.sessions (id),
    agent       TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    cost_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE app.spans (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     UUID NOT NULL REFERENCES app.runs (id),
    parent_id  UUID REFERENCES app.spans (id),
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at   TIMESTAMPTZ,
    attrs      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX spans_run_idx ON app.spans (run_id);

CREATE TABLE app.notes (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_ref TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_by UUID REFERENCES app.runs (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX notes_entity_idx ON app.notes (entity_ref);

CREATE TABLE app.eval_runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_hash  TEXT NOT NULL,
    model       TEXT NOT NULL,
    git_sha     TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    summary     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE app.eval_results (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    eval_run_id UUID NOT NULL REFERENCES app.eval_runs (id),
    question_id TEXT NOT NULL,
    tier        TEXT NOT NULL,
    score       NUMERIC(8,4),
    passed      BOOLEAN,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
);
