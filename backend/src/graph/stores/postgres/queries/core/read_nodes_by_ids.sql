-- Nodes named by an explicit id (identity key or alias in :keys) or by a spec_id (own or alias in :spec_ids).
SELECT
    n.uuid,
    n.spec_id,
    jsonb_build_array(n.props -> 'identity_key')
    || coalesce(n.props -> 'identity_aliases', CAST('[]' AS jsonb)) AS keys,
    jsonb_build_array(n.spec_id)
    || coalesce(n.props -> 'spec_id_aliases', CAST('[]' AS jsonb)) AS spec_ids
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.spec_id IS NOT NULL
    AND n.labels && CAST(:labels AS text [])
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_each(CAST(:where AS jsonb)) AS w
        WHERE NOT (
            coalesce(n.props -> w.key = w.value, FALSE)
            OR (n.props -> w.key IS NULL AND w.value = CAST('null' AS jsonb))
        )
    )
    AND (
        (n.props ->> 'identity_key') = any(CAST(:keys AS text []))
        OR jsonb_exists_any(n.props -> 'identity_aliases', CAST(:keys AS text []))
        OR n.spec_id = any(CAST(:spec_ids AS text []))
        OR jsonb_exists_any(n.props -> 'spec_id_aliases', CAST(:spec_ids AS text []))
    )
ORDER BY n.spec_id COLLATE "C"
