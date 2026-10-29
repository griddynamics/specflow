// Each of $spec_ids resolves to the node holding it as spec_id or as an alias.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND (n.spec_id IN $spec_ids OR any(alias IN coalesce(n.spec_id_aliases, []) WHERE alias IN $spec_ids))
RETURN n.uuid AS uuid, n.spec_id AS spec_id
ORDER BY spec_id
