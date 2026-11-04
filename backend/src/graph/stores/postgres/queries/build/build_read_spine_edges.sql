-- Spine reachability and realization walks: the edges of :relations between
-- spec nodes of the project, with their basis and, for an inferred edge, the
-- rule that wrote it.
WITH found AS (
    SELECT
        r.rel_type AS relation,
        s.uuid AS source,
        t.uuid AS target,
        coalesce(r.props ->> 'basis', 'stated') AS basis,
        r.props ->> 'inference_rule' AS inference_rule
    FROM graph.edge AS r
    INNER JOIN graph.node AS s ON r.source_id = s.id
    INNER JOIN graph.node AS t ON r.target_id = t.id
    WHERE
        s.group_id = :project_id
        AND s.kind = 'spec'
        AND s.uuid IS NOT NULL
        AND t.kind = 'spec'
        AND r.rel_type = any(cast(:relations AS text[]))
        AND r.group_id = :project_id
        AND t.group_id = :project_id
)

SELECT
    relation,
    source,
    target,
    basis,
    inference_rule
FROM found
ORDER BY relation COLLATE "C", source COLLATE "C", target COLLATE "C"
