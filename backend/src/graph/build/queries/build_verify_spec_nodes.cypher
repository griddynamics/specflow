// Verification: spec nodes with their build-owned properties and labels.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id
RETURN elementId(n) AS id, n.uuid AS uuid, n.spec_id AS spec_id, n.name AS name, n.identity_key AS identity_key, n.identity_aliases AS identity_aliases, n.spec_id_aliases AS spec_id_aliases, n.label_disagreement AS label_disagreement, [label IN labels(n) WHERE label IN $labels] AS labels, [(n)-[:REFERENCED_BY]->(f:Fragment) WHERE f.group_id = $project_id | f.fragment_id] AS evidence
ORDER BY id
