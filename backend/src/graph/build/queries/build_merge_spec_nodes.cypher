// Persist (merge): per row, move every relationship of each merged node whose type build does not own
// to the survivor, then delete the merged node with its build-owned relationships.
// The survivor gets its final spec_id_aliases. A merged node already gone is skipped.
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.survivor})
SET new.spec_id_aliases = row.aliases, new.last_build_id = $build_id
WITH new, row
CALL (new, row) {
UNWIND row.merged AS merged
MATCH (old:SpecNode {group_id: $project_id, uuid: merged})
WHERE old <> new
CALL (old, new) {
MATCH (old)-[r]-()
WHERE NOT type(r) IN $owned_types
WITH DISTINCT old, new, r
WITH new, r, type(r) AS rel_type, properties(r) AS props, startNode(r) = old AS outgoing, (startNode(r) = new OR endNode(r) = new) AS between, CASE WHEN startNode(r) = endNode(r) THEN new WHEN startNode(r) = old THEN endNode(r) ELSE startNode(r) END AS other
FOREACH (_ IN CASE WHEN NOT between AND outgoing AND NOT EXISTS { MATCH (new)-[x]->(other) WHERE type(x) = rel_type AND properties(x) = props } THEN [1] ELSE [] END | CREATE (new)-[moved:$(rel_type)]->(other) SET moved += props)
FOREACH (_ IN CASE WHEN NOT between AND NOT outgoing AND NOT EXISTS { MATCH (other)-[x]->(new) WHERE type(x) = rel_type AND properties(x) = props } THEN [1] ELSE [] END | CREATE (other)-[moved:$(rel_type)]->(new) SET moved += props)
DELETE r
}
DETACH DELETE old
}
RETURN count(new) AS written
