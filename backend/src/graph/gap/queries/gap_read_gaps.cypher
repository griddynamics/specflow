// Gaps with what they are about: the stored properties of each gap whose status is in $statuses (and whose gap id
// is in $gap_ids, when that list is not empty); its ABOUT and IMPACTS nodes (relation, spec id, uuid, labels, name,
// spec id aliases, the documents holding the node's evidence, edge key and the run that added the relation); its
// CITES witnesses (fragment, its document, quote, lines, quote hash, edge key); its follow-up chain (continues: the
// gap is the source); the properties of the clarifications that answer it. Sorting and the other filters are the
// library's.
MATCH (g:Gap {group_id: $project_id})
WHERE g.status IN $statuses AND (size($gap_ids) = 0 OR g.gap_id IN $gap_ids)
OPTIONAL MATCH (g)-[r:ABOUT|IMPACTS {group_id: $project_id}]->(n:SpecNode {group_id: $project_id})
OPTIONAL MATCH (n)-[:REFERENCED_BY {group_id: $project_id}]->(e:Fragment {group_id: $project_id})
WITH g, r, n, collect(DISTINCT e.document_id) AS documents
WITH g, collect(CASE WHEN n IS NULL THEN NULL ELSE {relation: type(r), spec_id: n.spec_id, uuid: n.uuid, labels: labels(n), name: n.name, aliases: coalesce(n.spec_id_aliases, []), documents: documents, edge_key: r.edge_key, added_by_run: r.added_by_run} END) AS nodes
OPTIONAL MATCH (g)-[c:CITES {group_id: $project_id}]->(f:Fragment {group_id: $project_id})
WITH g, nodes, collect(CASE WHEN f IS NULL THEN NULL ELSE {fragment_id: f.fragment_id, document_id: f.document_id, quote: c.quote, line_start: c.line_start, line_end: c.line_end, quote_hash: c.quote_hash, edge_key: c.edge_key} END) AS cites
OPTIONAL MATCH (g)-[u:FOLLOWS_UP {group_id: $project_id}]-(o:Gap {group_id: $project_id})
WITH g, nodes, cites, collect(CASE WHEN o IS NULL THEN NULL ELSE {gap_id: o.gap_id, reason: u.reason, continues: startNode(u) = g} END) AS follow_ups
OPTIONAL MATCH (k:Clarification {group_id: $project_id})-[:ANSWERS {group_id: $project_id}]->(g)
WITH g, nodes, cites, follow_ups, collect(CASE WHEN k IS NULL THEN NULL ELSE properties(k) END) AS clarifications
RETURN properties(g) AS gap, nodes, cites, follow_ups, clarifications
ORDER BY g.gap_id
