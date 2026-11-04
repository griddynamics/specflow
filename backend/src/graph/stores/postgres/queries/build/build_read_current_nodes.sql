-- Diff: the current spec nodes; the vectors are left out.
WITH found AS (
    SELECT
        n.uuid,
        array_prepend('SpecNode', n.labels) AS labels,
        (n.props - 'embedding' - 'name_embedding') || jsonb_build_object(
            'group_id', n.group_id, 'embedding', NULL, 'name_embedding', NULL
        ) AS props
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.uuid IS NOT NULL
)

SELECT
    uuid,
    labels,
    props
FROM found
ORDER BY uuid COLLATE "C"
