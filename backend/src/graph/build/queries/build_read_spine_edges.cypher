// Spine reachability and realization walks: the edges of $relations between spec nodes of the project,
// with their basis and, for an inferred edge, the rule that wrote it.
MATCH (s:SpecNode)-[r]->(t:SpecNode)
WHERE s.group_id = $project_id AND s.uuid IS NOT NULL AND type(r) IN $relations AND r.group_id = $project_id AND t.group_id = $project_id
RETURN type(r) AS relation, s.uuid AS source, t.uuid AS target, coalesce(r.basis, 'stated') AS basis, r.inference_rule AS inference_rule
ORDER BY relation, source, target
