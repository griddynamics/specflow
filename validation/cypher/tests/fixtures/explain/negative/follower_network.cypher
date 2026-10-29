// Fixture (negative): the registered cap is 4 but this walk is bounded to 2, so the plan's
// expansion cannot match it.
MATCH p = (start:Person:Entity {id: $person_id})-[:FOLLOWS*1..2]->(other:Person)
WHERE length(p) <= $follow_max_depth
RETURN other.id AS id
