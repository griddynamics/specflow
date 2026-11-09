// Persist: create new tasks by task id; a task this run created already (a resumed batch) is left as it is.
UNWIND $rows AS row
MERGE (t:Task {group_id: $project_id, task_id: row.id})
ON CREATE SET t += row.properties, t.uuid = row.uuid, t.attempts = 0, t.created_at = $now, t.updated_at = $now,
              t.created_run_id = $run_id, t.last_run_id = $run_id
RETURN count(t) AS written
