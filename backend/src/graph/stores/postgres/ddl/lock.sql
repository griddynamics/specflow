-- The advisory lock that serialises schema setup. Transaction level: released
-- on commit or rollback, safe behind a pooler. Concurrent DDL without it fails
-- with duplicate keys in pg_namespace.
SELECT pg_advisory_xact_lock(hashtext('specflow.graph.ensure_schema'))
