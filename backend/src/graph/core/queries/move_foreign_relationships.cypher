// Move every relationship of the old node whose type is not in $owned_types to the new node:
// same type, direction and properties (a self-loop stays a self-loop), unless an equal one is
// already there (then it is only deleted); a relationship between old and new is only deleted;
// moved counts every relationship taken off old; a dynamic type in CREATE does not scan
MATCH (old:SpecNode {group_id: $project_id, uuid: $from_key})
WHERE $from_label IN labels(old)
MATCH (new:SpecNode {group_id: $project_id, uuid: $to_key})
WHERE $to_label IN labels(new) AND new <> old
MATCH (old)-[r]-(other)
WHERE NOT type(r) IN $owned_types
WITH DISTINCT old, new, r
WITH new, r, type(r) AS rel_type, properties(r) AS props, startNode(r) = old AS outgoing, (startNode(r) = new OR endNode(r) = new) AS between, CASE WHEN startNode(r) = endNode(r) THEN new WHEN startNode(r) = old THEN endNode(r) ELSE startNode(r) END AS other
FOREACH (_ IN CASE WHEN NOT between AND outgoing AND NOT EXISTS { MATCH (new)-[x]->(other) WHERE type(x) = rel_type AND properties(x) = props } THEN [1] ELSE [] END | CREATE (new)-[moved:$(rel_type)]->(other) SET moved += props)
FOREACH (_ IN CASE WHEN NOT between AND NOT outgoing AND NOT EXISTS { MATCH (other)-[x]->(new) WHERE type(x) = rel_type AND properties(x) = props } THEN [1] ELSE [] END | CREATE (other)-[moved:$(rel_type)]->(new) SET moved += props)
DELETE r
RETURN count(r) AS moved
