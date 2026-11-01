-- The constraints of the graph schema (primary and foreign keys), for
-- diagnostics (unscoped). Parameter: schema.
WITH found AS (
    SELECT
        k.conname AS constraint_name,
        k.contype AS constraint_type,
        t.relname AS table_name
    FROM pg_constraint AS k
    INNER JOIN pg_class AS t ON k.conrelid = t.oid
    INNER JOIN pg_namespace AS ns ON t.relnamespace = ns.oid
    WHERE ns.nspname = :schema
)

SELECT
    constraint_name,
    constraint_type,
    table_name
FROM found
ORDER BY constraint_name COLLATE "C"
