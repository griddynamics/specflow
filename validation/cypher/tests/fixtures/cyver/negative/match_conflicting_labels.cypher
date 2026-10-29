// Fixture (negative): `n` cannot be both a Person and a Genre.
MATCH (n:Person), (n:Genre)
RETURN n
