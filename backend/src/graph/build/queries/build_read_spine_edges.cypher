// Spine reachability: the spine-relation edges between spec nodes of the project, with their basis.
MATCH (s:SpecNode)-[r]->(t:SpecNode)
WHERE s.group_id = $project_id AND s.uuid IS NOT NULL AND type(r) IN $relations AND r.group_id = $project_id AND t.group_id = $project_id
RETURN type(r) AS relation, s.uuid AS source, t.uuid AS target, coalesce(r.basis, 'stated') AS basis
ORDER BY relation, source, target
