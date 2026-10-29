// Infer (attach_orphan): for each node of $uuids, the $limit most similar target nodes by exact embedding cosine,
// leaving out those it already has an edge of the relation with; best first, ties by spec_id, then uuid.
// __ARROW__ is a slot: -> for a directed relation (the default), - for a symmetric one.
MATCH (c:SpecNode)
WHERE c.group_id = $project_id AND c.uuid IS NOT NULL AND any(label IN labels(c) WHERE label IN $target_labels) AND c.embedding IS NOT NULL
WITH collect(c) AS targets
UNWIND $uuids AS subject_uuid
MATCH (n:SpecNode {group_id: $project_id, uuid: subject_uuid})
WHERE n.embedding IS NOT NULL
CALL (n, targets) {
UNWIND targets AS c
WITH n, c
WHERE c <> n AND NOT EXISTS {
MATCH (n)-[x:__TYPE__]/*__ARROW__*/->/*__ARROW__*/(c)
WHERE x.group_id = $project_id AND coalesce(x.basis, 'stated') IN $bases
}
WITH c, vector.similarity.cosine(n.embedding, c.embedding) AS score
WHERE score IS NOT NULL
WITH c, score
ORDER BY score DESC, c.spec_id, c.uuid
LIMIT $limit
RETURN collect({uuid: c.uuid, score: score}) AS candidates
}
RETURN n.uuid AS uuid, candidates
ORDER BY uuid
