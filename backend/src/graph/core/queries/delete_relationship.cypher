// Delete one relationship of a type by its edge_key.
MATCH ()-[r:__TYPE__ {group_id: $project_id, edge_key: $key}]->()
DELETE r
RETURN count(*) AS deleted
