// Fixture (positive): a registered capped walk planned to exactly its cap (4).
MATCH p = (start:Person:Entity {id: $person_id})-[:FOLLOWS*1..4]->(other:Person)
WHERE length(p) <= $follow_max_depth
RETURN other.id AS id
