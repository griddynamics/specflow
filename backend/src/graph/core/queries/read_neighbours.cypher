// The domain edges around one spec node, with both ends.
MATCH (n:SpecNode {group_id: $project_id, spec_id: $spec_id})
MATCH (n)-[r]-(m:SpecNode)
WHERE type(r) IN $relations AND r.group_id = $project_id AND m.group_id = $project_id AND any(label IN labels(m) WHERE label IN $labels)
WITH DISTINCT r, startNode(r) AS s, endNode(r) AS t
RETURN type(r) AS relation, s.spec_id AS source, t.spec_id AS target, properties(r) AS props
ORDER BY source, relation, target, r.edge_key
