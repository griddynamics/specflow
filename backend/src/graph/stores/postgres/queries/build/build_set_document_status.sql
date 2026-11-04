-- Persist: set the status of documents in a batch. Parameters: project_id,
-- build_id, rows: a JSON list of document_id and status. written counts the
-- rows that found their document.
WITH r AS (
    SELECT
        e.item ->> 'document_id' AS document_id,
        e.item ->> 'status' AS status
    FROM jsonb_array_elements(cast(:rows AS jsonb)) AS e (item)
),

updated AS (
    UPDATE graph.node
    SET
        props = graph.props_merge(
            node.props,
            jsonb_build_object(
                'status', r.status, 'last_build_id', cast(:build_id AS text)
            )
        )
    FROM
        (SELECT DISTINCT ON (r1.document_id)
            r1.document_id,
            r1.status
        FROM r AS r1) AS r
    WHERE
        node.group_id = :project_id
        AND node.kind = 'document'
        AND node.document_id = r.document_id
    RETURNING node.id
)

SELECT count(*) AS written
FROM r
INNER JOIN graph.node AS d
    ON
        d.group_id = :project_id
        AND d.kind = 'document'
        AND r.document_id = d.document_id
