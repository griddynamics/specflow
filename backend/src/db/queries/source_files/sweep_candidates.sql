SELECT
    EXISTS(
        SELECT 1 FROM connections
        WHERE id = :source_id AND project_id = :project_id
    ) AS present,
    ARRAY(
        SELECT candidate.document_id
        FROM UNNEST(:document_ids) AS candidate (document_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM documents AS d
            WHERE d.id = candidate.document_id
        )
    ) AS missing
