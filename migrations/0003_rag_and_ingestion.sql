-- 0003_rag_and_ingestion: Phase 3 storage — the retrieval chunk store and the
-- Reconciler's staged raw lines.
--
--   rag                    — clause-aware contract chunks + embeddings (§4.4). Derived
--                            data: chunks rebuild from the corpus via `make embed`, and
--                            re-seeding cascades them away (FK to label.contracts). Not
--                            part of the world fingerprint. NOT in the SQL tool's
--                            allowlist — agents reach chunks through search_contracts /
--                            read_clause, never raw SQL.
--   staging.ingested_lines — parsed statement lines from `ingest_statement`. Agents
--                            propose, humans approve (invariant 5): agent-parsed lines
--                            live here and never touch label.statement_lines; promotion
--                            on batch approval is the Phase 6 review action.
--
-- pgvector: dim fixed at 384 (bge-small-en-v1.5, §4.4) at migration time per §9. The
-- ivfflat index is deliberately NOT created here — it is built by the embed job after
-- bulk insert (then ANALYZE), per the §9 pitfall; an ivfflat index trained on an empty
-- table has useless centroids.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA rag;

CREATE TABLE rag.contract_chunks (
    contract_id     BIGINT NOT NULL REFERENCES label.contracts (id) ON DELETE CASCADE,
    clause_no       TEXT NOT NULL,   -- '§1'..'§8' / '§A1','§A2','§A9' / 'title'
    part            INTEGER NOT NULL DEFAULT 0,  -- oversize clauses split into parts
    heading         TEXT NOT NULL,   -- the rendered clause heading line
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,   -- sha256(content) — the embed job's idempotency key
    artist_id       BIGINT NOT NULL REFERENCES label.artists (id),
    kind            TEXT NOT NULL CHECK (kind IN ('base', 'amendment')),
    effective_from  DATE NOT NULL,
    effective_to    DATE,
    -- Heading hits outrank body hits (setweight A vs B) in ts_rank_cd.
    tsv             tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', heading), 'A') ||
                        setweight(to_tsvector('english', content), 'B')
                    ) STORED,
    embedding       vector(384),
    embedding_model TEXT,            -- which embedder produced `embedding` (NULL = none yet)
    PRIMARY KEY (contract_id, clause_no, part)
);

CREATE INDEX contract_chunks_tsv_idx ON rag.contract_chunks USING GIN (tsv);
CREATE INDEX contract_chunks_artist_idx ON rag.contract_chunks (artist_id);

CREATE TABLE staging.ingested_lines (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    statement_id    BIGINT NOT NULL REFERENCES label.statements (id),
    period          TEXT NOT NULL,   -- the line's own period (bleed detection intact)
    isrc            TEXT NOT NULL,   -- '' for physical (release-level) lines
    upc             TEXT,
    store           TEXT NOT NULL,
    territory       TEXT NOT NULL,
    units           BIGINT NOT NULL,
    gross_amount    NUMERIC(18,6) NOT NULL,
    currency        TEXT NOT NULL,
    line_hash       TEXT NOT NULL,   -- recomputed with datagen's formula from parsed values
    ingested_by_run UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ingested_lines_statement_idx ON staging.ingested_lines (statement_id);
