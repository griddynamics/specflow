// The people a person follows, up to FOLLOW_MAX_DEPTH hops away (registered cap 4).
MATCH p = (start:Person:Entity {id: $person_id})-[:FOLLOWS*1..4]->(other:Person)
WHERE length(p) <= $follow_max_depth
RETURN DISTINCT other.id AS id, other.name AS name
ORDER BY id
