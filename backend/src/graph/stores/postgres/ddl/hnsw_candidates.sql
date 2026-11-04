-- The embedding models with more than :threshold search vectors of one
-- dimension in a single project: the populations that deserve an HNSW index
-- (unscoped). The server quotes the model literal; a model several projects
-- qualify for is listed once per project.
WITH populations AS (
    SELECT
        n.embedding_model AS model_id,
        quote_literal(n.embedding_model) AS model_literal,
        md5(n.embedding_model) AS model_hash,
        vector_dims(n.embedding) AS dim
    FROM graph.node AS n
    WHERE
        n.kind = 'spec'
        AND n.embedding IS NOT NULL
        AND n.embedding_model IS NOT NULL
    GROUP BY n.group_id, n.embedding_model, vector_dims(n.embedding)
    HAVING count(*) > :threshold
)

SELECT
    model_id,
    model_literal,
    model_hash,
    dim
FROM populations
ORDER BY model_id COLLATE "C", dim
