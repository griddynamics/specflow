-- Port of core/queries/search_nodes_vector_exact.cypher. Parameters:
-- project_id, model, vector (text '[..]'), labels (text[]), where (jsonb
-- object: equality on props, JSON null = property absent), limit.
-- Rows: uuid, spec_id and score = (1 + cos) / 2 = (2 - (a <=> b)) / 2, best
-- first, ties by spec_id. A vector of another size, or a zero vector (whose
-- distance is NaN), never matches.
WITH scored AS (
    SELECT
        n.uuid,
        n.spec_id,
        CASE
            WHEN vector_dims(n.embedding) = vector_dims(cast(:vector AS vector))
                THEN (2 - (n.embedding <=> cast(:vector AS vector))) / 2
        END AS score
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.embedding IS NOT NULL
        AND n.embedding_model = :model
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:labels AS text[])
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_each(cast(:where AS jsonb)) AS w
            WHERE NOT (
                coalesce(n.props -> w.key = w.value, FALSE)
                OR (
                    n.props -> w.key IS NULL
                    AND w.value = cast('null' AS jsonb)
                )
            )
        )
)

SELECT
    uuid,
    spec_id,
    score
FROM scored
WHERE score IS NOT NULL AND score <> cast('NaN' AS float8)
ORDER BY score DESC, spec_id COLLATE "C"
LIMIT :limit
