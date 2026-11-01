// ArcadeDB override of build/queries/build_repair_set_spec_ids.cypher: nodes are addressed by uuid (a label
// write moves the record, so an element id read earlier is stale); spec_key follows spec_id.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.id})
SET n.spec_id = row.spec_id, n.spec_id_aliases = row.aliases, n.spec_key = $project_id + ':' + row.spec_id
RETURN count(n) AS written
