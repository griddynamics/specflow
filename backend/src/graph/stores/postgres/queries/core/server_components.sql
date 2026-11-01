-- Server name, version and vector extension version, for the version gate
-- (unscoped). edition is empty (PostgreSQL has none); vector_version is empty
-- when the extension is missing.
SELECT
    'PostgreSQL' AS component,
    '' AS edition,
    ARRAY[current_setting('server_version')] AS versions,
    coalesce(
        (
            SELECT e.extversion FROM pg_extension AS e
            WHERE e.extname = 'vector'
        ),
        ''
    ) AS vector_version
