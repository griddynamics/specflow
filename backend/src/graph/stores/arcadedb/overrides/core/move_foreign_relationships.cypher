// ArcadeDB override of core/queries/move_foreign_relationships.cypher: one statement per relationship type
// __TYPE__ (static: ArcadeDB creates an edge of type EDGE for a dynamic type); moved counts the
// relationships of that type taken off the old node. Same rules as the canonical text.
MATCH (old:SpecNode {group_id: $project_id, uuid: $from_key})
WHERE $from_label IN labels(old)
MATCH (new:SpecNode {group_id: $project_id, uuid: $to_key})
WHERE $to_label IN labels(new) AND new <> old
MATCH (old)-[r:__TYPE__]-(other)
WITH DISTINCT old, new, r
WITH new, r, properties(r) AS props, startNode(r) = old AS outgoing, (startNode(r) = new OR endNode(r) = new) AS between, CASE WHEN startNode(r) = endNode(r) THEN new WHEN startNode(r) = old THEN endNode(r) ELSE startNode(r) END AS other
FOREACH (_ IN CASE WHEN NOT between AND outgoing AND NOT EXISTS { MATCH (new)-[x:__TYPE__]->(other) WHERE properties(x) = props } THEN [1] ELSE [] END | CREATE (new)-[moved:__TYPE__]->(other) SET moved += props)
FOREACH (_ IN CASE WHEN NOT between AND NOT outgoing AND NOT EXISTS { MATCH (other)-[x:__TYPE__]->(new) WHERE properties(x) = props } THEN [1] ELSE [] END | CREATE (other)-[moved:__TYPE__]->(new) SET moved += props)
DELETE r
RETURN count(r) AS moved
