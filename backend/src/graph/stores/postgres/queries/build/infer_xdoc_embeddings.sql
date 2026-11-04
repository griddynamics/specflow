-- Infer (cross_document): the embedding of each node of :uuids, read once for
-- every hub's member pairs (their cosines are computed in Python); a node
-- missing or without an embedding is left out. Parameters: uuids (text[]).
WITH found AS (
    SELECT
        n.uuid,
        cast(n.embedding AS real []) AS embedding
    FROM unnest(cast(:uuids AS text[])) AS w (node_uuid)
    INNER JOIN graph.node AS n
        ON
            n.group_id = :project_id
            AND n.kind = 'spec'
            AND w.node_uuid = n.uuid
    WHERE n.embedding IS NOT NULL
)

SELECT
    uuid,
    embedding
FROM found
ORDER BY uuid COLLATE "C"
