-- Port of core/queries/search_nodes_vector_exact.cypher. Params: project_id, model, vector (text '[..]'),
-- labels (text[]), where (jsonb object, equality on props, JSON null = property absent), limit.
-- Rows: uuid, spec_id, score = (1 + cos) / 2 = (2 - (a <=> b)) / 2, best first, ties by spec_id.
SELECT d.uuid, d.spec_id, d.score
FROM (
  SELECT n.uuid, n.spec_id,
         CASE WHEN vector_dims(n.embedding) = vector_dims(CAST(:vector AS vector))
              THEN (2 - (n.embedding <=> CAST(:vector AS vector))) / 2 END AS score
  FROM graph.node n
  WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.embedding IS NOT NULL AND n.embedding_model = :model
    AND n.spec_id IS NOT NULL AND n.labels && CAST(:labels AS text[])
    AND NOT EXISTS (
      SELECT 1 FROM jsonb_each(CAST(:where AS jsonb)) w
      WHERE NOT (coalesce(n.props -> w.key = w.value, false) OR (n.props -> w.key IS NULL AND w.value = 'null'::jsonb)))
) d
WHERE d.score IS NOT NULL AND d.score <> 'NaN'::float8
ORDER BY d.score DESC, d.spec_id COLLATE "C"
LIMIT :limit;
