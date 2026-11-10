-- Graph store schema (design D5, D7). Idempotent; ensure_schema runs it as
-- the application role (not a superuser) inside one transaction, under
-- pg_advisory_xact_lock(hashtext('specflow.graph.ensure_schema')).
-- The vector extension is created beforehand by an administrator (checked,
-- never created here). Statements end with ';' at the end of a line; the
-- adapter renders the schema name.
CREATE SCHEMA IF NOT EXISTS graph;

-- Tokenisation pre-step shared by documents and query terms, so that the
-- 'simple' parser splits words as the Lucene standard analyzer does:
-- hyphen and slash become spaces ('signed-in', 'orders/returns' are two
-- words, not PostgreSQL's compound lexemes); an underscore, an apostrophe
-- between letters and a comma between digits join their neighbours into one
-- token (a letter that no text holds stands in for them).
CREATE OR REPLACE FUNCTION graph.fts_norm(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$
  SELECT translate(
    regexp_replace(
      regexp_replace($1, '(\w)[''’](?=\w)', E'\\1\u01c2', 'g'),
      '(\d),(?=\d)', E'\\1\u01c2', 'g'),
    '-/_', E'  \u01c2')
$$;

-- Number of tokens of a tsvector (sum of position counts); the term
-- frequency of a lexeme is the cardinality of its positions. At most 256
-- positions are kept per lexeme.
CREATE OR REPLACE FUNCTION graph.tsv_len(tsvector) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$
  SELECT coalesce(sum(cardinality(positions)), 0)::integer FROM unnest($1)
$$;

-- SET n += patch: a top-level merge; a JSON null in the patch removes the
-- key (jsonb_strip_nulls is recursive, so it is not used) and none is kept
-- from the base either. Nested values are kept as given.
CREATE OR REPLACE FUNCTION graph.props_merge(base jsonb, patch jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
  SELECT coalesce(jsonb_object_agg(s.k, s.v), '{}'::jsonb) FROM (
    SELECT e.key AS k, e.value AS v FROM jsonb_each(coalesce(base, '{}'::jsonb)) e
     WHERE NOT jsonb_exists(coalesce(patch, '{}'::jsonb), e.key)
       AND jsonb_typeof(e.value) <> 'null'
    UNION ALL
    SELECT e.key, e.value FROM jsonb_each(coalesce(patch, '{}'::jsonb)) e
     WHERE jsonb_typeof(e.value) <> 'null') s
$$;

-- SET += of several patches in order: the last patch naming a key decides;
-- a JSON null stays (props_merge removes the key where it is applied).
CREATE OR REPLACE FUNCTION graph.props_fold(patches jsonb[]) RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
  SELECT coalesce(jsonb_object_agg(l.k, l.v), '{}'::jsonb) FROM (
    SELECT DISTINCT ON (e.key) e.key AS k, e.value AS v
    FROM unnest(patches) WITH ORDINALITY AS p(patch, ord), jsonb_each(p.patch) AS e
    ORDER BY e.key, p.ord DESC
  ) l
$$;

-- The document length as Lucene scores with it: its BM25 reads the length
-- from a one-byte norm (SmallFloat.intToByte4), which is exact below 32
-- tokens and truncates a longer length to the lower edge of its bucket of 3
-- significant bits. The average length uses the true lengths.
CREATE OR REPLACE FUNCTION graph.lucene_len(len integer) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$
  SELECT CASE
    WHEN len < 32 THEN len
    ELSE ((((len - 24) >> v.s) & 7 | 8) << v.s) + 24
  END
  FROM (SELECT floor(log(2, (len - 24)::numeric))::int - 3 AS s) v
$$;

-- Tokenizer gaps against Lucene (docs/configuration.md, PostgreSQL): e-mail
-- addresses, URLs and paths split differently, lower-casing follows the
-- database locale, a tsvector keeps at most 256 positions per word and is
-- limited to 1 MB (a longer text fails the write; there is no length cap).
CREATE TABLE IF NOT EXISTS graph.meta (
    meta_key text PRIMARY KEY,
    meta_value text NOT NULL
);

CREATE TABLE IF NOT EXISTS graph.node (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id text NOT NULL,
    kind text NOT NULL CHECK (
        kind IN ('spec', 'fragment', 'document', 'foreign')
    ),
    labels text[] NOT NULL DEFAULT '{}',
    props jsonb NOT NULL DEFAULT '{}',
    -- key columns generated from props (the single source of truth is props)
    uuid text GENERATED ALWAYS AS (props ->> 'uuid') STORED,
    spec_id text GENERATED ALWAYS AS (props ->> 'spec_id') STORED,
    fragment_id text GENERATED ALWAYS AS (props ->> 'fragment_id') STORED,
    document_id text GENERATED ALWAYS AS (props ->> 'document_id') STORED,
    -- the search vector: untyped dimension; its model selects the population
    embedding vector,
    embedding_model text GENERATED ALWAYS AS (
        props ->> 'embedding_model'
    ) STORED,
    -- BM25 inputs: the 'simple' configuration; the tsvector keeps positions
    -- (tf) and *_len is the token count (dl)
    name_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'name', '')))
    ) STORED,
    name_len integer GENERATED ALWAYS AS (
        graph.tsv_len(
            to_tsvector(
                'simple', graph.fts_norm(coalesce(props ->> 'name', ''))
            )
        )
    ) STORED,
    stmt_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple', graph.fts_norm(coalesce(props ->> 'statement_text', ''))
        )
    ) STORED,
    stmt_len integer GENERATED ALWAYS AS (
        graph.tsv_len(
            to_tsvector(
                'simple',
                graph.fts_norm(coalesce(props ->> 'statement_text', ''))
            )
        )
    ) STORED,
    text_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', graph.fts_norm(coalesce(props ->> 'text', '')))
    ) STORED,
    text_len integer GENERATED ALWAYS AS (
        graph.tsv_len(
            to_tsvector(
                'simple', graph.fts_norm(coalesce(props ->> 'text', ''))
            )
        )
    ) STORED
);

CREATE UNIQUE INDEX IF NOT EXISTS node_spec_uuid_uq
ON graph.node (group_id, uuid) WHERE kind = 'spec';

CREATE UNIQUE INDEX IF NOT EXISTS node_spec_specid_uq
ON graph.node (group_id, spec_id) WHERE kind = 'spec' AND spec_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS node_fragment_uq
ON graph.node (group_id, fragment_id) WHERE kind = 'fragment';

CREATE UNIQUE INDEX IF NOT EXISTS node_document_uq
ON graph.node (group_id, document_id) WHERE kind = 'document';

CREATE UNIQUE INDEX IF NOT EXISTS node_foreign_uq
ON graph.node (group_id, (labels[1]), uuid) WHERE kind = 'foreign';

CREATE INDEX IF NOT EXISTS node_group_kind_idx ON graph.node (group_id, kind);

CREATE INDEX IF NOT EXISTS node_identity_key_idx
ON graph.node (group_id, (props ->> 'identity_key')) WHERE kind = 'spec';

-- Alias lookups (read_node_by_alias, read_node_uuids_by_spec_id,
-- read_nodes_by_ids) test the alias arrays with ? and ?|, which these
-- indexes serve; jsonb_exists() cannot use an index.
CREATE INDEX IF NOT EXISTS node_spec_id_aliases_idx
ON graph.node USING gin ((props -> 'spec_id_aliases')) WHERE kind = 'spec';

CREATE INDEX IF NOT EXISTS node_identity_aliases_idx
ON graph.node USING gin ((props -> 'identity_aliases')) WHERE kind = 'spec';

CREATE TABLE IF NOT EXISTS graph.edge (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id text NOT NULL,
    rel_type text NOT NULL,
    source_id bigint NOT NULL REFERENCES graph.node (id) ON DELETE CASCADE,
    target_id bigint NOT NULL REFERENCES graph.node (id) ON DELETE CASCADE,
    props jsonb NOT NULL DEFAULT '{}',
    edge_key text GENERATED ALWAYS AS (props ->> 'edge_key') STORED
);

-- A relationship is one row per (type, edge_key, ends): the upsert conflicts
-- on it. Like the Cypher store, the database does not forbid the same key on
-- other ends (the owned writer refuses it); an edge without a key never
-- conflicts (NULLs are distinct). A relationship is found by its key through
-- edge_key_idx.
CREATE UNIQUE INDEX IF NOT EXISTS edge_key_uq
ON graph.edge (group_id, rel_type, edge_key, source_id, target_id);

CREATE INDEX IF NOT EXISTS edge_key_idx
ON graph.edge (group_id, rel_type, edge_key);

CREATE INDEX IF NOT EXISTS edge_source_idx ON graph.edge (source_id, rel_type);

CREATE INDEX IF NOT EXISTS edge_target_idx ON graph.edge (target_id, rel_type);

CREATE INDEX IF NOT EXISTS edge_group_type_idx
ON graph.edge (group_id, rel_type);
