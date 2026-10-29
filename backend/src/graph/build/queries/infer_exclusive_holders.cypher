// Infer: the nodes of $uuids that already have a stated or implied edge on one of the exclusive pairs $pairs.
UNWIND $uuids AS subject_uuid
MATCH (n:SpecNode {group_id: $project_id, uuid: subject_uuid})-[x:__TYPE__]->(t:SpecNode)
WHERE x.group_id = $project_id AND coalesce(x.basis, 'stated') IN $bases AND t.group_id = $project_id AND any(pair IN $pairs WHERE pair[0] IN labels(n) AND pair[1] IN labels(t))
RETURN DISTINCT n.uuid AS uuid
ORDER BY uuid
