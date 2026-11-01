// ArcadeDB override of build/queries/build_merge_spec_nodes.cypher, step 3 of 3: delete each merged node
// (a merged node already gone is skipped) with the build-owned relationships it still has.
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.survivor})
UNWIND row.merged AS merged
MATCH (old:SpecNode {group_id: $project_id, uuid: merged})
WHERE old <> new
DETACH DELETE old
