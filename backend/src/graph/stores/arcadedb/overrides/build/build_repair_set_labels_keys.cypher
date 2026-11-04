// ArcadeDB override of build/queries/build_repair_set_labels.cypher, last step: after its relationships moved
// and the old node is deleted, the copy takes the node's uuid back, and spec_key follows its spec_id.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.id + '|relabel'})
SET n.uuid = row.id, n.spec_key = CASE WHEN n.spec_id IS NULL THEN null ELSE $project_id + ':' + n.spec_id END
RETURN count(n) AS written
