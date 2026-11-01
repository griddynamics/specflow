-- Repair: delete spec nodes addressed by id (text of the node id), with their
-- relationships. Parameter ids: text[]. written counts the deleted nodes.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'spec'
        AND cast(node.id AS text) = any(cast(:ids AS text[]))
    RETURNING node.id
)

SELECT count(*) AS written FROM deleted
