// Fixture (negative): `id()` is deprecated -- a server notification.
MATCH (m:Movie:Entity {id: $movie_id})
RETURN id(m)
