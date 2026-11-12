// Delete one open finding with its relationships; nothing changes (deleted is 0) when it is gone or closed.
MATCH (f:Finding {group_id: $project_id, finding_id: $finding_id})
WHERE f.status = 'open'
DETACH DELETE f
RETURN count(*) AS deleted
