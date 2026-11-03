// Infer (realization): each subject node with its members and the container nodes reached from them
// by one container path, each with the members it is reached from; subjects without any container
// are left out. A rule with several container paths runs this once per path (graph.build.infer).
// __MEMBER_ARROW__ is a slot: -> for a directed relation (the default), - for a symmetric one.
// __PATTERN__ is a slot for the path from a member to a container, rendered from a validated container
// path: min..max edges of its relations, or one edge of each relation of a sequence (default: one
// directed container-relation edge). Every edge of the path is stated or implied.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $subject_labels)
CALL (n) {
OPTIONAL MATCH (m:SpecNode)-[r:__MEMBER__]/*__MEMBER_ARROW__*/->/*__MEMBER_ARROW__*/(n)
WHERE r.group_id = $project_id AND coalesce(r.basis, 'stated') IN $bases AND m.group_id = $project_id AND m.uuid IS NOT NULL
RETURN collect(DISTINCT m.uuid) AS members
}
CALL (n, members) {
UNWIND members AS member_uuid
MATCH (m:SpecNode {group_id: $project_id, uuid: member_uuid})
MATCH p = (m)/*__PATTERN__*/-[:__CONTAINER__]->/*__PATTERN__*/(c:SpecNode)
WHERE c <> n AND c.group_id = $project_id AND c.uuid IS NOT NULL AND any(label IN labels(c) WHERE label IN $target_labels) AND all(x IN relationships(p) WHERE x.group_id = $project_id AND coalesce(x.basis, 'stated') IN $bases)
WITH c, collect(DISTINCT m.uuid) AS via
RETURN collect({uuid: c.uuid, via: via}) AS containers
}
WITH n, members, containers
WHERE size(containers) > 0
RETURN n.uuid AS uuid, n.spec_id AS spec_id, members, containers
ORDER BY spec_id, uuid
