-- The domain edges around one spec node, with both ends.
WITH center AS (
    SELECT n.id
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.spec_id = :spec_id
),

around AS (
    SELECT
        e.id AS edge_id,
        e.target_id AS other_id
    FROM center
    INNER JOIN graph.edge AS e ON center.id = e.source_id
    UNION
    SELECT
        e.id AS edge_id,
        e.source_id AS other_id
    FROM center
    INNER JOIN graph.edge AS e ON center.id = e.target_id
)

SELECT
    e.type AS relation,
    s.spec_id AS source,
    t.spec_id AS target,
    e.props || jsonb_build_object('group_id', e.group_id) AS props
FROM around
INNER JOIN graph.edge AS e ON around.edge_id = e.id
INNER JOIN graph.node AS m ON around.other_id = m.id
INNER JOIN graph.node AS s ON e.source_id = s.id
INNER JOIN graph.node AS t ON e.target_id = t.id
WHERE
    e.type = any(CAST(:relations AS text []))
    AND e.group_id = :project_id
    AND m.group_id = :project_id
    AND m.kind = 'spec'
    AND m.labels && CAST(:labels AS text [])
ORDER BY
    s.spec_id COLLATE "C",
    e.type COLLATE "C",
    t.spec_id COLLATE "C",
    e.edge_key COLLATE "C"
