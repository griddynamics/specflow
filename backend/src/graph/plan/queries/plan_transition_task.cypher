// Lifecycle: one task state change. The task is matched on its immutable key and locked first (the _lock
// write), so its status and claim token are read after any concurrent change commits; the change applies
// only from an allowed status and, when asked, with the matching claim token.
MATCH (t:Task {group_id: $project_id, task_id: $task_id})
SET t._lock = true
REMOVE t._lock
WITH t,
     CASE
       WHEN NOT t.status IN $from_statuses THEN 'wrong_state'
       WHEN $check_token AND coalesce(t.claim_token, '') <> $claim_token THEN 'claim_mismatch'
       ELSE 'ok'
     END AS outcome
FOREACH (_ IN CASE WHEN outcome = 'ok' THEN [1] ELSE [] END |
  SET t += $properties, t.updated_at = $now
)
RETURN outcome, t.status AS status, properties(t) AS task
