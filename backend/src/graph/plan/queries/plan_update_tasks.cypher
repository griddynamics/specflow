// Persist: update tasks only while their status is still the one the run read. The task is matched on its
// immutable key and locked first (the _lock write), so a claim committed meanwhile is seen and the task
// is skipped into the run's conflicts; a task this run already updated (a resumed batch) applies again.
// One row: the ids of the tasks that did not apply (the run's conflicts).
UNWIND $rows AS row
MATCH (t:Task {group_id: $project_id, task_id: row.id})
SET t._lock = true
REMOVE t._lock
WITH t, row, (t.status = row.expected_status OR t.last_run_id = $run_id) AS applies
FOREACH (_ IN CASE WHEN applies THEN [1] ELSE [] END |
  SET t += row.properties, t.updated_at = $now, t.last_run_id = $run_id
)
RETURN collect(CASE WHEN applies THEN null ELSE row.id END) AS conflicts
