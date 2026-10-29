// Extract nodes: the names of the persisted hub nodes ($labels are the ontology's hub labels).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.name IS NOT NULL
WITH n, [label IN labels(n) WHERE label IN $labels] AS hub
WHERE size(hub) > 0
RETURN n.name AS name, hub[0] AS label, coalesce(n.identity_aliases, []) AS aliases
ORDER BY name, label
