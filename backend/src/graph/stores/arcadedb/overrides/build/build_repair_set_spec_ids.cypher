// ArcadeDB override of build/queries/build_repair_set_spec_ids.cypher: nodes are addressed by uuid, or by
// element id for a node without one (a label write moves the record: such a node is repaired in the next round); spec_key follows spec_id.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id})
WHERE coalesce(n.uuid, elementId(n)) = row.id
SET n.spec_id = row.spec_id, n.spec_id_aliases = row.aliases, n.spec_key = $project_id + ':' + row.spec_id
RETURN count(n) AS written
