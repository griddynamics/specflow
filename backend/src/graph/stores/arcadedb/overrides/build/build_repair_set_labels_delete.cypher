// ArcadeDB override of build/queries/build_repair_set_labels.cypher, step 4: each old node whose copy exists is
// deleted (its relationships moved to the copy in step 3).
UNWIND $rows AS row
MATCH (old:SpecNode {group_id: $project_id, uuid: row.id})
MATCH (new:SpecNode {group_id: $project_id, uuid: row.id + '|relabel'})
DETACH DELETE old
