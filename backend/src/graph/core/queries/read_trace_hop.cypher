// Trace (trace.md "Library"): the domain edges of $relations at each spec node of $frontier (by uuid), with
// the other end, whether the edge leaves the frontier node, and the edge's basis, confidence and rule.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IN $frontier
MATCH (n)-[r]-(m:SpecNode)
WHERE type(r) IN $relations AND r.group_id = $project_id AND m.group_id = $project_id AND m.spec_id IS NOT NULL AND any(label IN labels(m) WHERE label IN $labels)
WITH n, m, r, startNode(r) AS s
RETURN n.uuid AS frontier, m.uuid AS uuid, m.spec_id AS spec_id, labels(m) AS labels, m.name AS name, type(r) AS relation, s.uuid = n.uuid AS outgoing, r.basis AS basis, r.confidence AS confidence, r.inference_rule AS inference_rule
ORDER BY frontier, spec_id, relation
