// Fixture (positive): a list of maps and a list of integers read by subscript. The server
// types each element from the bound value and names no parameter on a mismatch, so both
// must be bound as lists the query can index.
UNWIND range(0, size($parts) - 1) AS part_index
MATCH (m:Movie:Entity {id: $parts[part_index].key})
WITH part_index, m, range(1, $candidates[part_index]) AS slots
RETURN m.id AS id, size(slots) AS candidates,
       vector.similarity.cosine($parts[part_index].embedding, m.plot_embedding) AS score
