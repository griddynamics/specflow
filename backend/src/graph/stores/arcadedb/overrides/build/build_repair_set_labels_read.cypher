// ArcadeDB: the labels of each keyed node of a label repair, read before the repair is planned (in the
// repair's transaction), so the copy of build_repair_set_labels_copy.cypher keeps the labels it does not change.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id, uuid: row.id})
RETURN row.id AS id, labels(n) AS labels
