// Spec nodes by label and attribute equality ($where; a null value requires the property to be absent).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND all(name IN keys($where) WHERE n[name] = $where[name] OR (n[name] IS NULL AND $where[name] IS NULL))
OPTIONAL MATCH (n)-[ref:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND coalesce(f.status, '') <> $retired
OPTIONAL MATCH (d:Document)-[:CONTAINS]->(f)
WHERE d.group_id = $project_id
WITH n, collect(CASE WHEN f IS NULL THEN NULL ELSE {fragment_id: f.fragment_id, document_id: f.document_id, path: d.path, line_start: ref.line_start, line_end: ref.line_end} END) AS provenance
RETURN labels(n) AS labels, n {.*, embedding: null, name_embedding: null} AS props, provenance
ORDER BY n.spec_id
