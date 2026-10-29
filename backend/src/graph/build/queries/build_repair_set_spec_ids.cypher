// Repair: set the spec_id and aliases of nodes addressed by element id.
UNWIND $rows AS row
MATCH (n:SpecNode)
WHERE elementId(n) = row.id AND n.group_id = $project_id
SET n.spec_id = row.spec_id, n.spec_id_aliases = row.aliases
RETURN count(n) AS written
