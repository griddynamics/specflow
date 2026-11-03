// ArcadeDB override of build/queries/build_repair_set_labels.cypher, step 1: a node without a uuid (addressed by
// element id) changes its labels in place; a keyed node is copied by the steps after this one.
UNWIND $rows AS row
MATCH (n:SpecNode {group_id: $project_id})
WHERE n.uuid IS NULL AND elementId(n) = row.id
REMOVE n:$(row.remove)
SET n:$(row.label), n.label_disagreement = row.label_disagreement
RETURN count(n) AS written
