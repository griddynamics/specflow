-- Domain edges between spec nodes of the project, filtered by relation and label.
SELECT
    e.type AS relation,
    s.spec_id AS source,
    t.spec_id AS target,
    e.props || jsonb_build_object('group_id', e.group_id) AS props
FROM graph.edge AS e
INNER JOIN graph.node AS s ON e.source_id = s.id
INNER JOIN graph.node AS t ON e.target_id = t.id
WHERE
    e.group_id = :project_id
    AND s.group_id = :project_id
    AND t.group_id = :project_id
    AND s.kind = 'spec'
    AND t.kind = 'spec'
    AND s.spec_id IS NOT NULL
    AND e.type = any(CAST(:relations AS text []))
    AND s.labels && CAST(:labels AS text [])
    AND t.labels && CAST(:labels AS text [])
    AND (
        CAST(:include_inferred AS boolean)
        OR coalesce(e.props ->> 'basis', '') <> :inferred
    )
ORDER BY
    s.spec_id COLLATE "C",
    e.type COLLATE "C",
    t.spec_id COLLATE "C",
    e.edge_key COLLATE "C"
