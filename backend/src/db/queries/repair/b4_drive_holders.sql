UPDATE documents d SET source_id = h.connection_id
FROM (
    SELECT DISTINCT ON (i.document_id)
        i.document_id,
        i.connection_id
    FROM connection_items i
    JOIN connections c ON c.id = i.connection_id
    WHERE i.document_id IS NOT NULL /*i_scope*/
    ORDER BY i.document_id, c.created_at, c.id
) h
WHERE
    d.id = h.document_id AND d.origin = 'connection'
    AND d.source_id IS NULL /*d_scope*/
