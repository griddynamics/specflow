// Lifecycle: claim one task. The task is locked first; then, in the same statement, a plan persist in
// progress, the task's status, its area and story status and every prerequisite's status are read, and the
// claim applies only when the task is ready.
MATCH (t:Task {group_id: $project_id, task_id: $task_id})
SET t._lock = true
REMOVE t._lock
WITH t
OPTIONAL MATCH (p:Plan {group_id: $project_id})
OPTIONAL MATCH (a:WorkArea {group_id: $project_id, area_id: t.area_id})
OPTIONAL MATCH (s:Story {group_id: $project_id, story_id: t.story_id})
OPTIONAL MATCH (t)-[:WAITS_FOR {group_id: $project_id}]->(w:Task)
WITH t, p, a, s, collect(w.status) AS prerequisites
WITH t,
     CASE
       WHEN p IS NOT NULL AND p.applying IS NOT NULL THEN 'plan_applying'
       WHEN t.status <> 'pending' THEN 'wrong_state'
       WHEN a IS NULL OR a.status <> 'active' OR s IS NULL OR s.status <> 'active' THEN 'not_ready'
       WHEN any(status IN prerequisites WHERE status <> 'completed') THEN 'not_ready'
       ELSE 'ok'
     END AS outcome
FOREACH (_ IN CASE WHEN outcome = 'ok' THEN [1] ELSE [] END |
  SET t.status = 'in_progress', t.claim_token = $claim_token, t.claimed_by = $claimed_by, t.claimed_at = $now,
      t.attempts = coalesce(t.attempts, 0) + 1, t.updated_at = $now
)
RETURN outcome, t.status AS status, properties(t) AS task
