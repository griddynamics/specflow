-- The schema itself, its tables, indexes and functions, by name (unscoped). Parameter: schema.
SELECT
    c.relname AS name,
    'relation' AS kind
FROM pg_class AS c
INNER JOIN pg_namespace AS ns ON c.relnamespace = ns.oid
WHERE ns.nspname = :schema AND c.relkind IN ('r', 'i')
UNION ALL
SELECT
    p.proname AS name,
    'function' AS kind
FROM pg_proc AS p
INNER JOIN pg_namespace AS ns ON p.pronamespace = ns.oid
WHERE ns.nspname = :schema
UNION ALL
SELECT
    ns.nspname AS name,
    'schema' AS kind
FROM pg_namespace AS ns
WHERE ns.nspname = :schema
