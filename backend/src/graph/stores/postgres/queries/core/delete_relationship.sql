-- Delete one relationship of a type by its edge_key.
-- Parameters: project_id, rel_type, key.
WITH deleted AS (
    DELETE FROM graph.edge
    WHERE
        edge.group_id = :project_id
        AND edge.rel_type = :rel_type
        AND edge.edge_key = :key
    RETURNING edge.id
)

SELECT count(*) AS deleted FROM deleted
