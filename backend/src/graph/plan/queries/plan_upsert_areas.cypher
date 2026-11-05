// Persist: create or update work areas by area id; a null property in row.properties removes it.
UNWIND $rows AS row
MERGE (a:WorkArea {group_id: $project_id, area_id: row.id})
ON CREATE SET a.uuid = row.uuid, a.created_at = $now
SET a += row.properties, a.updated_at = $now, a.last_run_id = $run_id
RETURN count(a) AS written
