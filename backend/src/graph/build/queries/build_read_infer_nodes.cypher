// Infer: the nodes a judgement shows, with up to $per_node grounding fragments in document order.
UNWIND $uuids AS node_uuid
MATCH (n:SpecNode {group_id: $project_id, uuid: node_uuid})
CALL (n) {
OPTIONAL MATCH (n)-[:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IS NOT NULL AND f.text IS NOT NULL AND f.status IN $statuses
WITH f
ORDER BY f.document_id, f.ordinal, f.fragment_id
LIMIT $per_node
RETURN collect(f {.fragment_id, content_hash: coalesce(f.content_hash, '')}) AS fragments
}
RETURN n.uuid AS uuid, n.spec_id AS spec_id, coalesce(n.name, '') AS name, n.identity_key AS identity_key, [label IN labels(n) WHERE label IN $labels] AS labels, n.statements AS statements, fragments
ORDER BY uuid
