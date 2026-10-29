INSERT INTO connections (
    id, project_id, kind, external_id, link, name, state, connected_by,
    expected_files, ended_at, name_suffix, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    f.project_id,
    'upload',
    'legacy-' || f.day_key,
    NULL,
    'Local files',
    'done',
    COALESCE(f.uploaded_by, p.created_by),
    NULL,
    f.created_at,
    1 + COALESCE((
        SELECT max(c.name_suffix) FROM connections c
        WHERE
            c.project_id = f.project_id AND c.kind = 'upload'
            AND date_trunc('minute', c.created_at)
            = date_trunc('minute', f.created_at)
    ), 0),
    f.created_at,
    f.created_at
FROM (
    SELECT DISTINCT ON (d.project_id, (d.created_at AT TIME ZONE 'UTC')::date)
        d.project_id,
        to_char((d.created_at AT TIME ZONE 'UTC')::date, 'YYYYMMDD') AS day_key,
        d.created_at,
        d.uploaded_by
    FROM documents d
    WHERE d.origin = 'upload' AND d.source_id IS NULL /*d_scope*/
    ORDER BY
        d.project_id,
        (d.created_at AT TIME ZONE 'UTC')::date,
        d.created_at,
        d.id
) f
JOIN projects p ON p.id = f.project_id
ON CONFLICT (project_id, kind, external_id) DO NOTHING
