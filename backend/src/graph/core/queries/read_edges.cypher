// Domain edges between spec nodes of the project, filtered by relation and label.
MATCH (s:SpecNode)-[r]->(t:SpecNode)
WHERE s.group_id = $project_id AND s.spec_id IS NOT NULL AND type(r) IN $relations AND r.group_id = $project_id AND t.group_id = $project_id AND any(label IN labels(s) WHERE label IN $labels) AND any(label IN labels(t) WHERE label IN $labels) AND ($include_inferred OR coalesce(r.basis, '') <> $inferred)
RETURN type(r) AS relation, s.spec_id AS source, t.spec_id AS target, properties(r) AS props
ORDER BY source, relation, target, r.edge_key
