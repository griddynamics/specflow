// Persist, first batch: the project's Plan node exists and holds the applying run id, so claims wait.
MERGE (p:Plan {group_id: $project_id, plan_id: $plan_id})
ON CREATE SET p.uuid = $uuid, p.created_at = $now
SET p.applying = $run_id, p.updated_at = $now
RETURN p.plan_id AS plan_id
