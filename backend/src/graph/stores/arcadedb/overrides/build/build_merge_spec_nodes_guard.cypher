// ArcadeDB override of build/queries/build_merge_spec_nodes.cypher, guard between step 2 and 3: counts the
// relationships of the merged nodes that build does not own and that no move statement carried over (a type
// whose name cannot be rendered into a statement). The plan's reducer refuses the merge when there is one,
// which rolls the transaction back, so such an edge is never lost to the delete.
UNWIND $rows AS row
MATCH (new:SpecNode {group_id: $project_id, uuid: row.survivor})
UNWIND row.merged AS merged
MATCH (old:SpecNode {group_id: $project_id, uuid: merged})
WHERE old <> new
MATCH (old)-[x]-()
WHERE NOT type(x) IN $owned_types
RETURN count(x) AS left
