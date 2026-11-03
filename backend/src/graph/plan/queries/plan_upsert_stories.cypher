// Persist: create or update stories by story id; a null property in row.properties removes it.
UNWIND $rows AS row
MERGE (s:Story {group_id: $project_id, story_id: row.id})
ON CREATE SET s.uuid = row.uuid, s.created_at = $now
SET s += row.properties, s.updated_at = $now, s.last_run_id = $run_id
RETURN count(s) AS written
