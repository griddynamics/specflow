// Fixture (positive): MERGE of a declared relationship type with a declared property.
MATCH (p:Person {id: $person_id}), (m:Movie {id: $movie_id})
MERGE (p)-[r:ACTED_IN]->(m)
SET r.roles = $roles
RETURN r
