INSERT INTO connection_items (
    id, connection_id, project_id, external_id, name, mime_type, category,
    processing_class, size_bytes, state, document_id, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    c.id,
    d.project_id,
    d.content_hash,
    d.original_filename,
    d.media_type,
    CASE
        WHEN d.media_type = 'application/pdf' THEN 'pdf' ELSE 'other_files'
    END,
    'processed',
    d.size_bytes,
    'pending',
    d.id,
    d.created_at,
    d.created_at
FROM documents d
JOIN connections c
    ON
        c.project_id = d.project_id AND c.kind = 'upload'
        AND c.external_id = 'legacy-'
        || to_char((d.created_at AT TIME ZONE 'UTC')::date, 'YYYYMMDD')
WHERE d.origin = 'upload' AND d.source_id IS NULL /*d_scope*/
ON CONFLICT (connection_id, external_id) DO NOTHING
