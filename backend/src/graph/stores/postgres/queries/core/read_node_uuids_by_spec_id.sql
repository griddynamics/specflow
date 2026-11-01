-- Each of :spec_ids resolves to the node holding it as spec_id or as an alias.
SELECT
    n.uuid,
    n.spec_id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.spec_id IS NOT NULL
    AND n.labels && CAST(:labels AS text [])
    AND (
        n.spec_id = any(CAST(:spec_ids AS text []))
        OR jsonb_exists_any(n.props -> 'spec_id_aliases', CAST(:spec_ids AS text []))
    )
ORDER BY n.spec_id COLLATE "C"
