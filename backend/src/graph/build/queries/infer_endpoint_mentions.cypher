// Infer (endpoints): the targets mentioned in one of the subject's evidence fragments.
OPTIONAL MATCH (n)-[e:REFERENCED_BY]->(f:Fragment)<-[l:MENTIONED_IN]-(m:SpecNode)
WHERE e.group_id = $project_id AND l.group_id = $project_id AND f.group_id = $project_id AND f.status IN $statuses AND m.group_id = $project_id AND m.uuid IS NOT NULL AND any(label IN labels(m) WHERE label IN $target_labels)
RETURN m.uuid AS target
