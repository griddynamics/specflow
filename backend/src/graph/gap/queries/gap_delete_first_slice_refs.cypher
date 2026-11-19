// Delete the first-slice ExternalRef nodes of the project with their relationships: the last step of the migration
// of first-slice findings, run after the gaps that replace them are written (their refs are in the gap events).
MATCH (x:ExternalRef {group_id: $project_id})
DETACH DELETE x
RETURN count(*) AS deleted
