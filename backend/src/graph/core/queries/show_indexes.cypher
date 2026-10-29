// The indexes the database holds, for schema setup (unscoped).
SHOW INDEXES
YIELD name, type, entityType, labelsOrTypes, properties, state, options
RETURN name, type, entityType, labelsOrTypes, properties, state, options
