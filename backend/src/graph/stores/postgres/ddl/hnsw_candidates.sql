-- The embedding models with more than :threshold search vectors of one dimension in a single project:
-- the populations that deserve an HNSW index (unscoped). The model literal is quoted by the server.
SELECT
    n.embedding_model AS model,
    quote_literal(n.embedding_model) AS model_literal,
    md5(n.embedding_model) AS model_hash,
    vector_dims(n.embedding) AS dim
FROM graph.node AS n
WHERE n.kind = 'spec' AND n.embedding IS NOT NULL AND n.embedding_model IS NOT NULL
GROUP BY n.group_id, n.embedding_model, vector_dims(n.embedding)
HAVING count(*) > :threshold
ORDER BY n.embedding_model COLLATE "C", vector_dims(n.embedding)
