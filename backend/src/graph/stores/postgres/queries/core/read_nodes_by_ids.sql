-- Nodes named by an explicit id (identity key or alias in :keys) or by a
-- spec_id (own or alias in :spec_ids).
WITH found AS (
    SELECT
        n.uuid,
        n.spec_id,
        jsonb_build_array(n.props -> 'identity_key')
        || coalesce(n.props -> 'identity_aliases', cast('[]' AS jsonb)) AS keys,
        jsonb_build_array(n.spec_id)
        || coalesce(n.props -> 'spec_id_aliases', cast('[]' AS jsonb))
            AS spec_ids
    FROM graph.node AS n
    WHERE
        n.group_id = :project_id
        AND n.kind = 'spec'
        AND n.spec_id IS NOT NULL
        AND n.labels && cast(:labels AS text[])
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_each(cast(:where AS jsonb)) AS w
            WHERE NOT (
                coalesce(n.props -> w.key = w.value, FALSE)
                OR (
                    n.props -> w.key IS NULL
                    AND w.value = cast('null' AS jsonb)
                )
            )
        )
        AND (
            n.props ->> 'identity_key' = any(cast(:keys AS text[]))
            OR jsonb_exists_any(
                n.props -> 'identity_aliases', cast(:keys AS text[])
            )
            OR n.spec_id = any(cast(:spec_ids AS text[]))
            OR jsonb_exists_any(
                n.props -> 'spec_id_aliases', cast(:spec_ids AS text[])
            )
        )
)

SELECT
    uuid,
    spec_id,
    keys,
    spec_ids
FROM found
ORDER BY spec_id COLLATE "C"
