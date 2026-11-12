-- Port of core/queries/read_node_vectors.cypher: the stored search vectors of
-- the spec nodes of :uuids made by :model (similar, expand: no embedding
-- call); a node without one is left out. Parameters: uuids (text[]), model.
WITH found AS (
    SELECT
        n.uuid,
        n.spec_id,
        cast(n.embedding AS real[]) AS embedding
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = any(cast(:uuids AS text[]))
        AND n.spec_id IS NOT NULL
        AND n.embedding IS NOT NULL
        AND n.embedding_model = :model
)

SELECT
    uuid,
    spec_id,
    embedding
FROM found
ORDER BY spec_id COLLATE "C"
