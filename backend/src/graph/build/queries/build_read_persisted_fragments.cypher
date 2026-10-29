// Plan: the persisted fragments a revision is matched with; retired fragments are never matched.
MATCH (f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IS NOT NULL AND f.document_id IS NOT NULL AND coalesce(f.status, 'active') <> 'retired'
RETURN f.fragment_id AS fragment_id, f.document_id AS document_id, coalesce(f.heading_path, []) AS heading_path, coalesce(f.ordinal, 0) AS ordinal, coalesce(f.line_start, 0) AS line_start, coalesce(f.line_end, 0) AS line_end, coalesce(f.text, '') AS text, coalesce(f.content_hash, '') AS content_hash, coalesce(f.status, 'active') AS status, f.extracted_hash AS extracted_hash, f.error_code AS error_code
ORDER BY document_id, ordinal, fragment_id
