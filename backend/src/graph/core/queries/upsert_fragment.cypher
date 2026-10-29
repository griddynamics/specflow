// Create or update one Fragment by its fragment_id (unique per project).
MERGE (n:Fragment {group_id: $project_id, fragment_id: $key})
SET n += $props
RETURN count(n) AS written
