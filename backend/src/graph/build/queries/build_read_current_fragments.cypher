// Diff: the current fragments of the project.
MATCH (f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IS NOT NULL
RETURN f.fragment_id AS fragment_id, f.document_id AS document_id, f.spec_id AS spec_id, f.heading_path AS heading_path, f.ordinal AS ordinal, f.line_start AS line_start, f.line_end AS line_end, f.content_hash AS content_hash, f.status AS status, f.extracted_hash AS extracted_hash, f.error_code AS error_code
ORDER BY fragment_id
