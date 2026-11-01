// ArcadeDB override of build/queries/build_merge_spec_nodes.cypher, step 2 of 3 (one statement per relationship
// type __TYPE__, static: ArcadeDB creates an edge of type EDGE for a dynamic type): move every relationship of
// that type of each merged node to the survivor unless an equal one is there; same rules as the canonical text.
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.survivor})
UNWIND row.merged AS merged
MATCH (old:SpecNode {group_id: $project_id, uuid: merged})
WHERE old <> new
MATCH (old)-[r:__TYPE__]-()
WITH DISTINCT old, new, r
WITH new, r, properties(r) AS props, startNode(r) = old AS outgoing, (startNode(r) = new OR endNode(r) = new) AS between, CASE WHEN startNode(r) = endNode(r) THEN new WHEN startNode(r) = old THEN endNode(r) ELSE startNode(r) END AS other
FOREACH (_ IN CASE WHEN NOT between AND outgoing AND NOT EXISTS { MATCH (new)-[x:__TYPE__]->(other) WHERE properties(x) = props } THEN [1] ELSE [] END | CREATE (new)-[moved:__TYPE__]->(other) SET moved += props)
FOREACH (_ IN CASE WHEN NOT between AND NOT outgoing AND NOT EXISTS { MATCH (other)-[x:__TYPE__]->(new) WHERE properties(x) = props } THEN [1] ELSE [] END | CREATE (other)-[moved:__TYPE__]->(new) SET moved += props)
DELETE r
