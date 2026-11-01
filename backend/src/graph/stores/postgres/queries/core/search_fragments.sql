-- BM25 replacement of core/queries/search_fragments.cypher over Fragment(text). Params: project_id, terms (text[]),
-- active (status value), document_id (nullable text), skip, page. Slots K1, B as in search_nodes_fulltext.
-- Row: fetched, hits (jsonb list {fragment_id, score}). Statistics are over ALL fragments of the project
-- (any status), as the Lucene index covers every fragment node.
WITH q AS (
  SELECT DISTINCT u.lexeme FROM unnest(to_tsvector('simple', graph.fts_norm(array_to_string(CAST(:terms AS text[]), ' ')))) u
), corpus AS (
  SELECT n.id, n.fragment_id, n.props, n.text_tsv, n.text_len
  FROM graph.node n WHERE n.group_id = :project_id AND n.kind = 'fragment'
), fs AS (
  SELECT count(*) FILTER (WHERE text_len > 0) AS n_docs, avg(text_len) FILTER (WHERE text_len > 0) AS avgdl FROM corpus
), tf AS (
  SELECT c.id, u.lexeme, cardinality(u.positions) AS tf, c.text_len AS dl
  FROM corpus c CROSS JOIN LATERAL unnest(c.text_tsv) u WHERE u.lexeme IN (SELECT lexeme FROM q)
), scored AS (
  SELECT t.id, sum(ln(1 + (fs.n_docs - d.df + 0.5) / (d.df + 0.5))
                   * t.tf / (t.tf + /*__BM25_K__*/1.2/*__BM25_K__*/ * (1 - /*__BM25_B__*/0.75/*__BM25_B__*/ + /*__BM25_B__*/0.75/*__BM25_B__*/ * t.dl / fs.avgdl))) AS score
  FROM tf t JOIN (SELECT lexeme, count(*) AS df FROM tf GROUP BY lexeme) d USING (lexeme), fs
  GROUP BY t.id
), page AS (
  SELECT c.fragment_id, sc.score, row_number() OVER (ORDER BY sc.score DESC, c.fragment_id COLLATE "C") AS rn
  FROM scored sc JOIN corpus c ON c.id = sc.id
  WHERE c.fragment_id IS NOT NULL AND c.props ->> 'status' = :active
    AND (CAST(:document_id AS text) IS NULL OR c.props ->> 'document_id' = CAST(:document_id AS text))
  ORDER BY sc.score DESC, c.fragment_id COLLATE "C"
  OFFSET :skip LIMIT :page
)
SELECT count(*) AS fetched,
       coalesce(jsonb_agg(jsonb_build_object('fragment_id', fragment_id, 'score', score) ORDER BY rn), '[]'::jsonb) AS hits
FROM page;
