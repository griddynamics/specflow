-- One breadth-first step: spec nodes one domain edge (either direction) from :frontier that
-- are not in :seen, the first :limit by spec_id.
WITH steps AS (
    SELECT
        e.target_id AS other_id
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.source_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(CAST(:frontier AS text []))
        AND e.group_id = :project_id
        AND e.type = any(CAST(:relations AS text []))
    UNION
    SELECT
        e.source_id AS other_id
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.target_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(CAST(:frontier AS text []))
        AND e.group_id = :project_id
        AND e.type = any(CAST(:relations AS text []))
)

SELECT
    m.uuid,
    m.spec_id
FROM graph.node AS m
WHERE
    m.id IN (SELECT steps.other_id FROM steps)
    AND m.group_id = :project_id
    AND m.kind = 'spec'
    AND m.spec_id IS NOT NULL
    AND NOT (m.uuid = any(CAST(:seen AS text [])))
    AND m.labels && CAST(:labels AS text [])
ORDER BY m.spec_id COLLATE "C"
LIMIT :limit
