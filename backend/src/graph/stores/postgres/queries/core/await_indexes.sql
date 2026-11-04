-- The indexes of the graph schema that are not valid: a failed or running
-- concurrent build (unscoped). PostgreSQL builds an index inside CREATE INDEX,
-- so the count is 0 once schema setup returned. Parameter: schema.
SELECT count(*) AS invalid
FROM pg_index AS x
INNER JOIN pg_class AS c ON x.indexrelid = c.oid
INNER JOIN pg_namespace AS ns ON c.relnamespace = ns.oid
WHERE ns.nspname = :schema AND NOT x.indisvalid
