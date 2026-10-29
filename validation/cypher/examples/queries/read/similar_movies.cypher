// The movies of one genre ranked by similarity to a query vector; the vector stays in the graph.
MATCH (g:Genre {name: $genre})<-[:IN_GENRE]-(m:Movie)
WITH m, vector.similarity.cosine(m.plot_embedding, $query_embedding) AS score
RETURN m.id AS id, m.title AS title, score
ORDER BY score DESC, id
LIMIT $limit
