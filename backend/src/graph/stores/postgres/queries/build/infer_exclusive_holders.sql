-- Infer: the nodes of :uuids that already have a stated or implied edge of the
-- relation :rel_type on one of the exclusive pairs :pairs (a JSON list of
-- [source label, target label]).
WITH found AS (
    SELECT DISTINCT n.uuid
    FROM graph.node AS n
    INNER JOIN graph.edge AS x ON n.id = x.source_id
    INNER JOIN graph.node AS t ON x.target_id = t.id
    CROSS JOIN LATERAL jsonb_array_elements(cast(:pairs AS jsonb)) AS p (pair)
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.uuid = any(cast(:uuids AS text[]))
        AND x.rel_type = :rel_type
        AND x.group_id = :project_id
        AND coalesce(x.props ->> 'basis', 'stated')
        = any(cast(:bases AS text[]))
        AND t.group_id = :project_id
        AND t.kind = 'spec'
        AND jsonb_exists(to_jsonb(n.labels), p.pair ->> 0)
        AND jsonb_exists(to_jsonb(t.labels), p.pair ->> 1)
)

SELECT uuid
FROM found
ORDER BY uuid COLLATE "C"
