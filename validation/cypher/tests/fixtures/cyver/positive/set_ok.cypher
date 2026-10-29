// Fixture (positive): SET of a declared property.
MATCH (p:Person {id: $person_id})
SET p.status = $status
RETURN p
