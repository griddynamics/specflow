// Every edge of one domain relation in the project, found through its (group_id, edge_key) index,
// with the uuid, identity key and spec_id of both ends.
MATCH ()-[r:__TYPE__ {group_id: $project_id}]->()
WHERE r.edge_key IS NOT NULL
WITH r, startNode(r) AS s, endNode(r) AS t
WHERE s:SpecNode AND t:SpecNode AND s.group_id = $project_id AND t.group_id = $project_id
RETURN r.edge_key AS edge_key, properties(r) AS props, s.uuid AS source_uuid, t.uuid AS target_uuid, s.identity_key AS source_key, t.identity_key AS target_key, s.spec_id AS source_spec_id, t.spec_id AS target_spec_id
ORDER BY edge_key, source_uuid, target_uuid
