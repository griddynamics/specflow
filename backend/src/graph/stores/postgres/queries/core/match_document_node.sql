-- Fragment: the Document of the project with the document_id in the
-- parameter the token KEY names.
SELECT n.id
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'document'
    AND n.document_id = :__KEY__
