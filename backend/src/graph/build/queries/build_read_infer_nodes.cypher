// Infer: the nodes a judgement shows, with up to $per_node grounding fragments in document order. Each fragment
// has its heading path and, from its text, the heading and code fence lines up to the node's first evidence line
// in it (``headings``; none when the evidence edge or the fragment has no line), so the section the node sits in
// is found in code; ``document_id`` groups batched orphans by document. ``quote`` is the node's $quote attribute
// (the judgement input hash keys a node by identity key, label, name and quote, never its reworded texts).
UNWIND $uuids AS node_uuid
MATCH (n:SpecNode {group_id: $project_id, uuid: node_uuid})
CALL (n) {
OPTIONAL MATCH (n)-[r:REFERENCED_BY]->(f:Fragment)
WHERE f.group_id = $project_id AND f.fragment_id IS NOT NULL AND f.text IS NOT NULL AND f.status IN $statuses
WITH f, min(r.line_start) AS line
ORDER BY f.document_id, f.ordinal, f.fragment_id
LIMIT $per_node
WITH f, CASE WHEN f IS NULL OR line IS NULL OR f.line_start IS NULL OR line < f.line_start THEN [] ELSE split(f.text, '\n')[0..(line - f.line_start + 1)] END AS above
RETURN collect(f {.fragment_id, .document_id, content_hash: coalesce(f.content_hash, ''), heading_path: coalesce(f.heading_path, []), headings: [text_line IN above WHERE text_line =~ ' {0,3}(#{1,6}([ \t].*)?|```.*|~~~.*)']}) AS fragments
}
RETURN n.uuid AS uuid, n.spec_id AS spec_id, coalesce(n.name, '') AS name, n.identity_key AS identity_key, coalesce(n[$quote], '') AS quote, [label IN labels(n) WHERE label IN $labels] AS labels, n.statements AS statements, fragments
ORDER BY uuid
