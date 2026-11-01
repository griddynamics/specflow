-- Delete one spec node of the project by uuid when it carries the domain
-- label; its edges cascade.
WITH deleted AS (
    DELETE FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = :key
        AND CAST(:label AS text) = ANY(n.labels)
    RETURNING n.id
)

SELECT COUNT(*) AS deleted FROM deleted
