// Story calls: set properties of stories by story id; a null property in row.properties removes it. Runs after
// plan_lock_stories in the same transaction.
UNWIND $rows AS row
MATCH (s:Story {group_id: $project_id, story_id: row.id})
SET s += row.properties, s.updated_at = $now
RETURN count(s) AS written
