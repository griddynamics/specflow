-- Fragment: the Document of the project with document_id :__KEY__.
SELECT n.id
FROM graph.node AS n
WHERE n.group_id = :project_id AND n.kind = 'document' AND n.document_id = :__KEY__
