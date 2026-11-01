-- Each of :spec_ids resolves to the node holding it as spec_id or as an alias.
WITH found AS (
    SELECT
        n.uuid,
        n.spec_id
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:labels AS text[])
        AND (
            n.spec_id = any(cast(:spec_ids AS text[]))
            OR jsonb_exists_any(
                n.props -> 'spec_id_aliases', cast(:spec_ids AS text[])
            )
        )
)

SELECT
    uuid,
    spec_id
FROM found
ORDER BY spec_id COLLATE "C"
