// Story calls: set properties of tasks by task id; a null property in row.properties removes it. Runs after
// plan_lock_tasks in the same transaction.
UNWIND $rows AS row
MATCH (t:Task {group_id: $project_id, task_id: row.id})
SET t += row.properties, t.updated_at = $now
RETURN count(t) AS written
