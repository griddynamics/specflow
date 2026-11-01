-- The relationship types of one spec node of the project.
SELECT
    coalesce(
        array_agg(DISTINCT e.rel_type ORDER BY e.rel_type)
        FILTER (WHERE e.rel_type IS NOT NULL),
        cast(ARRAY[] AS text[])
    ) AS types
FROM graph.node AS n
LEFT JOIN graph.edge AS e ON n.id = e.source_id OR n.id = e.target_id
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = :n_key
    AND cast(:n_label AS text) = any(n.labels)
