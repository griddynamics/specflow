// One spec node by a spec_id alias with its provenance (the vectors are left out).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND $spec_id IN coalesce(n.spec_id_aliases, [])
OPTIONAL MATCH (n)-[ref:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND coalesce(f.status, '') <> $retired
OPTIONAL MATCH (d:Document)-[:CONTAINS]->(f)
WHERE d.group_id = $project_id
WITH n, collect(CASE WHEN f IS NULL THEN NULL ELSE {fragment_id: f.fragment_id, document_id: f.document_id, path: d.path, line_start: ref.line_start, line_end: ref.line_end} END) AS provenance
RETURN labels(n) AS labels, n {.*, embedding: null, name_embedding: null} AS props, provenance
ORDER BY n.spec_id
