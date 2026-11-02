// One page of spec nodes of the project with any of $labels and spec_id > $after (no bound when $after is null), in
// spec_id order: the first $limit nodes (the reader asks for one more than it returns, to know whether more exist), each
// with its provenance as in read_nodes.cypher (retired fragments left out, the vectors nulled). The cut comes before the
// provenance matches so a node with many fragments still counts once.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND ($after IS NULL OR n.spec_id > $after)
WITH n
ORDER BY n.spec_id
LIMIT $limit
OPTIONAL MATCH (n)-[ref:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND coalesce(f.status, '') <> $retired
OPTIONAL MATCH (d:Document)-[:CONTAINS]->(f)
WHERE d.group_id = $project_id
WITH n, collect(CASE WHEN f IS NULL THEN NULL ELSE {fragment_id: f.fragment_id, document_id: f.document_id, path: d.path, line_start: ref.line_start, line_end: ref.line_end} END) AS provenance
RETURN labels(n) AS labels, n {.*, embedding: null, name_embedding: null} AS props, provenance
ORDER BY n.spec_id
