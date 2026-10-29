// MERGE one relationship by edge_key between two existing nodes.
// No row: an end is missing. conflict true: a relationship of this type and edge_key already joins
// other endpoints (found by a (group_id, edge_key) index seek), nothing written.
// __SOURCE__ and __TARGET__ are slots for the node matches rendered from match_node / match_node_checked;
// their default is the match of a spec node.
/*__SOURCE__*/MATCH (s:SpecNode {group_id: $project_id, uuid: $s_key}) WHERE $s_label IN labels(s)/*__SOURCE__*/
/*__TARGET__*/MATCH (t:SpecNode {group_id: $project_id, uuid: $t_key}) WHERE $t_label IN labels(t)/*__TARGET__*/
OPTIONAL MATCH ()-[other:__TYPE__ {group_id: $project_id, edge_key: $key}]->()
WHERE NOT (startNode(other) = s AND endNode(other) = t)
WITH s, t, count(other) AS conflicts
FOREACH (_ IN CASE WHEN conflicts = 0 THEN [1] ELSE [] END | MERGE (s)-[r:__TYPE__ {group_id: $project_id, edge_key: $key}]->(t) SET r += $props)
RETURN conflicts > 0 AS conflict
