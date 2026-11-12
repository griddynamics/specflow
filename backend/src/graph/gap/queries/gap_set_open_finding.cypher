// Set properties of one open finding by finding id. A null property in $props removes it. A finding that is gone or
// closed is left as it is and written stays zero.
MATCH (f:Finding {group_id: $project_id, finding_id: $finding_id})
WHERE f.status = 'open'
SET f += $props, f.updated_at = $now
RETURN count(f) AS written
