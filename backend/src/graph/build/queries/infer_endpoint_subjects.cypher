// Infer (endpoints): each subject node with the uuids of its target nodes; subjects without any target are left out.
// __BRANCHES__ is a slot for the UNION branches rendered from infer_endpoint_via and infer_endpoint_mentions;
// its default is one directed via branch.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $subject_labels)
CALL (n) {
/*__BRANCHES__*/OPTIONAL MATCH (n)-[r:__TYPE__]->(m:SpecNode)
WHERE r.group_id = $project_id AND coalesce(r.basis, 'stated') IN $bases AND m.group_id = $project_id AND m.uuid IS NOT NULL AND any(label IN labels(m) WHERE label IN $target_labels)
RETURN m.uuid AS target/*__BRANCHES__*/
}
WITH n, collect(DISTINCT target) AS targets
WHERE size(targets) > 0
RETURN n.uuid AS uuid, n.spec_id AS spec_id, targets
ORDER BY spec_id, uuid
