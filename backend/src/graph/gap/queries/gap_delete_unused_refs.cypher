// Delete the ExternalRef nodes of $uuids that no finding refers to any more.
MATCH (x:ExternalRef {group_id: $project_id})
WHERE x.uuid IN $uuids AND NOT (x)<-[:REFERS_TO]-()
DETACH DELETE x
RETURN count(*) AS deleted
