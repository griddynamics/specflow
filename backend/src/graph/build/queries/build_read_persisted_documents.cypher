// Plan: the persisted documents the snapshot is compared with; missing properties are defaulted.
MATCH (d:Document)
WHERE d.group_id = $project_id AND d.document_id IS NOT NULL
RETURN d.document_id AS document_id, coalesce(d.revision_id, '') AS revision_id, coalesce(d.version, 0) AS version, coalesce(d.path, d.document_id) AS path, coalesce(d.title, '') AS title, coalesce(d.status, 'active') AS status
ORDER BY path, document_id
