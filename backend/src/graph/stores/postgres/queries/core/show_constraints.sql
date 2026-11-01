-- The constraints of the graph schema (primary keys, foreign keys), for diagnostics (unscoped).
SELECT
    k.conname AS name,
    k.contype AS type,
    t.relname AS table_name
FROM pg_constraint AS k
INNER JOIN pg_class AS t ON k.conrelid = t.oid
INNER JOIN pg_namespace AS ns ON t.relnamespace = ns.oid
WHERE ns.nspname = :schema
ORDER BY k.conname COLLATE "C"
