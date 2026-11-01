-- Graph store schema (design D5, D7). Idempotent; run by the application role (not a superuser) inside one
-- transaction under pg_advisory_xact_lock(hashtext('specflow.graph.ensure_schema')) by ensure_schema.
-- The vector extension is created beforehand by an administrator (checked, never created here).
-- Statements are separated by ';' at the end of a line; the schema name is rendered by the adapter.
CREATE SCHEMA IF NOT EXISTS graph;

-- Tokenisation pre-step shared by documents and query terms: hyphen and slash become spaces, so that
-- 'signed-in' and 'orders/returns' index as two words (as the Lucene standard analyzer does) instead of
-- PostgreSQL's compound lexemes, which inflate document length and document frequency.
CREATE OR REPLACE FUNCTION graph.fts_norm(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT translate($1, '-/', '  ') $$;

-- Number of tokens of a tsvector (sum of position counts); the term frequency of a lexeme is the
-- cardinality of its positions. At most 256 positions are kept per lexeme.
CREATE OR REPLACE FUNCTION graph.tsv_len(tsvector) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT coalesce(sum(cardinality(positions)), 0)::integer FROM unnest($1) $$;

-- SET n += patch: a top-level merge; a JSON null in the patch removes the key (jsonb_strip_nulls is
-- recursive, so it is not used). Nested values are kept as given.
CREATE OR REPLACE FUNCTION graph.props_merge(base jsonb, patch jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
  SELECT coalesce(jsonb_object_agg(s.k, s.v), '{}'::jsonb) FROM (
    SELECT e.key AS k, e.value AS v FROM jsonb_each(coalesce(base, '{}'::jsonb)) e
     WHERE NOT jsonb_exists(coalesce(patch, '{}'::jsonb), e.key)
    UNION ALL
    SELECT e.key, e.value FROM jsonb_each(coalesce(patch, '{}'::jsonb)) e
     WHERE jsonb_typeof(e.value) <> 'null') s
$$;

CREATE TABLE IF NOT EXISTS graph.meta (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS graph.node (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('spec', 'fragment', 'document', 'foreign')),
    labels text[] NOT NULL DEFAULT '{}',
    props jsonb NOT NULL DEFAULT '{}',
    -- key columns generated from props (the single source of truth is props)
    uuid text GENERATED ALWAYS AS (props ->> 'uuid') STORED,
    spec_id text GENERATED ALWAYS AS (props ->> 'spec_id') STORED,
    fragment_id text GENERATED ALWAYS AS (props ->> 'fragment_id') STORED,
    document_id text GENERATED ALWAYS AS (props ->> 'document_id') STORED,
    -- the search vector: untyped dimension; the model name selects the population
    embedding vector,
    embedding_model text GENERATED ALWAYS AS (props ->> 'embedding_model') STORED,
    -- BM25 inputs: the 'simple' configuration; the tsvector keeps positions (tf), *_len is dl
    name_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'name', '')))) STORED,
    name_len integer GENERATED ALWAYS AS (graph.tsv_len(to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'name', ''))))) STORED,
    stmt_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'statement_text', '')))) STORED,
    stmt_len integer GENERATED ALWAYS AS (graph.tsv_len(to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'statement_text', ''))))) STORED,
    text_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'text', '')))) STORED,
    text_len integer GENERATED ALWAYS AS (graph.tsv_len(to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'text', ''))))) STORED
);

CREATE UNIQUE INDEX IF NOT EXISTS node_spec_uuid_uq ON graph.node (group_id, uuid) WHERE kind = 'spec';

CREATE UNIQUE INDEX IF NOT EXISTS node_spec_specid_uq ON graph.node (group_id, spec_id) WHERE kind = 'spec' AND spec_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS node_fragment_uq ON graph.node (group_id, fragment_id) WHERE kind = 'fragment';

CREATE UNIQUE INDEX IF NOT EXISTS node_document_uq ON graph.node (group_id, document_id) WHERE kind = 'document';

CREATE UNIQUE INDEX IF NOT EXISTS node_foreign_uq ON graph.node (group_id, (labels[1]), uuid) WHERE kind = 'foreign';

CREATE INDEX IF NOT EXISTS node_group_kind_idx ON graph.node (group_id, kind);

CREATE INDEX IF NOT EXISTS node_identity_key_idx ON graph.node (group_id, (props ->> 'identity_key')) WHERE kind = 'spec';

CREATE TABLE IF NOT EXISTS graph.edge (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id text NOT NULL,
    type text NOT NULL,
    source_id bigint NOT NULL REFERENCES graph.node (id) ON DELETE CASCADE,
    target_id bigint NOT NULL REFERENCES graph.node (id) ON DELETE CASCADE,
    props jsonb NOT NULL DEFAULT '{}',
    edge_key text GENERATED ALWAYS AS (props ->> 'edge_key') STORED
);

-- A relationship is found by (group_id, type, edge_key); an edge without a key never conflicts (NULLs are distinct).
CREATE UNIQUE INDEX IF NOT EXISTS edge_key_uq ON graph.edge (group_id, type, edge_key);

CREATE INDEX IF NOT EXISTS edge_source_idx ON graph.edge (source_id, type);

CREATE INDEX IF NOT EXISTS edge_target_idx ON graph.edge (target_id, type);

CREATE INDEX IF NOT EXISTS edge_group_type_idx ON graph.edge (group_id, type);
