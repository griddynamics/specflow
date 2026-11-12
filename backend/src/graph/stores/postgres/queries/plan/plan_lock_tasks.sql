-- Port of plan/queries/plan_lock_tasks.cypher. Story calls: lock every Task row
-- of the given stories (FOR UPDATE, in one fixed order for every caller, after
-- the stories), then answer their ids and statuses as they are after any
-- concurrent change commits.
SELECT
    n.props ->> 'task_id' AS task_id,
    n.props ->> 'status' AS status
FROM graph.node AS n
WHERE
    n.group_id = :project_id
    AND n.kind = 'foreign'
    AND n.labels[1] = 'Task'
    AND n.props ->> 'story_id' = any(cast(:story_ids AS text[]))
ORDER BY n.id
FOR UPDATE
