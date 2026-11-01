-- Extract nodes: the names of the persisted hub nodes (:labels are the
-- ontology's hub labels).
WITH found AS (
    SELECT
        n.props ->> 'name' AS name,
        (
            SELECT u.hub_label
            FROM unnest(n.labels) WITH ORDINALITY AS u (hub_label, position)
            WHERE u.hub_label = any(cast(:labels AS text[]))
            ORDER BY u.position
            LIMIT 1
        ) AS label,
        coalesce(n.props -> 'identity_aliases', cast('[]' AS jsonb)) AS aliases
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.props ->> 'name' IS NOT NULL
)

SELECT
    name,
    label,
    aliases
FROM found
WHERE label IS NOT NULL
ORDER BY name COLLATE "C", label COLLATE "C"
