// Create or update a foreign node (a declared label of another owner than build) by its uuid.
// Each foreign label holds a (group_id, uuid) uniqueness constraint (graph.core.schema).
MERGE (n:__FOREIGN_LABEL__ {group_id: $project_id, uuid: $key})
SET n += $props
RETURN count(n) AS written
