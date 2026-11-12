// Story calls: lock every Task node of the given stories in task id order (after the stories, never before), then
// answer their ids and statuses as they are after any concurrent change commits.
MATCH (t:Task {group_id: $project_id})
WHERE t.story_id IN $story_ids
WITH t ORDER BY t.task_id
SET t._lock = true
REMOVE t._lock
RETURN t.task_id AS task_id, t.status AS status
ORDER BY task_id
