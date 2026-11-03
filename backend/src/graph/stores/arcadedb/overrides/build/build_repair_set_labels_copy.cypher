// ArcadeDB override of build/queries/build_repair_set_labels.cypher, step 2: one statement per final label set,
// __LABEL__ the first of those domain labels and __MORE_LABELS__ one :__LABEL__ per further one (static:
// ArcadeDB ignores a dynamic label in CREATE), for the nodes of $ids.
// A keyed node is copied to a node of its final labels (the ones it had, less the removed ones, with the kept
// one) under a temporary uuid and without spec_key. Changing the labels in place moves the record once per
// label written, and ArcadeDB refuses the second move in one transaction (the record of the first move still
// holds the node's unique keys); a copy moves no record.
UNWIND $rows AS row
WITH row
WHERE row.id IN $ids
MATCH (o:SpecNode {group_id: $project_id, uuid: row.id})
CREATE (n:SpecNode:__LABEL__/*__MORE_LABELS__*/:__LABEL__/*__MORE_LABELS__*/)
SET n = properties(o), n.uuid = row.id + '|relabel', n.spec_key = null, n.label_disagreement = row.label_disagreement
RETURN count(n) AS written
