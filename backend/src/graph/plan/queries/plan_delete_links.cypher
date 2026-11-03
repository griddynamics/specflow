// Persist: delete plan relationships of one type by stored key and endpoint ids (a spec end by spec id).
UNWIND $rows AS row
MATCH (s)-[r:__TYPE__ {group_id: $project_id, edge_key: row.key}]->(t)
WHERE coalesce(s.task_id, s.story_id, s.area_id, s.spec_id) = row.source
  AND coalesce(t.task_id, t.story_id, t.area_id, t.spec_id) = row.target
DELETE r
RETURN count(*) AS deleted
