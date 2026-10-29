// Fixture (positive): OPTIONAL MATCH over a declared path.
OPTIONAL MATCH (m:Movie)-[:IN_GENRE]->(g:Genre)
RETURN m, g
