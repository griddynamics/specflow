// Fixture (positive): a `vector.similarity.cosine` argument; the server names the whole
// accepted set ("expected Vector, List<Float> ..."), which must retype to a number list.
MATCH (m:Movie:Entity {id: $movie_id})
RETURN vector.similarity.cosine(m.plot_embedding, $query_embedding) AS similarity
