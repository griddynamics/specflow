// Infer (cross_document): each hub of $subject_labels with its name and identity keys and its members: nodes of
// $member_labels linked to it by one stated or implied edge of a $via relation in either direction and, with
// $mentions, those grounded in a fragment the hub is MENTIONED_IN; with $mentions also the other nodes grounded in
// a fragment the hub is grounded in itself (REFERENCED_BY), each with `shared` (those fragment ids), its name, its
// $quote attribute and its statements, so the caller keeps only those whose own text names the hub; each member
// with the documents of its evidence fragments whose status is in $statuses. Members without such a fragment are
// left out, and so are hubs with fewer than two members.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $subject_labels)
CALL (n) {
OPTIONAL MATCH (n)-[r]-(v:SpecNode)
WHERE type(r) IN $via AND r.group_id = $project_id AND coalesce(r.basis, 'stated') IN $bases AND v.group_id = $project_id AND v.uuid IS NOT NULL AND v.spec_id IS NOT NULL AND any(label IN labels(v) WHERE label IN $member_labels)
WITH n, collect(DISTINCT v) AS linked
OPTIONAL MATCH (n)-[l:MENTIONED_IN]->(f:Fragment)<-[e:REFERENCED_BY]-(g:SpecNode)
WHERE $mentions AND g <> n AND l.group_id = $project_id AND e.group_id = $project_id AND f.group_id = $project_id AND f.status IN $statuses AND g.group_id = $project_id AND g.uuid IS NOT NULL AND g.spec_id IS NOT NULL AND any(label IN labels(g) WHERE label IN $member_labels)
WITH n, linked, collect(DISTINCT g) AS mentioned
WITH n, linked + mentioned AS named
OPTIONAL MATCH (n)-[o:REFERENCED_BY]->(h:Fragment)<-[q:REFERENCED_BY]-(s:SpecNode)
WHERE $mentions AND s <> n AND NOT s IN named AND o.group_id = $project_id AND q.group_id = $project_id AND h.group_id = $project_id AND h.status IN $statuses AND s.group_id = $project_id AND s.uuid IS NOT NULL AND s.spec_id IS NOT NULL AND any(label IN labels(s) WHERE label IN $member_labels)
WITH named, s, collect(DISTINCT h.fragment_id) AS fragments
WITH named, collect(CASE WHEN s IS NOT NULL THEN {node: s, shared: fragments} END) AS grounded
UNWIND [m IN named | {node: m, shared: null}] + grounded AS item
RETURN DISTINCT item.node AS m, item.shared AS shared
}
CALL (m) {
MATCH (m)-[e:REFERENCED_BY]->(f:Fragment)
WHERE e.group_id = $project_id AND f.group_id = $project_id AND f.status IN $statuses AND f.document_id IS NOT NULL
RETURN collect(DISTINCT f.document_id) AS documents
}
WITH n, m, shared, documents
WHERE size(documents) > 0
WITH n, collect(CASE WHEN shared IS NULL THEN {uuid: m.uuid, documents: documents} ELSE {uuid: m.uuid, documents: documents, shared: shared, name: m.name, quote: m[$quote], statements: m.statements} END) AS members
WHERE size(members) > 1
RETURN n.uuid AS uuid, n.spec_id AS spec_id, n.name AS name, [key IN [n.identity_key] + coalesce(n.identity_aliases, []) WHERE key IS NOT NULL] AS keys, members
ORDER BY spec_id, uuid
