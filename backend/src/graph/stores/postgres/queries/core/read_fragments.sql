-- The fragments of the project, optionally of one document.
SELECT n.props || jsonb_build_object('group_id', n.group_id) AS props
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'fragment'
    AND n.fragment_id IS NOT NULL
    AND (
        CAST(:document_id AS text) IS NULL
        OR (n.props ->> 'document_id') = CAST(:document_id AS text)
    )
ORDER BY
    (n.props ->> 'document_id') COLLATE "C",
    (n.props -> 'ordinal'),
    n.fragment_id COLLATE "C"
