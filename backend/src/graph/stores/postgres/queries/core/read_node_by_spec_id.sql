-- One spec node by spec_id with its provenance (the vectors are left out).
WITH picked AS (
    SELECT
        n.id,
        n.group_id,
        n.spec_id,
        n.labels,
        n.props
    FROM graph.node AS n
    WHERE n.group_id = :project_id AND n.kind = 'spec' AND n.spec_id = :spec_id
),

found AS (
    SELECT
        n.spec_id,
        prov.provenance,
        array_prepend('SpecNode', n.labels) AS labels,
        (n.props - 'embedding' - 'name_embedding') || jsonb_build_object(
            'group_id', n.group_id, 'embedding', NULL, 'name_embedding', NULL
        ) AS props
    FROM picked AS n
    LEFT JOIN LATERAL (
        SELECT
            coalesce(
                jsonb_agg(
                    jsonb_build_object(
                        'fragment_id', f.fragment_id,
                        'document_id', f.props ->> 'document_id',
                        'path', d.props ->> 'path',
                        'line_start', evid.props -> 'line_start',
                        'line_end', evid.props -> 'line_end'
                    )
                ),
                cast('[]' AS jsonb)
            ) AS provenance
        FROM graph.edge AS evid
        INNER JOIN graph.node AS f
            ON
                evid.target_id = f.id
                AND f.kind = 'fragment'
                AND f.group_id = :project_id
                AND coalesce(f.props ->> 'status', '') <> :retired
        LEFT JOIN graph.edge AS c
            ON f.id = c.target_id AND c.rel_type = 'CONTAINS'
        LEFT JOIN graph.node AS d
            ON
                c.source_id = d.id
                AND d.kind = 'document'
                AND d.group_id = :project_id
        WHERE evid.source_id = n.id AND evid.rel_type = 'REFERENCED_BY'
    ) AS prov ON TRUE
)

SELECT
    labels,
    props,
    provenance
FROM found
ORDER BY spec_id COLLATE "C"
