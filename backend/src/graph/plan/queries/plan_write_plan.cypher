// Persist, last batch: the Plan node's properties are written and the applying run id is cleared.
MATCH (p:Plan {group_id: $project_id, plan_id: $plan_id})
SET p += $properties, p.updated_at = $now, p.last_run_id = $run_id
REMOVE p.applying
RETURN p.plan_id AS plan_id
