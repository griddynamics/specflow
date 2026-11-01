-- Delete one relationship of a type by its edge_key. Parameters: project_id, rel_type, key.
WITH deleted AS (
    DELETE FROM graph.edge AS e
    WHERE e.group_id = :project_id AND e.type = :rel_type AND e.edge_key = :key
    RETURNING e.id
)

SELECT count(*) AS deleted FROM deleted
