-- HNSW page (port of search_nodes_vector.cypher). Params: project_id, model, vector, labels, where, limit, k.
-- Slot DIM: the dimension of the partial halfvec index (<= 4000), rendered by the adapter from the model's dim.
-- Needs `SET LOCAL hnsw.iterative_scan = relaxed_order` and `SET LOCAL hnsw.max_scan_tuples` in the transaction.
-- Index (created per model, see hnsw_index.sql) matches the WHERE embedding_model = model predicate literally,
-- so the model must be a literal in the statement for the planner to pick it (see postgres.md, HNSW).
-- Row: fetched (rows of the k-page), hits (jsonb list {uuid, spec_id, score}) of the project, best first.
-- The adapter runs it with SET LOCAL plan_cache_mode = force_custom_plan, hnsw.iterative_scan, hnsw.ef_search,
-- hnsw.max_scan_tuples (graph.stores.postgres.dialect; the generic-plan trap, spike section 4).
-- NOTE relaxed_order is approximately ordered: the outer ORDER BY re-sorts the k rows exactly.
WITH page AS (
  SELECT n.uuid, n.spec_id,
         (2 - (CAST(n.embedding AS halfvec(/*__DIM__*/3072/*__DIM__*/)) <=> CAST(CAST(:vector AS vector) AS halfvec(/*__DIM__*/3072/*__DIM__*/)))) / 2 AS score
  FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.embedding_model = :model
    AND n.spec_id IS NOT NULL AND n.labels && CAST(:labels AS text[])
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_each(CAST(:where AS jsonb)) w
      WHERE NOT (coalesce(n.props -> w.key = w.value, false) OR (n.props -> w.key IS NULL AND w.value = 'null'::jsonb)))
  ORDER BY CAST(n.embedding AS halfvec(/*__DIM__*/3072/*__DIM__*/)) <=> CAST(CAST(:vector AS vector) AS halfvec(/*__DIM__*/3072/*__DIM__*/))
  LIMIT :k
)
SELECT (SELECT count(*) FROM page) AS fetched,
       coalesce((SELECT jsonb_agg(jsonb_build_object('uuid', t.uuid, 'spec_id', t.spec_id, 'score', t.score) ORDER BY t.score DESC, t.spec_id COLLATE "C")
                 FROM (SELECT * FROM page WHERE score <> 'NaN'::float8 ORDER BY score DESC, spec_id COLLATE "C" LIMIT :limit) t), '[]'::jsonb) AS hits;
