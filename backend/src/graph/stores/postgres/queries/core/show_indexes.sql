-- The indexes of the graph schema, for diagnostics (unscoped).
-- Parameter: schema.
WITH found AS (
    SELECT
        c.relname AS index_name,
        t.relname AS table_name,
        x.indisunique AS is_unique,
        x.indisvalid AS is_valid,
        pg_get_indexdef(x.indexrelid) AS definition
    FROM pg_index AS x
    INNER JOIN pg_class AS c ON x.indexrelid = c.oid
    INNER JOIN pg_class AS t ON x.indrelid = t.oid
    INNER JOIN pg_namespace AS ns ON c.relnamespace = ns.oid
    WHERE ns.nspname = :schema
)

SELECT
    index_name,
    table_name,
    is_unique,
    is_valid,
    definition
FROM found
ORDER BY index_name COLLATE "C"
