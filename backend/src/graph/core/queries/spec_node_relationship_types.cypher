// The relationship types of one spec node of the project.
MATCH (n:SpecNode {group_id: $project_id, uuid: $n_key})
WHERE $n_label IN labels(n)
OPTIONAL MATCH (n)-[r]-()
RETURN collect(DISTINCT type(r)) AS types
