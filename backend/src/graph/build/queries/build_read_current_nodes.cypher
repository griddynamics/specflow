// Diff: the current spec nodes, found through the (group_id, uuid) unique index; the vectors are left out.
MATCH (n:SpecNode)
WHERE n.group_id = $project_id AND n.uuid IS NOT NULL
RETURN n.uuid AS uuid, labels(n) AS labels, n {.*, embedding: null, name_embedding: null} AS props
ORDER BY uuid
