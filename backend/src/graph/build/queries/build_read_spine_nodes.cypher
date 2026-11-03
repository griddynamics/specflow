// Spine reachability: the spec nodes with their build-owned labels and the stored values of the
// attributes the ontology's conditions name ($attributes), as [name, value] pairs (null values left out).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL
RETURN n.uuid AS uuid, n.spec_id AS spec_id, [label IN labels(n) WHERE label IN $labels] AS labels, [name IN $attributes WHERE n[name] IS NOT NULL | [name, n[name]]] AS attributes
ORDER BY uuid
