-- Infer (cross_document): the title of each document of :document_ids, else
-- its path, else its id. Parameters: document_ids (text[]).
WITH found AS (
    SELECT
        d.document_id,
        CASE
            WHEN coalesce(d.props ->> 'title', '') <> '' THEN d.props ->> 'title'
            WHEN coalesce(d.props ->> 'path', '') <> '' THEN d.props ->> 'path'
            ELSE d.document_id
        END AS title
    FROM unnest(cast(:document_ids AS text[])) AS w (document_id)
    INNER JOIN graph.node AS d
        ON
            d.group_id = :project_id
            AND d.kind = 'document'
            AND w.document_id = d.document_id
)

SELECT
    document_id,
    title
FROM found
ORDER BY document_id COLLATE "C"
