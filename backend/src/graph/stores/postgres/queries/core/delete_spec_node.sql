-- Delete one spec node of the project by uuid when it carries the domain
-- label; its edges cascade.
WITH deleted AS (
    DELETE FROM graph.node
    WHERE
        node.group_id = :project_id
        AND node.kind = 'spec'
        AND node.uuid = :key
        AND CAST(:label AS text) = ANY(node.labels)
    RETURNING node.id
)

SELECT COUNT(*) AS deleted FROM deleted
