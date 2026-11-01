// The relationship types of the database, read by the executor before it plans a merge or a move of
// relationships (unscoped: it reads the schema, not project data).
CALL db.relationshipTypes() YIELD relationshipType
RETURN relationshipType
ORDER BY relationshipType
