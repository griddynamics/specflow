-- Repair: delete build-owned edges addressed by id (text of the edge id).
-- Parameters: ids (text[]), relations (text[]). written counts the deleted
-- edges.
WITH deleted AS (
    DELETE FROM graph.edge
    WHERE
        edge.group_id = :project_id
        AND cast(edge.id AS text) = any(cast(:ids AS text[]))
        AND edge.rel_type = any(cast(:relations AS text[]))
    RETURNING edge.id
)

SELECT count(*) AS written FROM deleted
