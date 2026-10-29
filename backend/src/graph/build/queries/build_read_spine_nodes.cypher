// Spine reachability: the spec nodes with their build-owned labels.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL
RETURN n.uuid AS uuid, n.spec_id AS spec_id, [label IN labels(n) WHERE label IN $labels] AS labels
ORDER BY uuid
