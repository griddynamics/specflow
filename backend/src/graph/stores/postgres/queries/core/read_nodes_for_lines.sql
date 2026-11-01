-- Spec nodes whose evidence lines overlap a line range of one document.
-- Evidence lines are the REFERENCED_BY lines (the fragment's when the relationship has none);
-- retired fragments hold the lines of an earlier revision and are left out.
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
    AND n.spec_id IS NOT NULL
    AND n.labels && CAST(:labels AS text [])
    AND n.id IN (
        SELECT hit.source_id
        FROM graph.node AS doc
        INNER JOIN graph.edge AS contains_edge
            ON doc.id = contains_edge.source_id AND contains_edge.type = 'CONTAINS'
        INNER JOIN graph.node AS frag
            ON
                contains_edge.target_id = frag.id
                AND frag.kind = 'fragment'
                AND frag.group_id = :project_id
                AND coalesce(frag.props ->> 'status', '') <> :retired
        INNER JOIN graph.edge AS hit
            ON frag.id = hit.target_id AND hit.type = 'REFERENCED_BY'
        WHERE
            doc.group_id = :project_id
            AND doc.kind = 'document'
            AND doc.document_id = :document_id
            AND coalesce(hit.props -> 'line_start', frag.props -> 'line_start')
            <= to_jsonb(CAST(:line_end AS integer))
            AND coalesce(hit.props -> 'line_end', frag.props -> 'line_end')
            >= to_jsonb(CAST(:line_start AS integer))
    )
ORDER BY n.spec_id COLLATE "C"
