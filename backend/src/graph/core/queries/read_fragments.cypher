// The fragments of the project, optionally of one document.
MATCH (f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IS NOT NULL AND ($document_id IS NULL OR f.document_id = $document_id)
RETURN properties(f) AS props
ORDER BY f.document_id, f.ordinal, f.fragment_id
