// ArcadeDB override of build/queries/build_repair_set_labels.cypher, guard between step 3 and 4: counts the
// relationships of the old nodes that no move statement carried over (a type whose name cannot be rendered).
// The plan's reducer refuses the repair when there is one, which rolls the transaction back, so such an edge is
// never lost to the delete.
UNWIND $rows AS row
MATCH (old:SpecNode {group_id: $project_id, uuid: row.id})
MATCH (new:SpecNode {group_id: $project_id, uuid: row.id + '|relabel'})
MATCH (old)-[x]-()
RETURN count(x) AS left
