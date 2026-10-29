// Verification: fragment status and hashes (never the text), and whether any node is grounded in them.
MATCH (f:Fragment)
WHERE f.group_id = $project_id
RETURN f.fragment_id AS fragment_id, f.document_id AS document_id, f.status AS status, f.content_hash AS content_hash, f.extracted_hash AS extracted_hash, EXISTS {
MATCH (n:SpecNode)-[:REFERENCED_BY]->(f)
WHERE n.group_id = $project_id
} AS grounds
ORDER BY document_id, fragment_id
