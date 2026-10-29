UPDATE connection_items i
SET state = 'duplicate', error_reason = NULL, error_step = NULL
FROM documents d
WHERE
    d.id = i.document_id AND d.source_id IS NOT NULL
    AND d.source_id <> i.connection_id
    AND i.state IN ('processed', 'error') /*i_scope*/
