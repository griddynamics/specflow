// Repair: delete spec nodes addressed by element id, with their relationships.
UNWIND $ids AS id
MATCH (n:SpecNode)
WHERE elementId(n) = id AND n.group_id = $project_id
DETACH DELETE n
RETURN count(*) AS written
