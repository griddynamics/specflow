// The relationship types of one node without domain label
MATCH (n:__LABEL__ {group_id: $project_id, __KEY__: $n_key})
OPTIONAL MATCH (n)-[r]-()
RETURN collect(DISTINCT type(r)) AS types
