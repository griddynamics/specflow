// Spec nodes whose evidence lines overlap a line range of one document.
// Evidence lines are the REFERENCED_BY lines (the fragment's when the relationship has none);
// retired fragments hold the lines of an earlier revision and are left out.
MATCH (:Document {group_id: $project_id, document_id: $document_id})-[:CONTAINS]->(f:Fragment)
WHERE f.group_id = $project_id AND coalesce(f.status, '') <> $retired
MATCH (n:SpecNode)-[ref:REFERENCED_BY]->(f)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND coalesce(ref.line_start, f.line_start) <= $line_end AND coalesce(ref.line_end, f.line_end) >= $line_start
WITH DISTINCT n
OPTIONAL MATCH (n)-[ref:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND coalesce(f.status, '') <> $retired
OPTIONAL MATCH (d:Document)-[:CONTAINS]->(f)
WHERE d.group_id = $project_id
WITH n, collect(CASE WHEN f IS NULL THEN NULL ELSE {fragment_id: f.fragment_id, document_id: f.document_id, path: d.path, line_start: ref.line_start, line_end: ref.line_end} END) AS provenance
RETURN labels(n) AS labels, n {.*, embedding: null, name_embedding: null} AS props, provenance
ORDER BY n.spec_id
