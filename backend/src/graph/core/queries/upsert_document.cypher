// Create or update one Document by its document_id (unique per project).
MERGE (n:Document {group_id: $project_id, document_id: $key})
SET n += $props
RETURN count(n) AS written
