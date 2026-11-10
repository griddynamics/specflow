// The spec_id aliases of the project: each spec node of $labels that has any, with its aliases, in spec_id order.
// Plan reads resolve merged ids with these instead of reading every node of the project.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND size(coalesce(n.spec_id_aliases, [])) > 0 AND any(label IN labels(n) WHERE label IN $labels)
RETURN n.spec_id AS spec_id, n.spec_id_aliases AS aliases
ORDER BY spec_id
