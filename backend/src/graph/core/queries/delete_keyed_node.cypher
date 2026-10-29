// Delete a node without domain label by its key.
MATCH (n:__LABEL__ {group_id: $project_id, __KEY__: $key})
DETACH DELETE n
RETURN count(*) AS deleted
