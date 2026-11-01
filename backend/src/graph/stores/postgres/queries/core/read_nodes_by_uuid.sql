-- Spec nodes by uuid with their provenance (the vectors are left out).
SELECT
    array_prepend('SpecNode', n.labels) AS labels,
    (n.props - 'embedding' - 'name_embedding') || jsonb_build_object(
        'group_id', n.group_id, 'embedding', NULL, 'name_embedding', NULL
    ) AS props,
    prov.provenance
FROM graph.node AS n
LEFT JOIN LATERAL (
    SELECT
        coalesce(
            jsonb_agg(
                jsonb_build_object(
                    'fragment_id', f.fragment_id,
                    'document_id', f.props ->> 'document_id',
                    'path', d.props ->> 'path',
                    'line_start', ref.props -> 'line_start',
                    'line_end', ref.props -> 'line_end'
                )
            ),
            CAST('[]' AS jsonb)
        ) AS provenance
    FROM graph.edge AS ref
    INNER JOIN graph.node AS f
        ON
            ref.target_id = f.id
            AND f.kind = 'fragment'
            AND f.group_id = :project_id
            AND coalesce(f.props ->> 'status', '') <> :retired
    LEFT JOIN graph.edge AS c ON f.id = c.target_id AND c.type = 'CONTAINS'
    LEFT JOIN graph.node AS d
        ON c.source_id = d.id AND d.kind = 'document' AND d.group_id = :project_id
    WHERE ref.source_id = n.id AND ref.type = 'REFERENCED_BY'
) AS prov ON TRUE
WHERE
    n.group_id = :project_id
    AND n.kind = 'spec'
    AND n.uuid = any(CAST(:uuids AS text []))
    AND n.spec_id IS NOT NULL
ORDER BY n.spec_id COLLATE "C"
