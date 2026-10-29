// Infer (endpoints): the targets a subject reaches over one edge of a via relation.
// __ARROW__ is a slot: -> for a directed relation (the default), - for a symmetric one.
OPTIONAL MATCH (n)-[r:__TYPE__]/*__ARROW__*/->/*__ARROW__*/(m:SpecNode)
WHERE r.group_id = $project_id AND coalesce(r.basis, 'stated') IN $bases AND m.group_id = $project_id AND m.uuid IS NOT NULL AND any(label IN labels(m) WHERE label IN $target_labels)
RETURN m.uuid AS target
