-- BM25 replacement of core/queries/search_nodes_fulltext.cypher (Lucene BM25Similarity, per field, summed over
-- name + statement_text). Params: project_id, terms (text[], OR semantics), labels, where (jsonb), skip, page.
-- Slots K1 (1.2) and B (0.75). Row: fetched (rows of this page, already project-filtered), hits (jsonb list
-- {uuid, spec_id, score}) in score order, ties by spec_id then uuid; the reader loop (skip += fetched,
-- page *= 2, stop on fetched < page) works unchanged.
-- idf = ln(1 + (N - df + 0.5) / (df + 0.5)); term score = idf * tf / (tf + k1 * (1 - b + b * dl / avgdl)).
-- N, df, avgdl are per project and per field over docs that have the field (Lucene docCount).
-- Query terms are tokenised with the same fts_norm + 'simple' config as the documents, duplicates collapse.
WITH q AS (
  SELECT DISTINCT u.lexeme FROM unnest(to_tsvector('simple', graph.fts_norm(array_to_string(CAST(:terms AS text[]), ' ')))) u
), corpus AS (
  SELECT n.id, n.uuid, n.spec_id, n.labels, n.props, n.name_tsv, n.name_len, n.stmt_tsv, n.stmt_len
  FROM graph.node n WHERE n.group_id = :project_id AND n.kind = 'spec'
), fs AS (
  SELECT count(*) FILTER (WHERE name_len > 0) AS n_name, avg(name_len) FILTER (WHERE name_len > 0) AS avg_name,
         count(*) FILTER (WHERE stmt_len > 0) AS n_stmt, avg(stmt_len) FILTER (WHERE stmt_len > 0) AS avg_stmt
  FROM corpus
), name_tf AS (
  SELECT c.id, u.lexeme, cardinality(u.positions) AS tf, c.name_len AS dl
  FROM corpus c CROSS JOIN LATERAL unnest(c.name_tsv) u WHERE u.lexeme IN (SELECT lexeme FROM q)
), stmt_tf AS (
  SELECT c.id, u.lexeme, cardinality(u.positions) AS tf, c.stmt_len AS dl
  FROM corpus c CROSS JOIN LATERAL unnest(c.stmt_tsv) u WHERE u.lexeme IN (SELECT lexeme FROM q)
), contrib AS (
  SELECT t.id, ln(1 + (fs.n_name - d.df + 0.5) / (d.df + 0.5))
               * t.tf / (t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (1 - /*__BM25_B__*/0.75/*__BM25_B__*/ + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avg_name)) AS s
  FROM name_tf t JOIN (SELECT lexeme, count(*) AS df FROM name_tf GROUP BY lexeme) d USING (lexeme), fs
  UNION ALL
  SELECT t.id, ln(1 + (fs.n_stmt - d.df + 0.5) / (d.df + 0.5))
               * t.tf / (t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (1 - /*__BM25_B__*/0.75/*__BM25_B__*/ + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avg_stmt)) AS s
  FROM stmt_tf t JOIN (SELECT lexeme, count(*) AS df FROM stmt_tf GROUP BY lexeme) d USING (lexeme), fs
), scored AS (
  SELECT id, sum(s) AS score FROM contrib GROUP BY id
), page AS (
  SELECT c.uuid, c.spec_id, sc.score, row_number() OVER (ORDER BY sc.score DESC, c.spec_id COLLATE "C", c.uuid COLLATE "C") AS rn
  FROM scored sc JOIN corpus c ON c.id = sc.id
  WHERE c.spec_id IS NOT NULL AND c.labels && CAST(:labels AS text[])
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_each(CAST(:where AS jsonb)) w
      WHERE NOT (coalesce(c.props -> w.key = w.value, false) OR (c.props -> w.key IS NULL AND w.value = 'null'::jsonb)))
  ORDER BY sc.score DESC, c.spec_id COLLATE "C", c.uuid COLLATE "C"
  OFFSET :skip LIMIT :page
)
SELECT count(*) AS fetched,
       coalesce(jsonb_agg(jsonb_build_object('uuid', uuid, 'spec_id', spec_id, 'score', score) ORDER BY rn), '[]'::jsonb) AS hits
FROM page;
