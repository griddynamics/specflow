// ArcadeDB override of build/queries/build_repair_set_labels.cypher, step 3 (one statement per relationship
// type __TYPE__, static: ArcadeDB creates an edge of type EDGE for a dynamic type): every relationship of that
// type of the old node moves to its copy, in its own direction and with its properties (a self-loop stays one).
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.id + '|relabel'})
MATCH (old:SpecNode {group_id: $project_id, uuid: row.id})
MATCH (old)-[r:__TYPE__]-()
WITH DISTINCT old, new, r
WITH new, r, properties(r) AS props, startNode(r) = old AS outgoing, CASE WHEN startNode(r) = endNode(r) THEN new WHEN startNode(r) = old THEN endNode(r) ELSE startNode(r) END AS other
FOREACH (_ IN CASE WHEN outgoing THEN [1] ELSE [] END | CREATE (new)-[moved:__TYPE__]->(other) SET moved += props)
FOREACH (_ IN CASE WHEN NOT outgoing THEN [1] ELSE [] END | CREATE (other)-[moved:__TYPE__]->(new) SET moved += props)
DELETE r
