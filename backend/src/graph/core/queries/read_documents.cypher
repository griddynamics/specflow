// Every document of the project.
MATCH (d:Document)
WHERE d.group_id = $project_id AND d.document_id IS NOT NULL
RETURN properties(d) AS props
ORDER BY d.path, d.document_id
