-- The relationship types of one spec node of the project.
SELECT coalesce(
    array_agg(DISTINCT e.type ORDER BY e.type) FILTER (WHERE e.type IS NOT NULL),
    CAST(ARRAY[] AS text [])
) AS types
FROM graph.node AS n
LEFT JOIN graph.edge AS e ON n.id = e.source_id OR n.id = e.target_id
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = :n_key
    AND CAST(:n_label AS text) = any(n.labels)
