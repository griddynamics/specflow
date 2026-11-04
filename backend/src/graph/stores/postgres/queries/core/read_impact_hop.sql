-- One breadth-first step: spec nodes one domain edge (either direction) from
-- :frontier that are not in :seen, the first :limit by spec_id.
WITH steps AS (
    SELECT e.target_id AS other_id
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.source_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(cast(:frontier AS text[]))
        AND e.group_id = :project_id
        AND e.rel_type = any(cast(:relations AS text[]))
    UNION
    SELECT e.source_id AS other_id
    FROM graph.node AS s
    INNER JOIN graph.edge AS e ON s.id = e.target_id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid = any(cast(:frontier AS text[]))
        AND e.group_id = :project_id
        AND e.rel_type = any(cast(:relations AS text[]))
),

found AS (
    SELECT
        m.uuid,
        m.spec_id
    FROM graph.node AS m
    WHERE
        m.id IN (SELECT steps.other_id FROM steps)
        AND m.group_id = :project_id
        AND m.kind = 'spec'
        AND m.spec_id IS NOT NULL
        AND NOT (m.uuid = any(cast(:seen AS text[])))
        AND m.labels && cast(:labels AS text[])
)

SELECT
    uuid,
    spec_id
FROM found
ORDER BY spec_id COLLATE "C"
LIMIT :limit
