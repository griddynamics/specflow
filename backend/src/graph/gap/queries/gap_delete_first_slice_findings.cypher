// Delete the first-slice Finding nodes of the project with their relationships: the last step of the migration of
// first-slice findings, run after the gaps that replace them are written.
MATCH (f:Finding {group_id: $project_id})
DETACH DELETE f
RETURN count(*) AS deleted
