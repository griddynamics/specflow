-- Every document of the project.
SELECT n.props || jsonb_build_object('group_id', n.group_id) AS props
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'document'
    AND n.document_id IS NOT NULL
ORDER BY (n.props ->> 'path') COLLATE "C", n.document_id COLLATE "C"
