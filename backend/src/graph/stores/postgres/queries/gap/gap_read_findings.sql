-- Port of gap/queries/gap_read_findings.cypher. Findings: the stored
-- properties and the references of each ([type, value] pairs), filtered by
-- status and, when given, by id, author, dedupe key and about ids (an empty
-- :spec_ids list or a null filter asks for all). The library sorts and pages.
SELECT
    n.props AS finding,
    coalesce(
        (
            SELECT
                jsonb_agg(
                    jsonb_build_array(x.props ->> 'type', x.props ->> 'value')
                    ORDER BY x.props ->> 'type', x.props ->> 'value'
                )
            FROM graph.edge AS e
            INNER JOIN graph.node AS x
                ON e.target_id = x.id AND x.group_id = :project_id
            WHERE
                e.group_id = :project_id
                AND e.rel_type = 'REFERS_TO'
                AND e.source_id = n.id
        ),
        cast('[]' AS jsonb)
    ) AS refs
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'foreign'
    AND n.labels[1] = 'Finding'
    AND n.props ->> 'status' = any(cast(:statuses AS text[]))
    AND (
        cast(:finding_id AS text) IS NULL
        OR n.props ->> 'finding_id' = cast(:finding_id AS text)
    )
    AND (
        cast(:created_by AS text) IS NULL
        OR n.props ->> 'created_by' = cast(:created_by AS text)
    )
    AND (
        cast(:dedupe_key AS text) IS NULL
        OR n.props ->> 'dedupe_key' = cast(:dedupe_key AS text)
    )
    AND (
        cardinality(cast(:spec_ids AS text[])) = 0
        OR jsonb_exists_any(
            n.props -> 'about_spec_ids', cast(:spec_ids AS text[])
        )
    )
ORDER BY n.props ->> 'finding_id'
