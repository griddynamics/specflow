// One page of spec nodes of the project with any of $labels and spec_id > $after (no bound when $after is null), in
// spec_id order: the first $limit nodes (the reader asks for one more than it returns, to know whether more exist), each
// with its provenance as in read_nodes.cypher (retired fragments left out, the vectors not read). The cut comes before the
// provenance matches so a node with many fragments still counts once.
// props: the node's properties as [key, value] pairs without the two vectors, so the store never loads them
// (a map projection reads every property); the dialect maps the pairs back to a map with the vectors null.
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
RETURN labels(n) AS labels, [key IN keys(n) WHERE key <> 'embedding' AND key <> 'name_embedding' | [key, n[key]]] AS props, provenance
ORDER BY n.spec_id
