-- Port of gap/queries/gap_read_first_slice.cypher. First-slice findings,
-- read only by the migration of first-slice findings: the stored properties
-- of each Finding, the spec ids and fragment ids its ABOUT relations end at,
-- and the type and value of each ExternalRef it REFERS_TO.
SELECT
    f.props AS finding,
    coalesce(
        (
            SELECT jsonb_agg(DISTINCT n.spec_id)
            FROM graph.edge AS a
            INNER JOIN graph.node AS n
                ON
                    a.target_id = n.id
                    AND n.kind = 'spec'
                    AND n.group_id = :project_id
            WHERE
                a.group_id = :project_id
                AND a.source_id = f.id
                AND a.rel_type = 'ABOUT'
        ),
        cast('[]' AS jsonb)
    ) AS spec_ids,
    coalesce(
        (
            SELECT jsonb_agg(DISTINCT e.fragment_id)
            FROM graph.edge AS a
            INNER JOIN graph.node AS e
                ON
                    a.target_id = e.id
                    AND e.kind = 'fragment'
                    AND e.group_id = :project_id
            WHERE
                a.group_id = :project_id
                AND a.source_id = f.id
                AND a.rel_type = 'ABOUT'
        ),
        cast('[]' AS jsonb)
    ) AS fragment_ids,
    coalesce(
        (
            SELECT
                jsonb_agg(
                    jsonb_build_object(
                        'type', x.props ->> 'type',
                        'value', x.props ->> 'value'
                    )
                    ORDER BY x.props ->> 'type', x.props ->> 'value'
                )
            FROM graph.edge AS r
            INNER JOIN graph.node AS x
                ON
                    r.target_id = x.id
                    AND x.kind = 'foreign'
                    AND x.labels[1] = 'ExternalRef'
                    AND x.group_id = :project_id
            WHERE
                r.group_id = :project_id
                AND r.source_id = f.id
                AND r.rel_type = 'REFERS_TO'
        ),
        cast('[]' AS jsonb)
    ) AS refs
FROM graph.node AS f
WHERE
    f.group_id = :project_id
    AND f.kind = 'foreign'
    AND f.labels[1] = 'Finding'
ORDER BY f.props ->> 'finding_id'
