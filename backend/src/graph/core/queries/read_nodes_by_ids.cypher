// Nodes named by an explicit id (identity key or alias in $keys) or by a spec_id (own or alias in $spec_ids).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels) AND all(name IN keys($where) WHERE n[name] = $where[name] OR (n[name] IS NULL AND $where[name] IS NULL)) AND (n.identity_key IN $keys OR any(key IN coalesce(n.identity_aliases, []) WHERE key IN $keys) OR n.spec_id IN $spec_ids OR any(alias IN coalesce(n.spec_id_aliases, []) WHERE alias IN $spec_ids))
RETURN n.uuid AS uuid, n.spec_id AS spec_id, [n.identity_key] + coalesce(n.identity_aliases, []) AS keys, [n.spec_id] + coalesce(n.spec_id_aliases, []) AS spec_ids
ORDER BY spec_id
