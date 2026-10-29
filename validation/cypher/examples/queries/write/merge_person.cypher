// Create or update one person, keyed by id.
MERGE (p:Person:Entity {id: $id})
ON CREATE SET p.status = 'ACTIVE'
SET p.name = $name, p.born = $born
RETURN p.id AS id
