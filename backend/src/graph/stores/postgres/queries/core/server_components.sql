-- Server name, version and the vector extension version, for the version gate (unscoped).
-- edition is empty (PostgreSQL has none); vector_version is empty when the extension is missing.
SELECT
    'PostgreSQL' AS name,
    ARRAY[current_setting('server_version')] AS versions,
    '' AS edition,
    coalesce(
        (SELECT e.extversion FROM pg_extension AS e WHERE e.extname = 'vector'),
        ''
    ) AS vector_version
