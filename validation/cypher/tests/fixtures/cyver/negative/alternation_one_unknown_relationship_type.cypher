// Fixture (negative): one branch of an alternation is unknown. The schema score can stay 1.0
// (FOLLOWS alone satisfies the path) while the metadata names the unknown branch.
MATCH (:Person)-[:FOLLOWS|NOT_A_REL]->(:Person)
RETURN 1
