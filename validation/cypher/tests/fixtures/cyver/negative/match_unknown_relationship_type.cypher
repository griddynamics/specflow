// Fixture (negative): an unknown relationship type.
MATCH (:Person)-[:NOT_A_REL]->(:Movie)
RETURN 1
