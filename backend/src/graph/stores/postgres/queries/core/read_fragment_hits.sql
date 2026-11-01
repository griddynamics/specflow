-- Fragments by id with their document.
SELECT
    f.props || jsonb_build_object('group_id', f.group_id) AS props,
    d.props || jsonb_build_object('group_id', d.group_id) AS document
FROM graph.node AS f
LEFT JOIN graph.node AS d
    ON
        d.group_id = :project_id
        AND d.kind = 'document'
        AND d.document_id = f.props ->> 'document_id'
WHERE
    f.group_id = :project_id
    AND f.kind = 'fragment'
    AND f.fragment_id = any(cast(:fragment_ids AS text[]))
