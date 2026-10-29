// Move one ACTED_IN edge onto the surviving person after two duplicates are merged.
MATCH (old:Person:Entity {id: $old_id})-[r:ACTED_IN]->(m:Movie)
WHERE elementId(r) = $rel_id
MATCH (survivor:Person:Entity {id: $survivor_id})
CREATE (survivor)-[moved:ACTED_IN]->(m)
SET moved = properties(r)
DELETE r
