// One breadth-first step: spec nodes one domain edge (either direction) from $frontier that
// are not in $seen, the first $limit by spec_id.
MATCH (s:SpecNode)
WHERE s.group_id = $project_id AND s.uuid IN $frontier
MATCH (s)-[r]-(m:SpecNode)
WHERE type(r) IN $relations AND r.group_id = $project_id AND m.group_id = $project_id AND m.spec_id IS NOT NULL AND NOT m.uuid IN $seen AND any(label IN labels(m) WHERE label IN $labels)
WITH DISTINCT m
RETURN m.uuid AS uuid, m.spec_id AS spec_id
ORDER BY spec_id
LIMIT $limit
