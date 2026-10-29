// Fixture (negative): not a declared Person property.
MATCH (p:Person)
RETURN p.not_a_declared_property
