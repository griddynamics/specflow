// One persisted plan relationship type, with stored keys, endpoint ids and properties.
MATCH ()-[r:__TYPE__ {group_id: $project_id}]->()
WITH r, startNode(r) AS source, endNode(r) AS target
WHERE source.group_id = $project_id AND target.group_id = $project_id
RETURN type(r) AS type,
       CASE
         WHEN source:SpecNode THEN source.spec_id
         WHEN source:Task THEN source.task_id
         WHEN source:WorkArea THEN source.area_id
         WHEN source:Story THEN source.story_id
       END AS source,
       CASE
         WHEN target:SpecNode THEN target.spec_id
         WHEN target:Task THEN target.task_id
         WHEN target:WorkArea THEN target.area_id
         WHEN target:Story THEN target.story_id
       END AS target,
       r.edge_key AS edge_key,
       [key IN keys(r) WHERE key <> 'edge_key' AND key <> 'group_id' | [key, r[key]]] AS properties
ORDER BY edge_key
