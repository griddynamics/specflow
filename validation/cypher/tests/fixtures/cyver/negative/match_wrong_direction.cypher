// Fixture (negative): ACTED_IN is Person -> Movie, never the reverse.
MATCH (:Movie)-[:ACTED_IN]->(:Person)
RETURN 1
