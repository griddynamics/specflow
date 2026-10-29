UPDATE documents d SET source_id = c.id
FROM connections c
WHERE
    d.origin = 'upload' AND d.source_id IS NULL /*d_scope*/
    AND c.project_id = d.project_id AND c.kind = 'upload'
    AND c.external_id = 'legacy-'
    || to_char((d.created_at AT TIME ZONE 'UTC')::date, 'YYYYMMDD')
