// One movie's plot embedding, for re-ranking outside the graph (a registered vector read).
MATCH (m:Movie:Entity {id: $movie_id})
RETURN m.plot_embedding AS plot_embedding
