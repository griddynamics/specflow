// Fixture (negative): SET of a property Person does not declare.
MATCH (p:Person {id: $person_id})
SET p.not_a_declared_property = 1
