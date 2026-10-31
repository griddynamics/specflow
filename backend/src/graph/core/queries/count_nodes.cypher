// Number of spec nodes of the project with any of $labels (the node filter of read_nodes.cypher).
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.spec_id IS NOT NULL AND any(label IN labels(n) WHERE label IN $labels)
RETURN count(n) AS total
